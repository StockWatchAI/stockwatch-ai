import os
import json
import traceback
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from anthropic import Anthropic
from datetime import datetime, timedelta

import i18n

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
    price = d.get("c") or 0
    # **値が取れない銘柄は落とす。** Finnhubは現行プランで米国株しか返さず、
    # 米国以外は 200 のまま全項目0で返ってくる。そのまま通すと
    # 「7203.T +0.00%」のような中身の無い通知を送ることになる
    if not price:
        return None
    return {"price": price, "change_pct": d.get("dp", 0) or 0}


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
    # 通知をタップしたときに開く記事が要るので、見出しとURLを対で持つ
    return [
        {"headline": i.get("headline", ""), "url": i.get("url", "")}
        for i in items
    ]


def summarize(symbol, quote, headlines, language_name):
    """1銘柄ぶんの一文を、指定された言語で書かせる。

    **日本語だけ従来の文面を残す。** この文面で調整してきたので、英語の
    プロンプトに一本化すると既存の日本語の利用者の通知が変わってしまう。
    日本語以外は英語の指示に「この言語で書け」を添える形にする
    （言語を増やしてもプロンプトを書き足さずに済む）。アプリ側の
    `AIPersonality.systemPrompt` と同じ考え方。
    """
    if language_name == "Japanese":
        news_text = "\n".join("- " + h for h in headlines) if headlines else "- 特になし"
        prompt = f"""あなたは株式市場の朝ブリーフィングを書くアナリストです。

銘柄: {symbol}
株価: ${quote['price']}（前日比 {quote['change_pct']:+.2f}%）
直近ニュース見出し:
{news_text}

上記をもとに、日本語で60字以内の一文を書いてください。
値動きの理由と今日の注目点を簡潔に。前置き・挨拶・記号は不要。本文のみ出力。"""
    else:
        news_text = "\n".join("- " + h for h in headlines) if headlines else "- none"
        prompt = f"""You are an analyst writing a one-line morning briefing.

Ticker: {symbol}
Price: ${quote['price']} ({quote['change_pct']:+.2f}% from the previous close)
Recent headlines:
{news_text}

Write a single sentence in {language_name}, no more than 100 characters.
Give the reason for the move and what to watch today. No preamble, no greeting,
no bullet marks. Output the sentence only."""

    res = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    # 先頭が必ずテキストとは限らないので、テキストのブロックを探して取る。
    # ここで落ちると全員ぶんのブリーフィングが止まる
    for block in res.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""


def main():
    users = list(db.collection("users").stream())
    print(f"ユーザー数: {len(users)}")

    all_symbols = set()
    for u in users:
        all_symbols.update(u.to_dict().get("watchlist", []))
    print(f"対象銘柄: {all_symbols}")

    # 株価とニュースは言語に依らないので、銘柄ごとに1回だけ取る
    market = {}
    for sym in all_symbols:
        try:
            q = get_quote(sym)
            if q is None:
                # 米国以外の銘柄。Finnhubが値を返さないため、この銘柄は載せない
                print(f"{sym}: 株価が取れないためスキップ（米国株以外の可能性）")
                continue
            market[sym] = {"quote": q, "news": get_news(sym)}
        except Exception as e:
            print(f"{sym} の取得に失敗: {repr(e)}")
            traceback.print_exc()

    # 要約は (銘柄, 言語) ごとに作る。
    # **使われている言語のぶんだけ作る。** 利用者の大半が日本語なら、
    # 呼び出し回数はこれまでと変わらない
    summaries = {}

    def summary_for(symbol, language_name):
        key = (symbol, language_name)
        if key in summaries:
            return summaries[key]
        entry = market[symbol]
        try:
            text = summarize(
                symbol,
                entry["quote"],
                [i["headline"] for i in entry["news"]],
                language_name,
            )
        except Exception as e:
            print(f"{symbol}({language_name}) の要約に失敗: {repr(e)}")
            traceback.print_exc()
            text = ""
        summaries[key] = text
        print(f"{symbol} [{language_name}]: {text}")
        return text

    for u in users:
        data = u.to_dict()
        token = data.get("fcmToken")
        watchlist = data.get("watchlist", [])
        if not token or not watchlist:
            print(f"{u.id}: トークンかウォッチリストが空のためスキップ")
            continue
        # アプリの通知設定。値が無い利用者は既定でオンとして扱う
        if data.get("notifyMorning", True) is False:
            print(f"{u.id}: notifyMorning がオフのためスキップ")
            continue

        valid = [s for s in watchlist if s in market]
        if not valid:
            print(f"{u.id}: 有効な銘柄データがないためスキップ")
            continue

        # AIの出力言語。指定が無い利用者は表示言語、それも無ければ日本語
        language_name = i18n.ai_language_name(data)

        lead = max(valid, key=lambda s: abs(market[s]["quote"]["change_pct"]))
        lq = market[lead]["quote"]
        # 件名は銘柄コードと変化率だけなので、言語によらず同じ
        title = f"{lead} {lq['change_pct']:+.1f}%"
        body = summary_for(lead, language_name)
        if not body:
            # 要約が作れなかったときは、事実だけの一文に落とす。
            # 本文が空の通知を送るより、値動きだけでも伝わるほうがよい
            body = i18n.t(
                data,
                f"{lead} は {lq['price']:,.2f} ドル（前日比 {lq['change_pct']:+.2f}%）です。",
                f"{lead} is at ${lq['price']:,.2f} ({lq['change_pct']:+.2f}% from the previous close).",
            )

        db.collection("briefings").document(u.id).set({
            "createdAt": firestore.SERVER_TIMESTAMP,
            "items": [
                {
                    "symbol": s,
                    "price": market[s]["quote"]["price"],
                    "changePct": market[s]["quote"]["change_pct"],
                    "summary": summary_for(s, language_name),
                }
                for s in valid
            ],
        })

        # タップしたときに開く記事。ニュースが取れない銘柄もあるので空を許す。
        # 空文字を送っておけばアプリ側は「記事なし」として銘柄だけ開く
        lead_news = market[lead].get("news") or []
        article_url = lead_news[0]["url"] if lead_news else ""

        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            # アプリはこの2つを見てニュースタブと記事を開く。
            # キー名はアプリ側のNotificationRouterと合わせる
            data={"symbol": lead, "url": article_url},
            token=token,
        )
        try:
            res = messaging.send(msg)
            print(f"通知送信成功: {u.id} → {title} / FCM応答={res}")
        except Exception as e:
            print(f"通知送信失敗: {u.id} → {repr(e)}")


if __name__ == "__main__":
    main()
