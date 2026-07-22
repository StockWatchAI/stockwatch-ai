import os
import json
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from anthropic import Anthropic

FINNHUB_KEY = os.environ["FINNHUB_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

# Firebase 初期化
cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
firebase_admin.initialize_app(cred)
db = firestore.client()
claude = Anthropic(api_key=ANTHROPIC_KEY)


def get_quote(symbol):
    """株価を取得"""
    r = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": FINNHUB_KEY},
        timeout=10,
    )
    d = r.json()
    return {
        "price": d.get("c", 0),
        "change_pct": d.get("dp", 0),
    }


def get_news(symbol):
    """直近ニュースの見出しを最大3件取得"""
    from datetime import datetime, timedelta
    today = datetime.utcnow().date()
    r = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": symbol,
            "from": str(today - timedelta(days=2)),
            "to": str(today),
            "token": FINNHUB_KEY,
        },
        timeout=10,
    )
    items = r.json()[:3]
    return [i.get("headline", "") for i in items]


def summarize(symbol, quote, headlines):
    """Claudeで1銘柄ぶんの短い要約を作る"""
    prompt = f"""あなたは米国株の朝ブリーフィングを書くアナリストです。

銘柄: {symbol}
株価: ${quote['price']}（前日比 {quote['change_pct']:+.2f}%）
直近ニュース見出し:
{chr(10).join('- ' + h for h in headlines) if headlines else '- 特になし'}

上記をもとに、日本語で60字以内の一文を書いてください。
値動きの理由と、今日の注目点を簡潔に。前置き・挨拶・記号は不要。本文のみ出力。"""

    res = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return res.content[0].text.strip()


def main():
    # 1. 銘柄ごとの要約を作る（キャッシュ方式：同じ銘柄は1回だけ生成）
    users = list(db.collection("users").stream())
    print(f"ユーザー数: {len(users)}")

    all_symbols = set()
    for u in users:
        all_symbols.update(u.to_dict().get("watchlist", []))
    print(f"対象銘柄: {all_symbols}")

    cache = {}
    for sym in all_symbols:
        try:
            q = get_quote(sym)
            n = get_news(sym)
            cache[sym] = {
                "quote": q,
                "summary": summarize(sym, q, n),
            }
            print(f"{sym}: {cache[sym]['summary']}")

except Exception as e:
            import traceback
            print(f"{sym} の処理に失敗: {repr(e)}")
            traceback.print_exc() 

print(f"FINNHUBキーの長さ: {len(FINNHUB_KEY)} 文字, 先頭4文字: {FINNHUB_KEY[:4]}")

    # 2. ユーザーごとに通知を送る
    for u in users:
        data = u.to_dict()
        token = data.get("fcmToken")
        watchlist = data.get("watchlist", [])
        if not token or not watchlist:
            continue

        # 変動率が一番大きい銘柄を主役にする
        valid = [s for s in watchlist if s in cache]
        if not valid:
            continue
        lead = max(valid, key=lambda s: abs(cache[s]["quote"]["change_pct"]))
        lq = cache[lead]["quote"]

        title = f"{lead} {lq['change_pct']:+.1f}%"
        body = cache[lead]["summary"]

        # 全銘柄ぶんの詳細をFirestoreに保存（アプリで開いたとき用）
        db.collection("briefings").document(u.id).set({
            "createdAt": firestore.SERVER_TIMESTAMP,
            "items": [
                {
                    "symbol": s,
                    "price": cache[s]["quote"]["price"],
                    "changePct": cache[s]["quote"]["change_pct"],
                    "summary": cache[s]["summary"],
                }
                for s in valid
            ],
        })

        # プッシュ通知
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token,
        )
        try:
            messaging.send(msg)
            print(f"通知送信成功: {u.id} → {title}")
        except Exception as e:
            print(f"通知送信失敗: {u.id} → {e}")


if __name__ == "__main__":
    main()
