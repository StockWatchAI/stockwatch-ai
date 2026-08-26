#!/usr/bin/env python3
"""米国株の株価・ニュース・決算・ロゴをFirestoreに寄せる（アプリの直叩きをやめるため）。

    python3 scripts/market_cache.py --inspect          # 取得と解釈だけ（Firestoreに触れない）
    python3 scripts/market_cache.py quotes             # 時間外気配 → quotes/{SYMBOL}
    python3 scripts/market_cache.py symbols            # ニュース・決算・ロゴ → symbols/{SYMBOL}

**なぜアプリから外すのか。**

1. **APIキーがバイナリに露出していた。** Info.plist にキーを置いてクライアントから
   直接叩いていたので、配布したipaから取り出せる。実際にFirestoreのルールを
   確かめたときと同じ手順で誰でも取れる
2. **レート制限が端末ごとにかかる。** 利用者が増えるほど更新頻度を下げるしかない
3. **APIコールが利用者数に比例する。** ここに寄せれば全利用者で1回になる

`ExtendedHoursQuotes.swift` の末尾に、この形を前提にした
`FirestoreQuoteProvider` のひな型が最初から置いてある。**そこに書いてある
`quotes/{SYMBOL}` の項目名に合わせてある**（勝手に変えるとアプリ側が読めない）。

## 書き込み先

    quotes/{SYMBOL}     regularClose / extendedPrice / session / asOf（Yahoo）
    symbols/{SYMBOL}    news[] / earnings / lastEarnings / logo
    config/newsSources  通す配信元の一覧（アプリも読む）

**どれも利用者由来のデータを置かない。** ここは全利用者が読めるので、
`market/` と同じ扱いにする（`CLAUDE.md` のセキュリティの節を参照）。

## ニュースの配信元を絞る

Finnhubは新しい順に返すので、先頭から取るとアグリゲーター記事ばかりになる
（AAPLの実測で248件中166件がYahoo）。`news_sources.py` のホワイトリストで
絞ったうえで、配信元ごとに1件ずつ拾って偏りをならす。

**絞った結果が0件になったら、絞る前の一覧に戻す。** 記事が1本も出ない画面は
体験として最悪で、フィルタの副作用としては許容できない。戻したことは
`filtered: false` で伝えて、アプリ側で区別できるようにしてある。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import news_sources

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
TIMEOUT = 20

FINNHUB = "https://finnhub.io/api/v1"

QUOTES_COLLECTION = "quotes"
SYMBOLS_COLLECTION = "symbols"
WATCHED_DOC = ("config", "watchedTickers")
SOURCES_DOC = ("config", "newsSources")

# 1銘柄あたりの保存件数。アプリは米国株で20件まで出す
NEWS_LIMIT = 20
NEWS_DAYS = 7

# 外部APIの間隔。Finnhubの無料枠は60 req/min なので、余裕を持って1秒に1回
API_INTERVAL = 1.0

_last_call = 0.0


# 実行中に出た失敗の内訳。**1銘柄ずつ印字すると本当の異常が埋もれる。**
_failures = {}


def call(url, params, label, headers=None):
    """外部APIを1回叩く。**失敗はNoneに畳む**（1銘柄で全体を止めない）"""
    global _last_call
    gap = API_INTERVAL - (time.monotonic() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.monotonic()
    try:
        res = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        res.raise_for_status()
        return res.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        # **402は「そのプランでは扱えない銘柄」。** 異常ではないので数えるだけ
        _failures.setdefault(code, []).append(label)
        if code != 402:
            print(f"    {label} に失敗: {e}")
        return None
    except Exception as e:
        _failures.setdefault("other", []).append(label)
        print(f"    {label} に失敗: {e}")
        return None


def report_failures():
    """失敗の内訳をまとめて1回だけ出す"""
    for code, labels in sorted(_failures.items(), key=lambda x: str(x[0])):
        note = ""
        if code == 429:
            note = "（レート制限。API_INTERVAL を延ばす）"
        head = ", ".join(l.split()[0] for l in labels[:12])
        more = f" ほか{len(labels) - 12}件" if len(labels) > 12 else ""
        print(f"  取得できず [{code}] {len(labels)}件{note}: {head}{more}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 米国市場のセッション
#
# **アプリの `USMarketClock` と同じ区切りにしてある。** ずれると、アプリが
# 「市場前」と出しているのにFirestoreには「閉場」と書かれる、という食い違いが出る。
# 夏時間は ZoneInfo に任せる（手計算しない）。祝日は持っていない。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def session_now(now=None):
    now = (now or datetime.now(UTC)).astimezone(ET)
    if now.weekday() >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "preMarket"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular"
    if 16 * 60 <= minutes < 20 * 60:
        return "postMarket"
    return "closed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 時間外気配（Yahoo Finance）
#
# **FMPをやめてYahooにした。** FMPの無料プランは大半の銘柄を「有料シンボル」
# として402で弾く。実測（2026-08-27）で**47銘柄中32銘柄が402**で、
# SNDK・ARM・AMAT・IONQ などの気配が一度も表示できていなかった。
#
#     FMP     15 / 47 銘柄
#     Yahoo   47 / 47 銘柄（取れなかった銘柄はゼロ）
#
# NVDAで突き合わせるとYahoo 218.87 / FMP 218.87〜219.48 でほぼ一致する。
#
# 分足に `includePrePost=true` を付けると、通常取引の前後のバーも返る。
# `meta.currentTradingPeriod.regular` が当日の通常取引の開始・終了なので、
# その外側のバーが時間外にあたる。
#
# **鍵が要らない。** Yahooは株価・チャート・配当・52週レンジの取得元でもあるので、
# 利用者から見た第三者サービスが1つ減る（プライバシーポリシーの記載も変わる）。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def yahoo_extended(symbol):
    """`(通常取引の終値, 時間外の値, その時刻)` を返す。取れなければ `(None, None, None)`

    **全項目を欠損に強く読む。** 非公式なので、項目が欠けても落とさない。
    """
    data = call(
        YAHOO_CHART.format(symbol=symbol),
        {"interval": "1m", "range": "1d", "includePrePost": "true"},
        f"{symbol} の気配",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        result = (data.get("chart", {}).get("result") or [None])[0]
    except AttributeError:
        return None, None, None
    if not result:
        return None, None, None

    meta = result.get("meta") or {}
    regular_close = meta.get("regularMarketPrice")
    if regular_close is None:
        return None, None, None

    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = period.get("start"), period.get("end")

    stamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    bars = [(t, c) for t, c in zip(stamps, closes) if c is not None]
    if not bars or start is None or end is None:
        return regular_close, None, None

    # **通常取引の外側のバーだけを見る。** 引け後があればそちら、
    # 無ければ寄り前（その日の通常取引が始まる前）
    after = [b for b in bars if b[0] >= end]
    before = [b for b in bars if b[0] < start]
    picked = after[-1] if after else (before[-1] if before else None)
    if not picked:
        return regular_close, None, None

    return regular_close, picked[1], datetime.fromtimestamp(picked[0], UTC)


def build_quote(symbol, session):
    regular, extended, as_of = yahoo_extended(symbol)
    if regular is None:
        return None

    # ザラ場中は時間外の気配が存在しない（現在値は株価カード側が出す）。
    # 閉場中は「直近セッションの最終気配」を出す
    # （日本の日中は米国市場が丸ごと閉まっているので、ここを空にすると終日空になる）
    if session == "regular":
        extended, as_of = None, None

    return {
        "symbol": symbol,
        "regularClose": regular,
        "extendedPrice": extended,
        "session": session,
        "asOf": as_of or datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ニュース・決算・ロゴ（Finnhub）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def diversified(items, limit):
    """配信元が偏らないように選ぶ。

    アプリの `MarketData.diversified` と同じ考え方。配信元ごとの列を作り、
    順番に1件ずつ拾って各社の新しい記事が上位に来るようにする。
    """
    by_source, order = {}, []
    for item in items:
        s = item.get("source") or ""
        if s not in by_source:
            order.append(s)
            by_source.setdefault(s, [])
        by_source[s].append(item)

    picked, round_no = [], 0
    while len(picked) < limit:
        added = False
        for s in order:
            column = by_source[s]
            if round_no < len(column):
                picked.append(column[round_no])
                added = True
                if len(picked) == limit:
                    break
        if not added:
            break
        round_no += 1
    return picked


def fetch_news(symbol, key, allowed, min_kept=0):
    """1銘柄ぶんのニュース。`(記事, 絞ったか)` を返す。

    **実測（2026-08-26）: Finnhubの現行プランが返す配信元は5つしかない。**

        銘柄   全件  通過  内訳
        AAPL    90    3   Benzinga×38, Yahoo×30, SeekingAlpha×12, ChartMill×7, CNBC×3
        NVDA   245   20   Benzinga×114, SeekingAlpha×52, Yahoo×39, ChartMill×20, CNBC×20
        MSFT   111    1   Benzinga×49, Yahoo×35, SeekingAlpha×19, ChartMill×7, CNBC×1
        PFE     32    0   Yahoo×20, Benzinga×7, SeekingAlpha×4, ChartMill×1

    通す表に当たるのは **CNBCだけ**で、Reuters・Bloomberg・AP・WSJは1件も来ない。
    つまりこの絞り込みをFinnhubに掛けると「CNBCだけの面」になり、
    銘柄によっては0件になってフォールバックで絞る前に戻る。
    **銘柄ごとに件数が1件だったり20件だったりして揃わない。**

    これは表の調整で直る話ではなく、取得元をEDGARに寄せる（Phase 2）ことでしか
    解けない。`min_kept` は、その移行のあいだ「何件を下回ったら絞らないことにするか」を
    Firestoreから変えられるようにするための逃げ道。**既定は0**（仕様書どおり
    「0件になったときだけ戻す」）。
    """
    today = datetime.now(UTC).date()
    data = call(
        f"{FINNHUB}/company-news",
        {"symbol": symbol, "from": str(today - timedelta(days=NEWS_DAYS)),
         "to": str(today), "token": key},
        f"{symbol} のニュース",
    )
    if not isinstance(data, list):
        return [], False

    raw = [
        {
            "id": item.get("id") or 0,
            "headline": item.get("headline") or "",
            "source": item.get("source") or "",
            "datetime": item.get("datetime") or 0,
            "url": item.get("url") or "",
            "summary": item.get("summary") or "",
            "image": item.get("image") or "",
        }
        for item in data
        if item.get("headline") and item.get("url")
    ]

    kept, dropped = news_sources.filter_items(raw, allowed)
    if dropped:
        top = sorted(dropped.items(), key=lambda x: -x[1])[:5]
        print(f"    落とした配信元: {', '.join(f'{n}×{c}' for n, c in top)}")

    # **少なすぎたら絞る前に戻す。** 記事が全く出ないほうが体験として悪い
    if len(kept) <= min_kept:
        print(f"    {symbol}: 絞ると {len(kept)} 件（下限 {min_kept}）なので絞らない一覧に戻す")
        return diversified(raw, NEWS_LIMIT), False

    return diversified(kept, NEWS_LIMIT), True


def fetch_earnings(symbol, key):
    """次回決算の予定と、直近発表ぶんのEPS実績"""
    today = datetime.now(UTC).date()
    upcoming = call(
        f"{FINNHUB}/calendar/earnings",
        {"from": str(today), "to": str(today + timedelta(days=120)),
         "symbol": symbol, "token": key},
        f"{symbol} の決算予定",
    )
    events = (upcoming or {}).get("earningsCalendar") or []
    events.sort(key=lambda e: e.get("date") or "")

    history = call(f"{FINNHUB}/stock/earnings", {"symbol": symbol, "token": key},
                   f"{symbol} の決算実績")
    last = None
    if isinstance(history, list) and history:
        # 返る順序が発表日順とは限らない。対象四半期の末日が最も新しいものを選ぶ
        last = max(history, key=lambda e: e.get("period") or "")

    return (events[0] if events else None), last


def fetch_logo(symbol, key):
    profile = call(f"{FINNHUB}/stock/profile2", {"symbol": symbol, "token": key},
                   f"{symbol} の会社情報")
    return (profile or {}).get("logo") or None


def build_symbol(symbol, key, allowed, min_kept=0):
    news, filtered = fetch_news(symbol, key, allowed, min_kept)
    upcoming, last = fetch_earnings(symbol, key)
    return {
        "symbol": symbol,
        "news": news,
        # 絞る前に戻したときは false。アプリ側で区別できるようにする
        "newsFiltered": filtered,
        "nextEarnings": upcoming,
        "lastEarnings": last,
        "logo": fetch_logo(symbol, key),
        "updatedAt": datetime.now(UTC),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Firestore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 和集合を作り直す間隔。`edgar.py` の `WATCHED_MAX_AGE` と揃えてある。
# **片方だけ長いと、新しく登録した銘柄がニュースだけ出ない／開示だけ出ない、
# という中途半端な状態になる**
WATCHED_MAX_AGE = timedelta(hours=1)


def load_watched(db):
    """ウォッチリストの和集合のうち、**米国株だけ**。

    `edgar.py` と同じ集約ドキュメントを読む。**古ければこちらでも作り直す。**
    読むだけにしていると、`edgar.py` が回っていない時間帯（週末など）に
    登録した銘柄がいつまでも対象に入らない。どちらが書いても同じ内容になるので、
    先に書いたほうが残って困らない。
    """
    doc = db.collection(WATCHED_DOC[0]).document(WATCHED_DOC[1]).get()
    if doc.exists:
        data = doc.to_dict() or {}
        tickers = data.get("tickers") or []
        updated = data.get("updatedAt")
        if tickers and updated:
            try:
                if datetime.now(UTC) - datetime.fromisoformat(updated) < WATCHED_MAX_AGE:
                    return sorted(tickers)
            except ValueError:
                pass

    print("集約ドキュメントが古い（または無い）ので users から作る")
    symbols = set()
    for u in db.collection("users").stream():
        symbols.update((u.to_dict() or {}).get("watchlist") or [])
    tickers = sorted(s for s in symbols if "." not in s and s)
    db.collection(WATCHED_DOC[0]).document(WATCHED_DOC[1]).set(
        {"tickers": tickers, "updatedAt": datetime.now(UTC).isoformat()}
    )
    return tickers


def load_allowed_sources(db):
    """通す配信元の一覧。**Firestoreの値を優先する**（アプリ更新なしで調整するため）"""
    doc = db.collection(SOURCES_DOC[0]).document(SOURCES_DOC[1]).get()
    if doc.exists:
        data = doc.to_dict() or {}
        allowed = data.get("allowed") or []
        if allowed:
            min_kept = int(data.get("minKept") or 0)
            print(f"配信元の一覧をFirestoreから読んだ: {len(allowed)} 件 / 下限 {min_kept} 件")
            return allowed, min_kept

    # 初回は既定値を書いておく。**書いておかないと、調整したくなったときに
    # 「どこを直せばよいか」が分からない**（空のドキュメントは手がかりにならない）
    print("配信元の一覧が無いので既定値を書き込む")
    db.collection(SOURCES_DOC[0]).document(SOURCES_DOC[1]).set(
        {
            "allowed": news_sources.DEFAULT_ALLOWED,
            "knownExcluded": news_sources.KNOWN_EXCLUDED,
            # 絞った結果がこの件数以下なら絞らない一覧に戻す。
            # 0 は「0件のときだけ戻す」の意味（仕様書の既定）
            "minKept": 0,
            "updatedAt": datetime.now(UTC).isoformat(),
        }
    )
    return news_sources.DEFAULT_ALLOWED, 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_quotes(db, tickers):
    # **鍵は要らない。** Yahooは認証なしで引ける
    session = session_now()
    print(f"米国市場: {session} / 対象 {len(tickers)} 銘柄")

    dry_run = os.environ.get("DRY_RUN") == "1"
    written = 0
    for symbol in tickers:
        quote = build_quote(symbol, session)
        if not quote:
            continue
        ext = quote["extendedPrice"]
        print(f"  {symbol:6} 終値={quote['regularClose']} 時間外={ext}")
        if not dry_run:
            db.collection(QUOTES_COLLECTION).document(symbol).set(quote)
            written += 1
    report_failures()
    print(f"{'DRY_RUN のため書き込まず' if dry_run else f'書き込み {written} 件 / 対象 {len(tickers)} 銘柄'}")


def run_symbols(db, tickers):
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        print("FINNHUB_API_KEY が無いため中止")
        return

    allowed, min_kept = load_allowed_sources(db)
    dry_run = os.environ.get("DRY_RUN") == "1"
    written = 0
    for symbol in tickers:
        print(f"  {symbol}")
        doc = build_symbol(symbol, key, allowed, min_kept)
        print(f"    ニュース {len(doc['news'])} 件 (絞った={doc['newsFiltered']}) "
              f"決算={bool(doc['nextEarnings'])} ロゴ={bool(doc['logo'])}")
        if not dry_run:
            db.collection(SYMBOLS_COLLECTION).document(symbol).set(doc)
            written += 1
    report_failures()
    print(f"{'DRY_RUN のため書き込まず' if dry_run else f'書き込み {written} 件 / 対象 {len(tickers)} 銘柄'}")


def inspect():
    """**Firestoreに触れない。** 手元の見本の銘柄で、取得と絞り込みだけ試す"""
    print(f"いまの米国市場: {session_now()}\n")

    finnhub_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    print(f"FINNHUB_API_KEY: {'あり' if finnhub_key else 'なし'}")
    print("気配はYahooから取るので鍵は不要\n")

    print("=== 配信元の絞り込み（表そのものの確認） ===")
    samples = ["Reuters", "reuters.com", "Bloomberg", "PR Newswire", "GlobeNewswire",
               "CNBC", "Motley Fool", "Zacks", "Benzinga", "TipRanks", "Yahoo",
               "InvestorPlace", "日本経済新聞", "한국경제"]
    for s in samples:
        mark = "通す" if news_sources.is_allowed(s) else "落とす"
        note = "（判断できないので通す）" if news_sources.is_undecidable(s) else ""
        print(f"  {s:24} {mark}{note}")

    if finnhub_key:
        print("\n=== AAPL のニュース ===")
        news, filtered = fetch_news("AAPL", finnhub_key, news_sources.DEFAULT_ALLOWED)
        print(f"  絞った後: {len(news)} 件 (絞った={filtered})")
        for n in news[:8]:
            print(f"    {n['source']:22} {n['headline'][:60]}")

    print("\n=== 気配（Yahoo・鍵不要）===")
    for sym in ("AAPL", "NVDA", "SNDK"):
        q = build_quote(sym, session_now())
        if q:
            print(f"  {sym:6} 終値={q['regularClose']} 時間外={q['extendedPrice']} 時刻={q['asOf']}")
        else:
            print(f"  {sym:6} 取れず")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=["quotes", "symbols"])
    parser.add_argument("--inspect", action="store_true", help="取得と絞り込みだけ試す")
    args = parser.parse_args()

    if args.inspect:
        sys.exit(inspect())
    if not args.mode:
        parser.error("mode（quotes / symbols）か --inspect を指定してください")

    import firebase_admin
    from firebase_admin import credentials, firestore

    firebase_admin.initialize_app(
        credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    )
    db = firestore.client()

    watched = load_watched(db)
    if not watched:
        print("対象の米国株が無いため中止")
        sys.exit(0)

    if args.mode == "quotes":
        run_quotes(db, watched)
    else:
        run_symbols(db, watched)
