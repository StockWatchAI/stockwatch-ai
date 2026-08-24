#!/usr/bin/env python3
"""主要指数が引けたときにプッシュ通知を送る。

    python3 scripts/market_close.py jp   # 日経平均（15:30 JST 引け）
    python3 scripts/market_close.py us   # S&P 500 とダウ（16:00 ET 引け）

朝のブリーフィングと同じく GitHub Actions の cron から呼ばれ、Firestore の
users に入っている FCM トークン宛に送る。

指数の値は Yahoo Finance から取る。Finnhub は現行プランで指数を返さないため。
非公式のエンドポイントなので、取得できなければ何も送らずに終了する。
誤った値を送るより、送らないほうがよい。

祝日は cron では判別できないので、取得したデータが「今日の取引ぶん」か
どうかを取引所の現地日付で確かめ、違えば送信しない。
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import firebase_admin
import requests
from firebase_admin import credentials, firestore, messaging

import i18n

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# 市場ごとの構成。表示順は通知に出したい順。
# 指数名と単位は言語ごとに持つ（利用者の表示言語で出し分ける）
MARKETS = {
    "jp": {
        "label_ja": "東京市場",
        "label_en": "The Tokyo market",
        "indices": [
            {"symbol": "^N225", "ja": "日経平均", "en": "Nikkei 225", "unit_ja": "円", "unit_en": ""},
        ],
        # アプリの設定画面と対応するFirestoreの項目名。
        # 値が無い利用者は、これまでどおり受け取る扱いにする
        "pref": "notifyJPClose",
    },
    "us": {
        "label_ja": "米国市場",
        "label_en": "US markets",
        "indices": [
            {"symbol": "^GSPC", "ja": "S&P500", "en": "S&P 500", "unit_ja": "", "unit_en": ""},
            {"symbol": "^DJI", "ja": "ダウ", "en": "Dow", "unit_ja": "", "unit_en": ""},
        ],
        "pref": "notifyUSClose",
    },
}


def fetch_index(symbol):
    """指数の終値と前日比を取る。取れなければ None。"""
    r = requests.get(
        YAHOO_CHART.format(symbol=requests.utils.quote(symbol)),
        params={"interval": "1d", "range": "5d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    meta = result["meta"]

    price = meta.get("regularMarketPrice")
    market_time = meta.get("regularMarketTime")
    if price is None or market_time is None:
        return None

    # 休場日は終値が null で入るので、日付と対にして取り除く
    closes = result["indicators"]["quote"][0]["close"]
    series = [(t, c) for t, c in zip(result["timestamp"], closes) if c is not None]
    if not series:
        return None

    offset = meta.get("gmtoffset", 0)

    def local_day(ts):
        return int((ts + offset) // 86_400)

    # 末尾が当日ぶんなら、その1つ前が前日終値。
    # meta.previousClose は空で返り、chartPreviousClose は取得範囲の直前を指すため使わない
    if local_day(series[-1][0]) == local_day(market_time) and len(series) >= 2:
        previous = series[-2][1]
    else:
        previous = series[-1][1]

    change = price - previous
    return {
        "price": price,
        "change": change,
        "change_pct": (change / previous * 100) if previous else 0.0,
        "market_time": market_time,
        "offset": offset,
    }


def traded_today(data):
    """取得したデータが今日の取引ぶんかを、取引所の現地日付で確かめる。

    祝日や臨時休場では前営業日の値が返るため、それを今日の終値として
    送らないようにする。
    """
    offset = timezone(timedelta(seconds=data["offset"]))
    market_date = datetime.fromtimestamp(data["market_time"], offset).date()
    today = datetime.now(offset).date()
    return market_date == today


def build_message(market, quotes, language):
    """通知の件名と本文を、指定した言語で組み立てる。

    `language` は `i18n.JA` か `i18n.EN`。**全員ぶんを1回で作らない。**
    利用者ごとに表示言語が違うため、言語の数だけ作って送るときに選ぶ。
    """
    conf = MARKETS[market]
    japanese = language == i18n.JA
    key = "ja" if japanese else "en"

    # 件名は変化率だけを並べ、通知一覧で一目で分かるようにする。
    # 区切りは日本語なら全角空白、英語なら中黒（英字が続くと全角空白は間延びする）
    separator = "　" if japanese else "  ·  "
    title = separator.join(
        f"{index[key]} {q['change_pct']:+.2f}%" for index, q in zip(conf["indices"], quotes)
    )

    lines = []
    for index, q in zip(conf["indices"], quotes):
        unit = index["unit_ja"] if japanese else index["unit_en"]
        if japanese:
            lines.append(f"{index['ja']} {q['price']:,.2f}{unit}（前日比 {q['change']:+,.2f}）")
        else:
            lines.append(f"{index['en']} {q['price']:,.2f}{unit} ({q['change']:+,.2f})")

    if japanese:
        head = f"{conf['label_ja']}が取引を終えました。"
    else:
        head = f"{conf['label_en']} closed."
    body = head + "\n" + "\n".join(lines)
    return title, body


def send(messages, pref_key):
    """利用者ごとに、その人の表示言語の文面を選んで送る。

    `messages` は言語コード → (件名, 本文) の辞書。
    """
    users = list(db.collection("users").stream())
    print(f"ユーザー数: {len(users)}")

    sent = failed = skipped = 0
    for u in users:
        data = u.to_dict()
        token = data.get("fcmToken")
        if not token:
            print(f"{u.id}: トークンが無いためスキップ")
            continue
        # 設定が無い利用者は既定でオン。アプリを更新していない人にも届く
        if data.get(pref_key, True) is False:
            skipped += 1
            print(f"{u.id}: {pref_key} がオフのためスキップ")
            continue

        # 言語の指定が無い利用者は日本語。アプリを更新していない人の
        # 通知がある日いきなり英語になるのを避ける
        title, body = messages[i18n.display_language(data)]
        try:
            res = messaging.send(
                messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    token=token,
                )
            )
            sent += 1
            print(f"通知送信成功: {u.id} → FCM応答={res}")
        except Exception as e:
            failed += 1
            print(f"通知送信失敗: {u.id} → {repr(e)}")

    print(f"送信 {sent} 件 / 失敗 {failed} 件 / 設定オフ {skipped} 件")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MARKETS:
        sys.exit("使い方: market_close.py [jp|us]")
    market = sys.argv[1]
    conf = MARKETS[market]

    quotes = []
    for index in conf["indices"]:
        symbol, name = index["symbol"], index["ja"]
        try:
            data = fetch_index(symbol)
        except Exception as e:
            print(f"{name}({symbol}) の取得に失敗: {repr(e)}")
            return
        if data is None:
            print(f"{name}({symbol}) のデータが空のため中止")
            return
        if not traded_today(data):
            print(f"{name}({symbol}) は本日の取引データではないため中止（休場と判断）")
            return
        print(f"{name}: {data['price']:,.2f} ({data['change_pct']:+.2f}%)")
        quotes.append(data)

    # 言語ごとに1回だけ作る。利用者ごとに作ると同じ文字列を人数ぶん組み立てることになる
    messages = {
        lang: build_message(market, quotes, lang) for lang in (i18n.JA, i18n.EN)
    }
    for lang, (title, body) in messages.items():
        print(f"[{lang}] 件名: {title}")
        print(f"[{lang}] 本文: {body}")

    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN のため送信しません")
        return

    send(messages, conf["pref"])


if __name__ == "__main__":
    # 送信しない確認だけなら認証情報を要求しない
    if os.environ.get("DRY_RUN") != "1":
        cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
    main()
