import os
import json
import traceback
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from anthropic import Anthropic
from datetime import datetime, timedelta

FINNHUB_KEY = os.environ["FINNHUB_API_KEY"].strip()
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

print(f"FINNHUBキー: {len(FINNHUB_KEY)}文字")
print(f"ANTHROPICキー: {len(ANTHROPIC_KEY)}文字")

cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
firebase_admin.initialize_app(cred)
db = firestore.client()
claude = Anthropic(api_key=ANTHROPIC_KEY)


def get_quote(symbol):
    r = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": FINNHUB_KEY},
        timeout=10,
    )
    d = r.json()
    print(f"  [{symbol}] 株価API応答: {d}")
    return {"price": d.get("c", 0), "change_pct": d.get("dp", 0) or 0}


def get_news(symbol):
    today = datetime.utcnow().date()
    r = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": symbol,
            "from": str(today - timedelta(days=3)),
            "to": str(today),
            "token": FINNHUB_KEY,
        },
        timeout=10,
    )
    data = r.json()
    items = data[:3] if isinstance(data, list) else []
    print(f"  [{symbol}] ニュース {len(items)}件")
    return [i.get("headline", "") for i in items]


def summarize(symbol, quote, headlines):
    news_text = "\n".join("- " + h for h in headlines) if headlines else "- 特になし"
    prompt = f"""あなたは米国株の朝ブリーフィングを書くアナリストです。

銘柄: {symbol}
株価: ${quote['price']}（前日比 {quote['change_pct']:+.2f}%）
直近ニュース見出し:
{news_text}

上記をもとに、日本語で60字以内の一文を書いてください。
値動きの理由と今日の注目点を簡潔に。前置き・挨拶・記号は不要。本文のみ出力。"""

    res = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return res.content[0].text.strip()


def main():
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
            s = summarize(sym, q, n)
            cache[sym] = {"quote": q, "summary": s}
            print(f"{sym}: {s}")
        except Exception as e:
            print(f"{sym} の処理に失敗: {repr(e)}")
            traceback.print_exc()

    for u in users:
        data = u.to_dict()
        token = data.get("fcmToken")
        watchlist = data.get("watchlist", [])
        if not token or not watchlist:
            print(f"{u.id}: トークンかウォッチリストが空のためスキップ")
            continue

        valid = [s for s in watchlist if s in cache]
        if not valid:
            print(f"{u.id}: 有効な銘柄データがないためスキップ")
            continue

        lead = max(valid, key=lambda s: abs(cache[s]["quote"]["change_pct"]))
        lq = cache[lead]["quote"]
        title = f"{lead} {lq['change_pct']:+.1f}%"
        body = cache[lead]["summary"]

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

        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token,
        )
        try:
            res = messaging.send(msg)
            print(f"通知送信成功: {u.id} → {title} / FCM応答={res}")
        except Exception as e:
            print(f"通知送信失敗: {u.id} → {repr(e)}")


if __name__ == "__main__":
    main()
