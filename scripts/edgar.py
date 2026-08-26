#!/usr/bin/env python3
"""SEC EDGARの提出書類を一次情報のニュースとして取り込み、Firestoreに置く。

    python3 scripts/edgar.py --inspect   # 取得と解釈だけ（Firestore・Claude・通知に触れない）
    python3 scripts/edgar.py             # 取り込んでFirestoreへ書く

**なぜEDGARなのか。** これまでの米国株のニュースはFinnhub経由で、返ってくる記事の
大半が「3 Reasons to Buy NVDA Now」系のアグリゲーター記事だった。事実の報道ではなく
意見の再生産なので、要約に投げても中身が出てこない。EDGARは会社が自分で出した
一次情報で、提出とほぼ同時に反映され、米国政府の著作物なので再配信もできる。
認証も要らない（APIキーという概念が無い）。

**銘柄ごとにEDGARを叩かない。** 全社横断の getcurrent フィードを取ってから
自前で絞る。こうするとウォッチリストの銘柄数に関係なくリクエスト数が一定になる。

---

## 実測で分かった、仕様書と違うところ

**1. 13D/13Gのフォーム名は `SCHEDULE 13D` / `SCHEDULE 13G`。**
`SC 13D` では0件で返る（2024年の様式変更で名前が変わっている）。
`SC 13` で引くと `SC 13E3` だけが釣れるので、0件を「今日は提出が無い」と
読み違えやすい。**フォーム名は下の `FORMS` に集約してある。**

**2. 8-KのItem番号はフィードのsummaryに入っている。**

    <summary type="html">
     <b>Filed:</b> 2026-08-25 <b>AccNo:</b> 0001493152-26-040103 <b>Size:</b> 19 MB
     <br>Item 7.01: Regulation FD Disclosure
     <br>Item 9.01: Financial Statements and Exhibits
    </summary>

つまり**本文を取る前にItem番号で捨てられる**。低重要度の提出をLLMに投げずに済む。

**3. Form 4・13Dはフィードに2回出る。** 提出者側（`(Reporting)` / `(Filed by)`）と
対象会社側（`(Issuer)` / `(Subject)`）で1件ずつ。**銘柄に紐付けられるのは後者だけ**で、
前者のCIKは提出した個人・運用会社のものになる。EDINETの `issuerEdinetCode` と同じ話。
accessionNoは両方同じなので、docIDに使えば重複は自然に消える。

**4. 8-Kの中身は本文ではなく添付にあることが多い。** Item 2.02（業績発表）の本文は
「詳細はExhibit 99.1のとおり」で終わっていて、実測したAAPLの8-Kでは本文が
タグを落として4KB、中身のあるEX-99.1が173KBだった。**本文だけ要約すると
「決算を発表しました」しか書けない**ので、EX-99系の添付も一緒に読む。

## EDGARへのアクセス規則（守らないと予告なくブロックされる）

- User-Agentは**必須**。連絡先を含める（`USER_AGENT`）
- 10 req/sec 以下。ここでは安全側で **5 req/sec（200ms間隔）**
- IPやUser-Agentのローテーションは**禁止**。規約違反でブロック対象
- 429/403/503 が返ったら間隔を広げて待つ。実測でも探りを入れすぎて503を食らった

## 取り込みの冪等性

**docIDにaccessionNoを使う。** cronが二重に走っても同じ提出物が二度登録されない。
書く前にも既存のdocIDを引いて、要約のコストごと省いている。
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# **連絡先を必ず入れる。** SECは連絡先の無いUAを名乗るクライアントを拒否する
USER_AGENT = "StockWatchAI/2.0 (shuichi@tinkermode.com)"

BROWSE_EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar"
COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"

# リクエストの最小間隔。10 req/sec が上限なので半分に取ってある
MIN_INTERVAL = 0.2
TIMEOUT = 30

# **フォーム名は実測で確かめたものだけを置く。** `SC 13D` は0件で返る
FORMS = ["8-K", "4", "SCHEDULE 13D", "SCHEDULE 13G"]

# getcurrent 1ページの件数（上限100）と、1フォームあたりの最大ページ数。
# 実測では引け後のいちばん混む時間帯でも8-Kの100件が88分ぶんあったので、
# 10分間隔のcronなら1ページで足りる。取りこぼしの保険として2ページまで見る
PAGE_SIZE = 100
MAX_PAGES = 2

NEWS_COLLECTION = "news"
WATCHED_DOC = ("config", "watchedTickers")

# Firestoreの保持期間。TTLポリシーで自動削除させる
RETENTION_DAYS = 90

# ウォッチリストの和集合を作り直す間隔。**毎回usersを全件読むと利用者数に比例して
# コストが増える**ので、集約ドキュメントに残して使い回す。
#
# **短くしてある理由。** ここが古いと、利用者が新しく登録した米国株は
# ニュースも開示も気配も**何も出ない**（アプリはFirestoreしか見ない）。
# 6時間にしていたときは、登録してから記事が出るまで最大7時間かかる計算だった。
# 利用者数が数十のうちは1時間ごとに全件読んでも安い。
# 増えてきたら、ここではなくCloud Functionsでの都度更新に切り替える
WATCHED_MAX_AGE = timedelta(hours=1)

# Form 4 の足切り。役員の少額取引まで通すと通知疲れを起こす（単位: 米ドル）
FORM4_MIN_VALUE = 100_000

# 要約に渡す本文の上限。8-K本体は数KBだが、添付を足すと数百KBになる
MAX_BODY_CHARS = 12_000

# 一度の実行で要約する上限。**暴発したときの請求額を止める。** 提出が
# 一気に増えても、次の実行で続きを拾えばよい（accessionNoで重複しない）
MAX_SUMMARIES_PER_RUN = 40

# 仕分けに使うモデルと、重要な提出を書き直すモデル。
# 8-K本体は数KB程度なのでHaikuで足り、critical/highだけ上位モデルで作り直す
MODEL_DEFAULT = "claude-haiku-4-5"
MODEL_IMPORTANT = "claude-sonnet-4-6"

UTC = timezone.utc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8-K の Item 番号 → 重要度
#
# 8-KはItem番号で内容が決まる。**本文を取る前にここで捨てる。**
# 低重要度をLLMに投げるとコストの無駄になる。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ITEM_IMPACT = {
    "4.02": "critical",  # 過去の財務諸表が信頼できない
    "1.03": "critical",  # 破産・管財手続き
    "4.01": "high",      # 会計監査人の変更
    "2.02": "high",      # 業績発表
    "5.02": "high",      # 役員・取締役の異動
    "1.01": "high",      # 重要な契約の締結
    "2.01": "high",      # 資産の取得・処分（M&A）
    "1.05": "high",      # サイバーセキュリティインシデント
    "3.01": "high",      # 上場廃止の通知
    "2.03": "medium",    # 借入など財務上の債務の発生
    "3.02": "medium",    # 新株の発行（登録なし）＝希薄化
    "2.05": "medium",    # リストラ費用の計上
    "7.01": "medium",    # Reg FD 開示
    "8.01": "medium",    # その他の重要事象
    "5.07": "low",       # 株主総会の議決結果
    "9.01": "low",       # 財務諸表・添付書類（単独では出さない）
}

IMPACT_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# **この2つだけの提出は出さない。** 9.01は添付の目録、5.07は総会の集計で、
# 単独ではニュースにならない。他のItemと一緒に出ているぶんは残る
ITEMS_NOT_NEWSWORTHY = {"9.01", "5.07"}

# フォーム種別ごとの既定の重要度（8-K以外はItem番号を持たない）
FORM_IMPACT = {
    "SCHEDULE 13D": "high",    # 経営関与の意図あり。アクティビスト参入
    "SCHEDULE 13G": "medium",  # 純投資
    "4": "medium",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Form 4 の取引コード
#
# **市場での売買（P/S）だけを通す。** 付与・オプション行使・納税のための
# 引き渡し・贈与は本人の判断を表さないので、件数だけ増えてノイズになる。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FORM4_KEEP_CODES = {"P", "S"}
FORM4_CODE_LABEL = {"P": "買付", "S": "売却"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTTP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
_last_request = 0.0


def fetch(url, params=None, retries=3):
    """EDGARを1回叩く。**間隔を必ず空ける。**

    429/403/503 は「速すぎる」の合図なので、待ち時間を倍にして下がる。
    **User-Agentのローテーションはしない**（規約違反でブロック対象）。
    """
    global _last_request
    wait = 2.0
    for attempt in range(retries + 1):
        gap = MIN_INTERVAL - (time.monotonic() - _last_request)
        if gap > 0:
            time.sleep(gap)
        _last_request = time.monotonic()

        try:
            res = _session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == retries:
                raise
            print(f"    通信に失敗（{e}）。{wait:.0f}秒待って再試行")
            time.sleep(wait)
            wait *= 2
            continue

        if res.status_code in (403, 429, 503):
            if attempt == retries:
                res.raise_for_status()
            print(f"    {res.status_code} が返った。{wait:.0f}秒待って再試行: {url}")
            time.sleep(wait)
            wait *= 2
            continue

        res.raise_for_status()
        return res

    raise RuntimeError(f"取得できなかった: {url}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ティッカー ⇄ CIK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_cik_map():
    """CIK → {ticker, name} の対応表。

    1社が複数のティッカーを持つことがある（種類株）。**先に来たものを優先する。**
    company_tickers.json は時価総額の大きい順に並んでいるので、
    先頭にある普通株のティッカーが残る。
    """
    data = fetch(COMPANY_TICKERS).json()
    mapping = {}
    for row in data.values():
        cik = int(row["cik_str"])
        if cik in mapping:
            continue
        mapping[cik] = {"ticker": row["ticker"], "name": row["title"]}
    return mapping


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# getcurrent フィードの解釈
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_SUMMARY = re.compile(r"<summary[^>]*>(.*?)</summary>", re.S)
_UPDATED = re.compile(r"<updated>(.*?)</updated>", re.S)
_TERM = re.compile(r'label="form type"\s+term="([^"]*)"')
_HREF = re.compile(r'href="([^"]+)"')
# 「8-K - Apple Inc. (0000320193) (Filer)」の形。役割は付かないこともある
_TITLE_PARTS = re.compile(r"^(.*?)\s+-\s+(.*?)\s+\((\d{10})\)(?:\s+\((.*?)\))?\s*$", re.S)
_ACCN = re.compile(r"AccNo:</b>\s*([\d-]+)")
_FILED = re.compile(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})")
_ITEM = re.compile(r"Item\s+(\d+\.\d+)\s*:")

# 対象会社の側の項目にだけ付く役割。**提出者側のCIKで銘柄を引かない**
ISSUER_ROLES = {"issuer", "subject"}


def parse_feed(xml):
    """getcurrentのAtomを項目の一覧にする。

    **全項目を欠損に強く読む。** 非公式ではないが様式の保証も無いので、
    必要な項目が欠けている項目は黙って落とす（1件のために全体を止めない）。
    """
    out = []
    for raw in _ENTRY.findall(xml):
        title_m = _TITLE.search(raw)
        summary_m = _SUMMARY.search(raw)
        if not title_m or not summary_m:
            continue
        title = html.unescape(title_m.group(1)).strip()
        summary = html.unescape(summary_m.group(1))

        parts = _TITLE_PARTS.match(title)
        if not parts:
            continue
        form = (_TERM.search(raw).group(1) if _TERM.search(raw) else parts.group(1)).strip()
        name = parts.group(2).strip()
        cik = int(parts.group(3))
        role = (parts.group(4) or "").strip().lower()

        accn_m = _ACCN.search(summary)
        if not accn_m:
            continue

        href_m = _HREF.search(raw)
        updated_m = _UPDATED.search(raw)
        filed_m = _FILED.search(summary)

        out.append(
            {
                "form": form,
                "companyName": name,
                "cik": cik,
                "role": role,
                "accessionNo": accn_m.group(1),
                "items": _ITEM.findall(summary),
                "filedDate": filed_m.group(1) if filed_m else None,
                "acceptedAt": updated_m.group(1).strip() if updated_m else None,
                "indexUrl": href_m.group(1) if href_m else None,
            }
        )
    return out


def fetch_current(form):
    """1フォーム種別ぶんの最新提出。ページを送りながら集める"""
    entries = []
    for page in range(MAX_PAGES):
        res = fetch(
            BROWSE_EDGAR,
            params={
                "action": "getcurrent",
                "type": form,
                "output": "atom",
                "count": str(PAGE_SIZE),
                "start": str(page * PAGE_SIZE),
            },
        )
        page_entries = parse_feed(res.text)
        entries.extend(page_entries)
        # 埋まっていないページが来たら、それ以上は無い
        if len(page_entries) < PAGE_SIZE:
            break
    return entries


def select(entries, form, cik_map, watched):
    """フィードの項目から、対象銘柄ぶんだけを accessionNo 単位でまとめる。

    **対象会社側の項目からしか銘柄を引かない。** Form 4 と 13D は
    提出者側にも同じaccessionNoで1件出るが、そちらのCIKは提出した個人や
    運用会社のもので、銘柄には対応しない。
    """
    picked = {}
    for e in entries:
        # 修正報告（8-K/A など）も本物の変化なので残す。ただし別フォームは弾く
        if not e["form"].startswith(form):
            continue
        # 役割が付くフォームでは、対象会社側の項目だけを見る
        if e["role"] and e["role"] not in ISSUER_ROLES and form != "8-K":
            continue

        company = cik_map.get(e["cik"])
        if not company or company["ticker"] not in watched:
            continue

        accn = e["accessionNo"]
        if accn in picked:
            # 同じ提出が複数の項目で出ることがある。Item番号は取れたほうを残す
            if e["items"] and not picked[accn]["items"]:
                picked[accn]["items"] = e["items"]
            continue

        e = dict(e)
        e["ticker"] = company["ticker"]
        # フィードの社名は提出者の名前になっていることがある。対応表を正とする
        e["companyName"] = company["name"]
        picked[accn] = e
    return picked


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 重要度の仕分け
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def triage(entry):
    """要約に投げる前の仕分け。`(出すか, 重要度)` を返す。

    8-KはItem番号で決まる。**9.01と5.07だけの提出は出さない**（添付の目録と
    総会の集計で、単独ではニュースにならない）。
    """
    form = entry["form"]
    if form.startswith("8-K"):
        items = entry["items"]
        if not items:
            # Item番号が読めなかった。捨てずに中位で通す（様式が変わっても止めない）
            return True, "medium"
        if all(i in ITEMS_NOT_NEWSWORTHY for i in items):
            return False, "low"
        impacts = [ITEM_IMPACT.get(i, "medium") for i in items]
        return True, max(impacts, key=lambda x: IMPACT_ORDER[x])

    for prefix, impact in FORM_IMPACT.items():
        if form.startswith(prefix):
            return True, impact
    return True, "medium"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 提出物の本文
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
# インラインXBRLの隠し要素。本文の前に `iso4217:USD xbrli:shares …` が並ぶだけで、
# 読ませても意味が無いうえに本文に使える文字数を食う
_IX_HIDDEN = re.compile(r"<ix:(header|hidden)\b.*?</ix:\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v ]+")
_BLANK = re.compile(r"\n{3,}")


def to_text(markup):
    """HTML/XMLから本文を取り出す。整形は最小限でよい（読むのはLLM）"""
    s = _IX_HIDDEN.sub(" ", markup)
    s = _SCRIPT.sub(" ", s)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _SPACE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _BLANK.sub("\n\n", s).strip()


# **提出物のファイル名は submissions JSON から取る。**
#
# `Archives/.../index.json` はすぐ後に見に行くと**中身が揃っていない**。実測では
# 提出の2時間後でも本体（`quartzsea_8k.htm`）が一覧に無く、目次と提出物まるごとの
# `.txt` しか返らなかった。一方 `data.sec.gov/submissions/CIK*.json` は
# `primaryDocument` を持っていて、同じ提出物をその場で指せる。
#
# `primaryDocument` は `xslF345X05/wk-form4_1787701042.xml` のように、
# **人が読む用に整形するXSLの経路が前に付く**ことがある。生のファイルは
# 同じ階層の直下にあるので、その部分を落として使う。

_submissions_cache = {}


def submission_index(cik):
    """会社1社ぶんの提出履歴。1回の実行では会社ごとに1回だけ取る"""
    if cik in _submissions_cache:
        return _submissions_cache[cik]
    try:
        recent = fetch(SUBMISSIONS.format(cik=cik)).json()["filings"]["recent"]
    except Exception as e:
        print(f"    提出履歴を取れなかった（CIK {cik}）: {e}")
        _submissions_cache[cik] = {}
        return {}

    index = {}
    for i, accn in enumerate(recent.get("accessionNumber", [])):
        index[accn] = {
            "primaryDocument": recent.get("primaryDocument", [""] * (i + 1))[i],
            "items": recent.get("items", [""] * (i + 1))[i],
            "acceptedAt": recent.get("acceptanceDateTime", [""] * (i + 1))[i],
        }
    _submissions_cache[cik] = index
    return index


def primary_document(entry):
    """提出物の本体のURL。見つからなければ None"""
    record = submission_index(entry["cik"]).get(entry["accessionNo"])
    if not record or not record["primaryDocument"]:
        return None
    # `xslF345X05/primary_doc.xml` のような整形用の経路を落とす
    name = record["primaryDocument"].split("/")[-1]
    base = ARCHIVES.format(cik=entry["cik"], accn=entry["accessionNo"].replace("-", ""))
    return f"{base}/{name}"


def exhibit_urls(entry, limit=3):
    """EX-99系の添付。

    **8-Kの中身は本体ではなく添付にあることが多い。** Item 2.02（業績発表）の
    本体は「詳細はExhibit 99.1のとおり」で終わっていて、実測したAAPLの8-Kでは
    本体がタグを落として4KB、中身のあるEX-99.1が173KBだった。
    一覧が揃っていない提出では空になるが、そのときは本体だけで作る。
    """
    base = ARCHIVES.format(cik=entry["cik"], accn=entry["accessionNo"].replace("-", ""))
    try:
        items = fetch(f"{base}/index.json").json().get("directory", {}).get("item", [])
    except Exception:
        return []

    found = []
    for it in items:
        name = it.get("name", "")
        low = name.lower()
        if not low.endswith((".htm", ".html", ".txt")):
            continue
        if "ex99" in low or "ex-99" in low or low.startswith("ex99"):
            found.append(f"{base}/{name}")
    return found[:limit]


def read_document(url):
    try:
        return to_text(fetch(url).text)
    except Exception as e:
        print(f"    {url.rsplit('/', 1)[-1]} を取れなかった: {e}")
        return ""


def filing_body(entry):
    """要約に渡す本文。本体＋EX-99系の添付を、上限まで順に足す"""
    primary = primary_document(entry)
    if not primary:
        print(f"    本体が分からない: {entry['accessionNo']}")
        return ""

    chunks = []
    total = 0
    for url in [primary] + exhibit_urls(entry):
        if total >= MAX_BODY_CHARS:
            break
        text = read_document(url)
        if len(text) < 200:
            continue
        room = MAX_BODY_CHARS - total
        chunks.append(text[:room])
        total += min(len(text), room)

    return "\n\n---\n\n".join(chunks)





# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Form 4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# **Form 4 のXMLには2つの形がある。**
#
#     <transactionShares><value>99706</value></transactionShares>   ← 包む
#     <transactionCode>S</transactionCode>                          ← 包まない
#
# `<value>` を前提にした読み方で `transactionCode` を引くと必ず None になり、
# **売買コードの判定が常に外れて1件も通らなくなる**（実際にそうなっていた。
# S が22件・P が3件出ている一覧で採用0件だった）。読み方を分けてある。

_XML_VALUE = r"<{tag}>\s*<value>(.*?)</value>"
_XML_PLAIN = r"<{tag}>(.*?)</{tag}>"


def _tag_value(block, tag):
    """`<tag><value>…</value></tag>` の形から取る"""
    m = re.search(_XML_VALUE.format(tag=tag), block, re.S)
    return m.group(1).strip() if m else None


def _tag_text(block, tag):
    """`<tag>…</tag>` の形から取る。社名や役職に `&amp;` が入るので実体参照を戻す"""
    m = re.search(_XML_PLAIN.format(tag=tag), block, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def parse_form4(xml):
    """Form 4 のXMLから、市場での売買（P/S）だけを取り出す。

    XMLは `issuerTradingSymbol` を持っているので、**ここではCIKの対応表が要らない**。
    ただし取りに行く前に絞り込みたいので、フィードの段階ではCIKで引いている。
    """
    owner = _tag_text(xml, "rptOwnerName") or ""
    title = _tag_text(xml, "officerTitle") or ""
    symbol = _tag_text(xml, "issuerTradingSymbol") or ""

    trades = []
    for block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        code = _tag_text(block, "transactionCode")
        if code not in FORM4_KEEP_CODES:
            continue
        try:
            shares = float(_tag_value(block, "transactionShares") or 0)
            price = float(_tag_value(block, "transactionPricePerShare") or 0)
        except ValueError:
            continue
        value = shares * price
        if value < FORM4_MIN_VALUE:
            continue
        trades.append(
            {
                "code": code,
                "shares": shares,
                "price": price,
                "value": value,
                "date": _tag_value(block, "transactionDate") or "",
            }
        )

    return {"owner": owner, "officerTitle": title, "symbol": symbol, "trades": trades}


def form4_body(entry):
    """Form 4 を要約に渡せる文章にする。**該当する取引が無ければ空を返す**（＝出さない）"""
    url = primary_document(entry)
    if not url or not url.lower().endswith(".xml"):
        return "", None

    try:
        parsed = parse_form4(fetch(url).text)
    except Exception as e:
        print(f"    Form 4 を読めなかった: {e}")
        return "", None

    if not parsed["trades"]:
        return "", parsed

    lines = [
        f"Insider: {parsed['owner']}" + (f" ({parsed['officerTitle']})" if parsed["officerTitle"] else ""),
        f"Issuer: {entry['companyName']} ({entry['ticker']})",
        "Open-market transactions reported on this Form 4:",
    ]
    for t in parsed["trades"]:
        kind = "Purchase" if t["code"] == "P" else "Sale"
        lines.append(
            f"- {t['date']} {kind}: {t['shares']:,.0f} shares at ${t['price']:,.2f} "
            f"(about ${t['value']:,.0f})"
        )
    return "\n".join(lines), parsed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 要約
#
# **助言業の線を越えさせない。** 月額課金のアプリで個別銘柄の助言をすると
# 投資助言・代理業の登録が要る（金融庁の監督指針 VII-3-1(2)②イ）。
# 起きた事実の説明に徹させ、売買の示唆・目標株価・値動きの予測を書かせない。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYSTEM_PROMPT = """You summarize SEC EDGAR filings for a stock-watching app.

You are describing what a company disclosed. You are NOT giving investment advice.

Absolute constraints — the app is a paid subscription, and advice on individual
securities behind a paywall requires an investment-advisory registration:

- Never suggest buying or selling, and never imply it is a good or bad time to do either.
- Never give a price target, rating, score, or recommendation.
- Never predict that the share price will rise or fall.
- Describe what happened. Keep interpretation to the minimum needed to make the
  facts understandable.
- Do not state anything the filing does not say. If a number is unclear, leave it out.

Write plainly, for someone who does not read filings."""

# **出力の形をスキーマで固定する。** プロンプトで頼むだけだと、たまに前置きが
# 付いて `json.loads` が落ちる。落ちた1件のために実行ごと止めたくない
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline_en": {"type": "string"},
        "headline_ja": {"type": "string"},
        "summary_en": {"type": "string"},
        "summary_ja": {"type": "string"},
        "impact": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "impact_reason_en": {"type": "string"},
        "impact_reason_ja": {"type": "string"},
    },
    "required": [
        "headline_en", "headline_ja", "summary_en", "summary_ja",
        "impact", "impact_reason_en", "impact_reason_ja",
    ],
    "additionalProperties": False,
}

# **英語と日本語だけ作る。** アプリの表示言語がこの2つで、AIの出力言語15言語は
# 利用者ごとに違う。全員ぶんを先に作ると15倍の費用がかかるうえ、
# ほとんど読まれない。読む人がいる2言語を用意し、他はアプリ側で英語に倒す
LANGUAGES = ("en", "ja")


def build_prompt(entry, impact, body):
    items = ", ".join(entry["items"]) if entry["items"] else "n/a"
    return f"""Summarize this SEC filing.

Form type: {entry['form']}
8-K item numbers: {items}
Company: {entry['companyName']}
Ticker: {entry['ticker']}
Filed: {entry['filedDate']}
Preliminary importance from the item number: {impact}

Filing text:
\"\"\"
{body}
\"\"\"

Produce:
- headline_en / headline_ja: at most 40 characters, stating what happened.
- summary_en / summary_ja: 3 to 5 sentences explaining what happened in plain language.
- impact: critical, high, medium or low. Start from the preliminary importance and
  change it only if the text clearly warrants it.
- impact_reason_en / impact_reason_ja: one sentence on why that importance.

The Japanese fields must be natural Japanese, not a literal translation."""


def summarize(client, entry, impact, body):
    """1件ぶんの要約。**失敗はNoneに畳む**（1件のために実行ごと止めない）"""
    model = MODEL_IMPORTANT if impact in ("critical", "high") else MODEL_DEFAULT
    try:
        res = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(entry, impact, body)}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
    except Exception as e:
        print(f"    要約に失敗: {e}")
        return None

    text = next((b.text for b in res.content if getattr(b, "type", None) == "text"), None)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"    要約のJSONを読めなかった: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Firestore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_watched(db):
    """ウォッチリスト銘柄の和集合。

    **毎回usersを全件読まない。** 集約ドキュメントに残して使い回し、古くなったら
    作り直す。仕様書ではCloud Functionsで更新する案だったが、このリポジトリには
    Functionsが無く、そのためだけに増やすと運用が1つ増える。**取りこぼしても
    次の作り直しで直る**ので、この形にしてある。
    """
    ref = db.collection(WATCHED_DOC[0]).document(WATCHED_DOC[1])
    doc = ref.get()
    if doc.exists:
        data = doc.to_dict() or {}
        updated = data.get("updatedAt")
        tickers = data.get("tickers") or []
        if tickers and updated:
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(updated)
                if age < WATCHED_MAX_AGE:
                    print(f"集約ドキュメントを使う: {len(tickers)} 銘柄（{age} 前に更新）")
                    return set(tickers)
            except ValueError:
                pass

    print("和集合を作り直す")
    symbols = set()
    for u in db.collection("users").stream():
        symbols.update((u.to_dict() or {}).get("watchlist") or [])

    # **米国株だけ。** EDGARは米国の制度なので、`.T` などの接尾辞が付くものは対象外
    tickers = sorted(s for s in symbols if "." not in s and s)
    ref.set({"tickers": tickers, "updatedAt": datetime.now(UTC).isoformat()})
    print(f"和集合: {len(tickers)} 銘柄")
    return set(tickers)


def already_stored(db, accession_numbers):
    """取り込み済みのaccessionNoを引く。**要約の前に引く**（費用が乗る前に捨てる）"""
    stored = set()
    refs = [db.collection(NEWS_COLLECTION).document(a) for a in accession_numbers]
    # get_all は一度に投げられる件数に上限があるので分けて投げる
    for i in range(0, len(refs), 200):
        for doc in db.get_all(refs[i:i + 200]):
            if doc.exists:
                stored.add(doc.id)
    return stored


def to_document(entry, impact, summary, extra=None):
    now = datetime.now(UTC)
    doc = {
        "ticker": entry["ticker"],
        "cik": f"{entry['cik']:010d}",
        "companyName": entry["companyName"],
        "source": "sec_edgar",
        "formType": entry["form"],
        "items": entry["items"],
        "filedAt": entry["filedDate"],
        "acceptedAt": entry["acceptedAt"],
        "accessionNo": entry["accessionNo"],
        # **原文へのリンクは必ず持たせる。** AIの要約だけを見せて終わりにしない
        "sourceUrl": entry["indexUrl"],
        "impact": summary.get("impact", impact),
        "headline": {lang: summary[f"headline_{lang}"] for lang in LANGUAGES},
        "summary": {lang: summary[f"summary_{lang}"] for lang in LANGUAGES},
        "impactReason": {lang: summary[f"impact_reason_{lang}"] for lang in LANGUAGES},
        "createdAt": now,
        # TTLポリシー（フィールド expiresAt）で自動削除させる
        "expiresAt": now + timedelta(days=RETENTION_DAYS),
    }
    if extra:
        doc.update(extra)
    return doc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 取り込み
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def collect_candidates(cik_map, watched):
    """全フォーム種別を回して、対象銘柄ぶんの候補を作る"""
    candidates = {}
    for form in FORMS:
        try:
            entries = fetch_current(form)
        except Exception as e:
            print(f"{form}: 取得に失敗したので飛ばす {e}")
            continue
        picked = select(entries, form, cik_map, watched)
        print(f"{form}: フィード {len(entries)} 件 → 対象銘柄 {len(picked)} 件")
        for accn, entry in picked.items():
            keep, impact = triage(entry)
            if not keep:
                continue
            entry["impact"] = impact
            candidates[accn] = entry
    return candidates


def build_body(entry):
    """フォーム種別ごとに本文を作る。空を返したら「出さない」の意味"""
    if entry["form"].startswith("4"):
        body, parsed = form4_body(entry)
        extra = None
        if body and parsed:
            extra = {
                "insider": {
                    "name": parsed["owner"],
                    "officerTitle": parsed["officerTitle"],
                    "trades": [
                        {
                            "code": t["code"],
                            "label": FORM4_CODE_LABEL[t["code"]],
                            "shares": t["shares"],
                            "price": t["price"],
                            "value": t["value"],
                            "date": t["date"],
                        }
                        for t in parsed["trades"]
                    ],
                }
            }
        return body, extra
    return filing_body(entry), None


def main(db, client):
    watched = load_watched(db)
    if not watched:
        print("対象の米国株が無いため中止")
        return

    cik_map = load_cik_map()
    print(f"CIKの対応表: {len(cik_map)} 社")

    candidates = collect_candidates(cik_map, watched)
    if not candidates:
        print("対象の提出が無い")
        return

    stored = already_stored(db, list(candidates))
    fresh = {a: e for a, e in candidates.items() if a not in stored}
    print(f"候補 {len(candidates)} 件 / 取り込み済み {len(stored)} 件 / 新規 {len(fresh)} 件")
    if not fresh:
        return

    # 重要なものから処理する。上限で切られても大事なものが残るように
    order = sorted(fresh.values(), key=lambda e: -IMPACT_ORDER[e["impact"]])
    if len(order) > MAX_SUMMARIES_PER_RUN:
        print(f"1回の上限 {MAX_SUMMARIES_PER_RUN} 件に絞る（残りは次回）")
        order = order[:MAX_SUMMARIES_PER_RUN]

    dry_run = os.environ.get("DRY_RUN") == "1"
    written = skipped = 0

    for entry in order:
        label = f"{entry['ticker']} {entry['form']} {entry['accessionNo']}"
        body, extra = build_body(entry)
        if not body:
            # Form 4 で市場での売買が無かった、本文を取れなかった等
            print(f"  {label}: 出す中身が無いので飛ばす")
            skipped += 1
            continue

        summary = summarize(client, entry, entry["impact"], body)
        if not summary:
            skipped += 1
            continue

        doc = to_document(entry, entry["impact"], summary, extra)
        print(f"  {label} [{doc['impact']}] {doc['headline']['ja']}")
        if dry_run:
            continue
        db.collection(NEWS_COLLECTION).document(entry["accessionNo"]).set(doc)
        written += 1

    if dry_run:
        print(f"DRY_RUN のため書き込みませんでした（対象 {len(order) - skipped} 件）")
    else:
        print(f"書き込み {written} 件 / 飛ばした {skipped} 件")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 構造の確認（副作用なし）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 様式を疑うときの見本。Firestoreを読めないので手元の一覧で確かめる
INSPECT_TICKERS = {
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "JPM", "V", "WMT", "XOM", "UNH", "PG", "JNJ", "HD", "KO", "PFE",
    "INTC", "AMD", "NFLX", "DIS", "BA", "F", "GM", "T", "CSCO", "ORCL",
}


def inspect():
    """**Firestoreにも要約にも触れない。** いつ走らせても副作用が無い"""
    print(f"User-Agent: {USER_AGENT}")
    print(f"リクエスト間隔: {MIN_INTERVAL * 1000:.0f}ms（{1 / MIN_INTERVAL:.0f} req/sec）\n")

    cik_map = load_cik_map()
    print(f"=== company_tickers.json: {len(cik_map)} 社 ===")
    for cik in list(cik_map)[:3]:
        print(f"  CIK {cik:010d} → {cik_map[cik]['ticker']:6} {cik_map[cik]['name']}")

    for form in FORMS:
        print(f"\n=== {form} ===")
        try:
            entries = fetch_current(form)
        except Exception as e:
            print(f"  取得に失敗: {e}")
            continue
        print(f"  フィード {len(entries)} 件")
        if not entries:
            print("  この時間帯は提出が無い（getcurrentは直近ぶんしか返さない）")
            continue

        roles = {}
        for e in entries:
            roles[e["role"] or "(なし)"] = roles.get(e["role"] or "(なし)", 0) + 1
        print(f"  役割の内訳: {roles}")

        resolved = sum(1 for e in entries if e["cik"] in cik_map)
        print(f"  ティッカーに解決できた: {resolved}/{len(entries)}")

        picked = select(entries, form, cik_map, INSPECT_TICKERS)
        print(f"  見本の銘柄に当たった: {len(picked)} 件")

        if form == "8-K":
            counts = {}
            for e in entries:
                for i in e["items"]:
                    counts[i] = counts.get(i, 0) + 1
            print("  Item番号の内訳（多い順・上位8）:")
            for i, n in sorted(counts.items(), key=lambda x: -x[1])[:8]:
                impact = ITEM_IMPACT.get(i, "medium（表に無い）")
                print(f"    {i}: {n:3} 件  → {impact}")

        for e in entries[:3]:
            keep, impact = triage(e)
            ticker = cik_map.get(e["cik"], {}).get("ticker", "-")
            print(f"    {e['accessionNo']} {ticker:6} {e['form']:14} "
                  f"items={e['items']} 出す={keep} 重要度={impact}")

        # 1件だけ本文まで取って、要約に渡せる形になるか確かめる
        sample = next((e for e in entries if e["cik"] in cik_map and triage(e)[0]), None)
        if sample:
            sample = dict(sample)
            sample["ticker"] = cik_map[sample["cik"]]["ticker"]
            sample["companyName"] = cik_map[sample["cik"]]["name"]
            body, extra = build_body(sample)
            print(f"\n  本文の見本 ({sample['ticker']} {sample['accessionNo']}): {len(body)} 文字")
            if extra:
                print(f"    Form 4 の取引: {extra['insider']['trades']}")
            print("    " + body[:400].replace("\n", "\n    "))

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="取得と解釈だけ試す")
    args = parser.parse_args()

    if args.inspect:
        sys.exit(inspect())

    import firebase_admin
    from anthropic import Anthropic
    from firebase_admin import credentials, firestore

    service_account = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(service_account))

    main(firestore.client(), Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip()))
