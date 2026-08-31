#!/usr/bin/env python3
"""AI・半導体セクターのニュースを集めて要約し、Firestoreに1日1件置く。

    python3 scripts/sector_digest.py --inspect   # 収集と前処理だけ（Firestore・Claudeに触れない）
    DRY_RUN=1 python3 scripts/sector_digest.py   # 要約まで走らせて、書き込まない
    python3 scripts/sector_digest.py             # 収集→選別→要約→翻訳→Firestoreへ書く

アプリ側は `SectorDigest.swift` / `SectorNewsView.swift` が `sector_digest/{YYYY-MM-DD}`
を読み、「ニュース」タブに出す。**銘柄に紐付かない唯一の面**で、ウォッチリストが
0件の利用者にも同じものが出る。

**同じ結果を `ai_feed/{sha1}` にも1件1ドキュメントで書く**（設計メモ `ai-feed-spec.md`）。
まとめの面が「その日の8件」を日付で束ねて読ませるのに対し、AIフィードは
**利用者が `topics.json` の20タグから選んだトピックだけ**を流す面で、束ね方が違う。
収集・選別・要約は**同じ1回ぶんを使い回す**ので、APIの呼び出しは1つも増えていない
（タグ付けは選別と同じコールで取る）。

**全利用者で同じ1ドキュメントを読む。** 内容が利用者ごとに変わらないので、APIの費用が
利用者数に比例しない。**無料で出せる根拠はここ。** 「利用者ごとにパーソナライズ」を
足した瞬間に費用が線形に増えるので、当面やらない。

**集めるのは英語の記事だけ。** 入力の言語を揃えないと選別も要約も安定しない。
判定は `is_english`（見出し・本文・配信元を見る）。要約は日本語・英語・繁體中文・
韓国語の4言語ぶん作るので、**入力が英語であることと出力の言語は別の話。**

**通知は送らない。** Firestoreへ書くところまで。

---

## 費用を抑える作り（設計メモ ai-semi-news-spec.md のR1〜R9）

- **R1 1件ずつ叩かない。** 選別は40件を1コール、要約は掲載ぶんを1コールに畳む。
  1件1コールにすると、システムプロンプトを件数ぶん払うことになる
- **R2 選別と要約でモデルを分ける。** 載せる/載せないの判断はHaikuで足りる
- **R3 多言語は「翻訳」であって「再要約」ではない。** 元記事をもう一度読ませない。
  出来上がった短い要約（1件250トークン程度）を入力にする
- **R6 全文を投げない。** タイトル＋RSSのdescription（300字まで）で足りる
- **R7 同じ記事を二度要約しない。** URLのSHA-256を `processed_urls` に残す
- **R9 max_tokens を必ず指定する。** 出力課金は入力の5倍高い

### 設計メモから変えたところ

**1. 要約は日本語と英語を同時に書かせ、翻訳は英語から行う。**
メモは「日本語要約 → 各言語へ翻訳」だったが、それだと英語で使っている利用者に
`日本語 → 英語` の往復を通した文が出る。元記事が英語なのに一度日本語を経由するのは
情報が痩せるだけなので、Sonnetの1コールで ja と en を同時に書かせる（入力は1回ぶん、
増えるのは出力だけ）。繁體中文・韓国語はその英語から訳す。

**`en` はアプリの動作にも要る。** アプリは訳の欠けた言語を**英語に倒す**作りで、
日本語には倒さない（韓国語の利用者に日本語が出るのは事故に見えるため）。
`en` が無い記事は**行ごと落ちる**。

**2. Batch API を使っていない。** メモのR4（50%オフ）は、前夜に投入して朝に取り出す
2ジョブ構成と、その間の状態の持ち回りが要る。いまの規模だと月$2ほどの節約のために
運用が1つ増えるので、**費用が実際に問題になってから**にする。切り替えるときは
`summarize` / `translate` の呼び出しを `client.messages.batches.create` に寄せる。

**3. 動画（`videos`）は入れていない。** メモのP5。YouTube Data APIの鍵と字幕の取得が
別に要る。アプリ側は `videos` を読める状態にしてあるので、後から足せる。

---

## 実測で分かったこと（2026-08-30）

**IRのRSSは会社によって出していない・弾かれる。** 実際に叩いて確かめた結果:

    200 NVIDIA    https://nvidianews.nvidia.com/releases.xml      20件
    200 AMD       https://ir.amd.com/rss/news-releases.xml        10件
    200 SK hynix  https://news.skhynix.com/feed/                  10件
    200 Intel     https://newsroom.intel.com/feed                 10件
    403 TSMC      www.tsmc.com / pr.tsmc.com / investor.tsmc.com  （botを弾く）
    404 ASML      www.asml.com/rss/... /en/news/press-releases/rss
    403 Micron    investors.micron.com/rss/news-releases.xml
    000 Broadcom  investors.broadcom.com/rss/news-releases.xml    （接続できず）

**取れない会社はGoogleニュースRSS側で拾う。** TSMC・ASML・Broadcomは
`QUERIES` に企業名で入れてある。**IRが取れないことをバグと診断しないこと。**

**Googleニュースの `<title>` は末尾に ` - 配信元` が付く。** そのまま要約に渡すと
見出しに配信元が混ざるので落とす。

**Hacker Newsは `points > 100` でも広告記事が混ざる。** Algoliaは本文を見ないので、
最終的な取捨は選別（Haiku）に任せる。

**Hacker Newsは30時間・100点では0件で返る。** 実測（2026-08-30）で5つの問い合わせ
すべてが0件だった。HNの点数は投稿から丸一日かけて伸びるので、**投稿時刻で30時間に
切ると、いま100点を超えたばかりの記事が入らない**。72時間・50点にしてある。
重複はURLで落ちるので、窓を広げても同じ記事を二度要約することはない。

## 素直に新しい順で40件取ると、中身が「株価予想」で埋まる

実測（2026-08-30）で上位に並んだのは次のようなものだった。

    TSMC Stock Price Prediction: Can TSM Hit N...   （TradingKey）
    Why I'm Still Holding Nvidia (NVDA) Despite Its Sky-High P/E Ratio   （Motley Fool）
    Kevin Durant Is About To Make More Money From Nvidia Than His NBA Salary

さらに**同じ発表が5〜8件に増殖する**（Sony/WarnerのAnthropic提訴が、配信元違いで
8件並んだ）。増殖したぶんが枠を食うので、選別に渡る40件の中身が薄くなる。
**IRの50件も1件も残らなかった**が、こちらは理由が違う。押し出されたのではなく、
**いちばん新しい発表が39.8時間前**で、30時間の窓の外だった。IRの発表は数日おきなので、
ニュースと同じ窓で切ると常に0件になる（`LOOKBACK_BY_ORIGIN` で96時間にしてある）。

そこで、LLMに渡す前に**機械的な点数**を付けて並べ替える（`score`）。

- IRのフィード ＋100 … 会社が自分で出した一次情報。設計メモが優先すると決めたもの
- 通信社・主要紙 ＋50 … `news_sources.DEFAULT_ALLOWED` に当たるもの。
  **`is_undecidable`（ラテン文字を含まない配信元）は0点にする。** あれは
  「判断できないので通す」ための判定で、点数に使うと現地語の媒体が全部主要紙と
  同じ点になる（実測でスポーツ紙の芸能記事が上位に来た）
- `news_sources.KNOWN_EXCLUDED`（Motley Fool・Zacks・Benzinga等）は**落とす**
- 「株価予想」「買うべきか」系の見出し（`JUNK_TITLE`）は**落とす**
- 見出しが似ているものは1つに畳む（`dedupe_similar`・語の重なりで見る）

**ホワイトリストで「通す」判定はしない。** `news_sources` は前方一致で数えるだけの
表なので、これだけで絞ると表に無い良い記事（The Information、SemiAnalysis など）が
消える。**点数を上げるのに使い、落とすのは除外リストと見出しの型だけ**にしてある。
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

# 同じ `scripts/` にある表を使い回す。**通すかどうかの判定には使わず、点数に使う**
# （下のdocstringの「素直に新しい順で40件取ると」を参照）
import news_sources

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

USER_AGENT = "StockWatchAI/2.3 (shuichi@tinkermode.com)"
TIMEOUT = 30
# 外部への連続アクセスの間隔。相手を選ばず一律で置く
MIN_INTERVAL = 0.3

UTC = timezone.utc
# **ドキュメントIDは日本時間の日付。** 朝の配信（5:30 JST）の直前に走るので、
# UTCの日付で書くと前日ぶんとして積まれる
JST = timezone(timedelta(hours=9))

COLLECTION = "sector_digest"
PROCESSED_COLLECTION = "processed_urls"

# ドキュメントの保持期間。**アプリが読むのは最新3日ぶん**（`SectorDigest.dayCount`）
# だが、遡って読めるように少し長く持つ。
# **FirestoreのTTLポリシーはSparkプランで使えない**ので、ここで消す（`edgar.py` と同じ）
RETENTION_DAYS = 30
# 処理済みURLの保持期間。設計メモの通り90日
PROCESSED_RETENTION_DAYS = 90
PURGE_LIMIT = 300
BATCH_SIZE = 400

# 何時間ぶんを見るか。cronが遅れることを前提に、1日ぶんより広く取る
# （GitHub Actionsのスケジュールは実測で4時間以上遅れたことがある）
LOOKBACK_HOURS = 30

# **取得元ごとに窓を変える。** ニュースは毎時出るが、IRの発表は数日おきで、
# HNの点数は投稿から丸一日かけて伸びる。同じ30時間で切ると、この2つは
# ほぼ常に0件になる（実測はdocstringにある）。
#
# **窓を広げても同じ記事を二度要約しない。** `processed_urls` で落ちる
LOOKBACK_BY_ORIGIN = {
    "ir": 96,
    "practice": 96,   # 個人・企業のブログは毎日は出ない
    "hacker_news": 72,
}

# 選別に渡す上限。ここまでは機械的に落とす（LLMを使わない）
MAX_CANDIDATES = 40
# 実際に載せる件数。業界の動きと使い方の両方を入れるので、企業名で引いていた頃より広げてある
PICK_COUNT = 8

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# フィードだけを更新する実行（`--feed-only`）
#
# **まとめは1日1回のままにする。** `sector_digest/{YYYY-MM-DD}` は「その日の8件」を
# 読ませる面で、アプリは1日ぶん12件までしか出さない（`SectorDigest.newsLimitPerDay`）。
# ここに2時間おきで足すと、**朝の8件が枠を埋めたまま夕方の記事が一生出ない**
# （`write_digest` は後ろに足すので、古いものから12件になる）。
#
# AIフィードは `publishedAt` の降順で引く別の面なので、足したぶんがそのまま前に出る。
# **`--feed-only` は `ai_feed` にだけ書き、まとめには触らない。**
#
# 1回あたり4件に絞ってある。実測で1回 $0.0385（8件・朝の満杯の状態）なので、
# 4件なら $0.02 前後。2時間おき11回で1日 $0.2、月 $6 ほど。
# **`processed_urls` があるので同じ記事を二度要約しない**ぶん、実際はこれより下がる。
FEED_PICK_COUNT = 4

# 対象ティッカー。**記事に紐付ける表示用**で、取得の絞り込みには使わない
TICKERS = {
    "NVIDIA": "NVDA",
    "AMD": "AMD",
    "Advanced Micro Devices": "AMD",
    "Broadcom": "AVGO",
    "TSMC": "TSM",
    "Taiwan Semiconductor": "TSM",
    "ASML": "ASML",
    "Micron": "MU",
    "Intel": "INTC",
    "Arm": "ARM",
    "Supermicro": "SMCI",
    "Super Micro": "SMCI",
    "Marvell": "MRVL",
    "KLA": "KLAC",
    "Lam Research": "LRCX",
    "Applied Materials": "AMAT",
    "SK hynix": "000660.KS",
    "Samsung Electronics": "005930.KS",
}

# GoogleニュースRSSに投げる問い合わせ。**英語で引く。**
# 要約は4言語ぶん作るので、入力は1つの言語に揃えた方が選別も要約も安定する。
# AI・半導体の一次情報は英語の面がいちばん厚い。
#
# **企業名では引かない。** 以前は NVIDIA / TSMC / OpenAI … と企業名を並べていたが、
# それだと「どの会社の話か」でしか記事が集まらず、業界全体の動きや使い方の記事が
# 入ってこない。**話題で引く。** 主要企業の発表は業界のクエリと下のIRで拾える。
#
# 引用符は付けない。実測（2026-08-30）でフレーズ一致にすると
# `AI model release` `chip export controls` `AI productivity tools` が
# **そろって0件**になった（その語順で書かれた見出しが無い）。
#
# 各クエリの直近30時間の収穫（実測）はコメントに残してある。0件が続くようなら
# `--inspect` で確かめてから差し替える
QUERIES = [
    # ── 業界の動き ──
    "AI industry",              # 19
    "AI data center",           # 33
    "AI infrastructure",        # 22
    "AI chips",                 # 20
    "semiconductor industry",   #  5
    "chip manufacturing",       #  4
    "AI model release",         #  2
    "AI regulation",            #  2
    "chip export controls",     #  2
    "AI funding round",         #  2
    "HBM memory",               #  1
    # ── AIの使い方 ──
    "AI agents enterprise",     # 12
    "AI workflow",              #  6
    "AI coding assistant",      #  3
    "enterprise AI adoption",   #  3
    "AI productivity tools",    #  1
]

GOOGLE_NEWS = "https://news.google.com/rss/search"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 現地語のニュース（日本・韓国・台湾／香港）
#
# **上の `QUERIES` は `hl=en-US` で引いている。** つまり非英語の記事は「弾いている」
# のではなく**最初から取りに行っていない**（実測 2026-08-31 で、3,761件のうち
# 「英語以外」として落ちたのは25件だけだった）。現地の記事が欲しければ、
# ロケールを変えて別に引くしかない。
#
# **英語の面が薄いところを埋めるために入れる。** Rapidus・キオクシア・SKハイニックス・
# 聯電あたりの現地の一次情報は、英語媒体だとほとんど流れてこない。
#
# ⚠️ **現地語の記事は `ai_feed` にだけ入れ、`sector_digest`（まとめ）には入れない。**
# まとめの面には出し分けの仕組みが無く、**既に配信済みの2.2を使っている人は
# アプリを更新しても絞れない**（韓国の国内話題が日本語に訳されて並ぶ）。
# フィードは記事ごとに `lang` を持ち、アプリが読む言語で絞る。
#
# **クエリは半導体・AIの話題に寄せてある。** 現地の株式市場の話題まで広げると
# 「株価予想」系が増え、言語ごとに除外の見出しパターンを作る話になる。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LOCALES = {
    # キーは `topics.json` / アプリの `contentLanguageKeys` と同じ綴りにする
    "ja": {
        "label": "日本",
        "params": {"hl": "ja", "gl": "JP", "ceid": "JP:ja"},
        "queries": [
            "AI 半導体", "半導体 工場", "半導体 装置",
            "生成AI 企業", "AIエージェント 業務", "データセンター 電力",
        ],
    },
    "ko": {
        "label": "韓国",
        "params": {"hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        "queries": [
            "AI 반도체", "반도체 공장", "HBM 메모리",
            "생성형 AI 기업", "AI 에이전트 도입", "데이터센터 전력",
        ],
    },
    "zh_Hant": {
        "label": "台湾・香港",
        "params": {"hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
        "queries": [
            "AI 晶片", "半導體 製程", "先進封裝",
            "生成式 AI 企業", "AI 代理 應用", "資料中心 電力",
        ],
    },
}

# 英語由来の記事に入れる言語。**`lang` が無い古いドキュメントもこれとして扱う**
LANG_GLOBAL = "en"

# 現地語のキーワード。**ラテン文字の `KEYWORDS` は使えない。**
# 語の境界（`(?<![a-z0-9])`）で照合しているが、日本語・中国語には語の境界が無い。
# ここは素直な部分一致で見る（現地語の側は、そもそもクエリで絞り込んである）
LOCAL_KEYWORDS = {
    "ja": [
        "半導体", "チップ", "ウエハ", "ウェハ", "露光", "ファウンドリ", "パッケージ",
        "メモリ", "データセンター", "人工知能", "生成AI", "エージェント", "推論",
        "大規模言語モデル", "ラピダス", "キオクシア", "東京エレクトロン", "ソシオネクスト",
        "エヌビディア", "サムスン", "ハイニックス", "台積電", "アドバンテスト",
    ],
    "ko": [
        "반도체", "칩", "웨이퍼", "노광", "파운드리", "패키징", "메모리", "데이터센터",
        "인공지능", "생성형", "에이전트", "추론", "거대언어모델", "하이닉스", "삼성전자",
        "엔비디아", "마이크론", "소부장",
    ],
    "zh_Hant": [
        "半導體", "晶片", "晶圓", "微影", "代工", "封裝", "記憶體", "資料中心",
        "人工智慧", "生成式", "代理", "推論", "大型語言模型", "台積電", "聯電",
        "輝達", "三星", "美光", "先進製程",
    ],
}

# 現地の主要紙。**英語の `news_sources.DEFAULT_ALLOWED` と同じ役割**で、
# 落とすためではなく点数を上げるために使う（表に無い良い媒体を消さない）
LOCAL_MAJOR = {
    "ja": [
        "日本経済新聞", "日経", "朝日新聞", "読売新聞", "毎日新聞", "産経新聞",
        "時事通信", "共同通信", "NHK", "東洋経済", "ダイヤモンド", "日刊工業新聞",
        "ITmedia", "EE Times", "マイナビ", "Impress", "PC Watch", "TECH+",
    ],
    "ko": [
        "한국경제", "매일경제", "조선일보", "중앙일보", "동아일보", "연합뉴스",
        "전자신문", "디지털타임스", "서울경제", "이데일리", "ZDNet",
    ],
    "zh_Hant": [
        "經濟日報", "工商時報", "中央社", "聯合報", "自由時報", "天下雜誌",
        "數位時代", "科技新報", "DIGITIMES", "鉅亨網", "財訊", "商業周刊",
    ],
}

# 現地語の「株価予想」系。**英語の `JUNK_TITLE` と同じ役割。**
# クエリを半導体・AIに寄せてあるので数は少ないが、素通りさせると
# 助言に見える見出しがフィードに並ぶ
LOCAL_JUNK = {
    "ja": ["株価予想", "買うべき", "狙い目", "急騰", "爆上げ", "おすすめ銘柄",
           "注目銘柄", "儲か", "億り人", "テンバガー"],
    "ko": ["주가 전망", "주가전망", "사야", "급등", "추천주", "유망주", "대박"],
    "zh_Hant": ["股價預測", "該買", "飆漲", "推薦股", "明牌", "抱緊", "賺翻"],
}

# 各社のIR。**実測で通ったものだけ入れてある**（上のdocstringに全部の結果がある）。
# 落ちても他の取得は続ける作りなので、増やすときは `--inspect` で件数を見てから
IR_FEEDS = {
    "NVIDIA": "https://nvidianews.nvidia.com/releases.xml",
    "AMD": "https://ir.amd.com/rss/news-releases.xml",
    "SK hynix": "https://news.skhynix.com/feed/",
    "Intel": "https://newsroom.intel.com/feed",
    # AIの作り手側の発表。半導体のIRと同じ「会社が自分で出した一次情報」
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google AI": "https://blog.google/technology/ai/rss/",
}

# **AIの使い方の記事。**
#
# GoogleニュースRSSはここが弱い。実測（2026-08-30）で候補227件のうち、実務・使い方
# らしい見出しは**8件**しかなく、そのうち選別に渡る40件に届いたのは**2件**だった。
# 「使い方も入れる」と決めた以上、取りに行く先を用意しないと成立しない。
#
# 実際に叩いて通ったものだけ入れてある（`--inspect` で件数を見てから増やすこと）。
PRACTICE_FEEDS = {
    "Simon Willison": "https://simonwillison.net/atom/everything/",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "Stack Overflow": "https://stackoverflow.blog/feed/",
    "Meta Engineering": "https://engineering.fb.com/feed/",
}

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
# **設計メモの `points > 100` は30時間の窓だと0件になる**（上のdocstringの実測）。
# HNの点数は投稿から丸一日かけて伸びるので、窓を広げて閾値を下げてある
HN_MIN_POINTS = 50
# **短い語で引くと関連度が落ちる。** Algoliaの `search_by_date` は日付順なので、
# `RAG` `GPU` のような短い語だと無関係な記事が上位に来る（実測）。
# 最後は `has_keyword` と選別で落ちるが、候補の枠を食うので語を具体的にしてある
HN_QUERIES = [
    "AI agents", "LLM inference", "AI chip", "semiconductor",
    "open source model", "AI coding",
]

# 見出しに1つも入っていなければ落とす。**Hacker Newsのために置いてある**
# （Googleニュース側は企業名で引いているので、たいてい素通りする）
KEYWORDS = [
    # 業界
    "ai", "artificial intelligence", "llm", "model", "gpu", "chip", "semiconductor",
    "silicon", "wafer", "foundry", "hbm", "memory", "datacenter", "data center",
    "inference", "training", "nvidia", "tsmc", "asml", "amd", "intel", "broadcom",
    "micron", "samsung", "hynix", "openai", "anthropic", "deepmind", "xai", "arm",
    "accelerator", "cuda", "transformer", "neural", "fab", "lithography",
    # 使い方。**これが無いと使い方の記事が落ちる**
    # （`How we run coding agents in production` が実際に落ちた）
    "agent", "agentic", "copilot", "assistant", "chatbot", "coding", "workflow",
    "prompt", "fine-tuning", "finetuning", "embedding", "rag", "context window",
    "automation", "multimodal", "reasoning", "benchmark", "open source",
]

# 区分。**アプリの `SectorCategory` と文字列を揃える。**
# アプリは知らない値を `.other` に倒すので、ここを増やしてもアプリは壊れない
CATEGORIES = ["chip", "model", "funding", "policy", "product", "howto"]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIフィードのトピック語彙（`topics.json`）
#
# `sector_digest/{YYYY-MM-DD}` は「その日の8件」を日付で束ねた面だが、AIフィードは
# **利用者が選んだトピックだけを流す面**なので、記事1件＝1ドキュメントで
# `ai_feed/{sha1}` にも書く。収集・選別・要約は上と**同じ1回ぶんを使い回す**。
#
# **収集を利用者ごとに回さない。** 出し分けはアプリ側でやる。ここを崩すと
# APIの費用が利用者数に比例する（`sector_digest` を無料で出せている前提そのもの）。
#
# **タグはこの表のIDしか使わない。** LLMに自由生成させると表記ゆれで一致しなくなる。
# 表はアプリ側（`Test Stock20260208/topics.json`）にも同じ中身の写しがある。
# **片方だけ変えない**（IDが食い違うとアプリでラベルが引けず、そのタグだけ消える）。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOPICS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics.json")

with open(TOPICS_PATH, encoding="utf-8") as _f:
    TOPICS = json.load(_f)["topics"]

TOPIC_IDS = [t["id"] for t in TOPICS]
# 選別のプロンプトに入れる説明。**IDと1行の説明だけ**（ラベルは4言語ぶんあって長い）
TOPIC_HINTS = "\n".join(f"  {t['id']:16} {t['hint']}" for t in TOPICS)

AI_FEED_COLLECTION = "ai_feed"
# 1件に付けるタグの上限。**埋めさせない**（薄いタグを足すと絞り込みの精度が落ちる）
MAX_TAGS = 3
# 設計メモ §3。`publishedAt` が30日より古いものを消す。
# **FirestoreのTTLポリシーはSparkプランで使えない**ので、`expiresAt` を自分で入れて
# `purge_expired` に片付けさせる（`sector_digest` / `news` と同じ流儀）
AI_FEED_RETENTION_DAYS = 30

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 言語
#
# **キーはアプリの `SectorDigest.languageKeys` が引く名前と揃える。**
# 片方だけ変えると、その言語の利用者に英語が出続ける（壊れないので気付きにくい）。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Sonnetが直接書く言語。**元記事が英語なので、英語は訳さずに書かせる**
WRITTEN = ("ja", "en")
# Haikuが英語から訳す言語
TRANSLATED = {
    "zh_Hant": "Traditional Chinese (Taiwan usage)",
    "ko": "Korean",
}

MODEL_SELECT = "claude-haiku-4-5"
MODEL_SUMMARY = "claude-sonnet-5"
MODEL_TRANSLATE = "claude-haiku-4-5"

# 出力課金は入力の5倍高い。**上限を切らないと無駄に長い出力が返る**
MAX_TOKENS_SELECT = 2000
MAX_TOKENS_SUMMARY = 8000
# 繁體中文・韓国語は同じ内容でも日本語よりトークンが増えやすいので広めに取る
MAX_TOKENS_TRANSLATE = 6000

# 実行の終わりに概算を出すためだけの表（$ / 100万トークン）。
# **課金の正はコンソール側。** ここは桁を見誤らないための目安
PRICING = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 取得
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_last_request = 0.0


def fetch(url, params=None, retries=2, headers=None):
    """**落ちても例外を投げない呼び出し元がいる。** ここは投げる側で、
    呼び出し元が取得元ごとに握りつぶす（1つの取得元が落ちても他は続ける）"""
    global _last_request
    last_error = None
    for attempt in range(retries + 1):
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            res = requests.get(
                url,
                params=params,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
            )
            if res.status_code == 200:
                return res
            last_error = f"HTTP {res.status_code}"
            # 4xxは待っても変わらない。5xxと429だけ間を置いて試し直す
            if res.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{url}: {last_error}")


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def to_text(markup, limit=300):
    """RSSのdescriptionはHTMLで入っている。**全文は投げない**ので短く落とす（R6）"""
    if not markup:
        return ""
    text = _TAG.sub(" ", html.unescape(markup))
    text = _SPACE.sub(" ", text).strip()
    return text[:limit]


def _local(tag):
    """Atomは名前空間付きで返る。`{http://www.w3.org/2005/Atom}entry` → `entry`"""
    return tag.rsplit("}", 1)[-1]


def _find(node, name):
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node, name):
    child = _find(node, name)
    if child is None:
        return ""
    if child.text:
        return child.text.strip()
    return ""


def parse_date(value):
    """RFC822（RSS）とISO8601（Atom）の両方を受ける。読めなければNone"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_feed(xml, default_source=""):
    """RSSでもAtomでも同じ形に畳む。**読めない項目は落として続ける**"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise RuntimeError(f"XMLを読めなかった: {e}")

    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue

        title = _text(node, "title")
        link = _text(node, "link")
        if not link:
            # Atomは <link href="..."/>
            for child in node:
                if _local(child.tag) == "link" and child.get("href"):
                    link = child.get("href")
                    break
        if not title or not link:
            continue

        published = parse_date(
            _text(node, "pubDate") or _text(node, "published") or _text(node, "updated")
        )

        source = default_source
        source_node = _find(node, "source")
        if source_node is not None and (source_node.text or "").strip():
            source = source_node.text.strip()

        items.append({
            "title": html.unescape(title).strip(),
            "url": link.strip(),
            "source": source,
            "description": to_text(_text(node, "description") or _text(node, "summary")),
            "published_at": published,
        })
    return items


# GoogleニュースのタイトルはRSS側で ` - 配信元` が付く。要約に配信元を混ぜない
_GOOGLE_SUFFIX = re.compile(r"\s+-\s+[^-]{2,40}$")


def collect_google_news():
    items = []
    for query in QUERIES:
        # **フレーズ一致にしない**（`QUERIES` のコメントを参照）。
        # 引用符が要るクエリは `QUERIES` 側に書く
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        try:
            res = fetch(GOOGLE_NEWS, params=params)
            picked = parse_feed(res.content)
        except RuntimeError as e:
            print(f"  Googleニュース「{query}」: 取得に失敗したので飛ばす {e}")
            continue
        for item in picked:
            item["title"] = _GOOGLE_SUFFIX.sub("", item["title"]).strip()
            item["origin"] = "google_news"
            item["query"] = query
        print(f"  Googleニュース「{query}」: {len(picked)} 件")
        items.extend(picked)
    return items


def collect_feeds(feeds, origin, label, suffix=""):
    """RSS/Atomのフィードをまとめて引く。**1つ落ちても他は続ける**"""
    items = []
    for name, url in feeds.items():
        source = f"{name}{suffix}"
        try:
            res = fetch(url)
            picked = parse_feed(res.content, default_source=source)
        except RuntimeError as e:
            # **出していない・botを弾くフィードがある。** 落ちても続ける
            print(f"  {label} {name}: 取得に失敗したので飛ばす {e}")
            continue
        for item in picked:
            item["source"] = source
            item["origin"] = origin
        print(f"  {label} {name}: {len(picked)} 件")
        items.extend(picked)
    return items


def collect_google_news_local():
    """現地語のGoogleニュース。**ロケールごとに `hl` / `gl` / `ceid` を変えて引く。**

    `origin` は `google_news_local`、`lang` にロケールのキーを入れる。
    以降の前処理は `lang` を見て、英語前提の判定を飛ばす。
    """
    items = []
    for lang, locale in LOCALES.items():
        for query in locale["queries"]:
            params = dict(locale["params"], q=query)
            try:
                res = fetch(GOOGLE_NEWS, params=params)
                picked = parse_feed(res.content)
            except RuntimeError as e:
                print(f"  {locale['label']}「{query}」: 取得に失敗したので飛ばす {e}")
                continue
            for item in picked:
                item["title"] = _GOOGLE_SUFFIX.sub("", item["title"]).strip()
                item["origin"] = "google_news_local"
                item["lang"] = lang
                item["query"] = query
            print(f"  {locale['label']}「{query}」: {len(picked)} 件")
            items.extend(picked)
    return items


def collect_ir():
    return collect_feeds(IR_FEEDS, "ir", "IR", suffix=" IR")


def collect_practice():
    return collect_feeds(PRACTICE_FEEDS, "practice", "実務")


def collect_hacker_news(_since):
    # **HNだけ窓が広い。** 点数が伸びるのを待つ必要がある（`LOOKBACK_BY_ORIGIN`）
    since = datetime.now(UTC) - timedelta(hours=LOOKBACK_BY_ORIGIN["hacker_news"])
    items = []
    for query in HN_QUERIES:
        params = {
            "tags": "story",
            "query": query,
            "hitsPerPage": 20,
            "numericFilters": f"points>{HN_MIN_POINTS},created_at_i>{int(since.timestamp())}",
        }
        try:
            res = fetch(HN_SEARCH, params=params)
            hits = res.json().get("hits", [])
        except (RuntimeError, ValueError) as e:
            print(f"  Hacker News「{query}」: 取得に失敗したので飛ばす {e}")
            continue
        picked = 0
        for hit in hits:
            url = hit.get("url")
            title = (hit.get("title") or "").strip()
            # 本文だけの投稿（urlが無い）は元記事が無いので出さない
            if not url or not title:
                continue
            items.append({
                "title": title,
                "url": url,
                "source": "Hacker News",
                "description": "",
                "published_at": parse_date(hit.get("created_at")),
                "origin": "hacker_news",
                "points": hit.get("points"),
            })
            picked += 1
        print(f"  Hacker News「{query}」: {picked} 件")
    return items


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前処理（LLMを使わない）
#
# **ここで機械的に落とすのが最大の費用対策。** 150件前後を40件まで落としてから
# 初めてLLMに渡す。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_NORMALIZE = re.compile(r"[^a-z0-9]+")

# 見出しの型で落とすもの。**配信元では捕まえきれない**（まともな媒体も
# 「買うべきか」記事を出す）。設計メモの「意見の再生産を入れない」に対応する
JUNK_TITLE = re.compile(
    r"stock price prediction|price prediction|should you buy|should i buy"
    r"|reasons to buy|reasons to sell|is a buy|is it too late to buy"
    r"|p/e ratio|price target|best stocks|top \d+ stocks|stocks to buy"
    r"|here'?s why|why i'?m |prediction:|forecast:|could make you a millionaire"
    r"|billionaires are|wall street'?s .* price|analyst[s]? (say|see|raise|cut)"
    r"|better buy|vs\.? .* which|what to expect from .* stock"
    r"|top pick for|clear winner|charts point",
    re.I,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 英語の記事だけを通す
#
# **要約に入れる記事は英語だけにする。** GoogleニュースRSSは `hl=en-US` で引いても
# 現地語の媒体を混ぜて返す。入力の言語が揃っていないと、選別も要約も安定しない。
#
# 実測（2026-08-30・直近30時間の210件）:
#
#     見出しが非ラテン文字      0 件
#     本文が非ラテン文字        0 件
#     配信元だけが非ラテン文字  3 件  （매일경제 / 스포츠조선 / تسنیم）
#
# つまり `hl=en-US` の時点でほぼ英語になっていて、残るのは
# **「英語版を出している現地の媒体」**だけだった。これは落とす。英語で書かれてはいるが
# 自国向けの記事の機械翻訳で、AI・半導体の面に足すものが無い
# （実測で釣れたのはスポーツ紙の芸能記事だった）。
#
# **英語の機能語で判定しない。** ラテン文字の非英語（スペイン語・ポルトガル語・
# ドイツ語など）を捕まえられないか試したが、**実測で該当0件・誤検知12件**だった。
# 見出しは "Judge blocks Anthropic blacklisting" のように機能語を持たない書き方が
# 普通にあるので、この方向の判定は入れない。
#
# ⚠️ **アプリ側の `NewsSourceFilter` とは判断が逆になる。** あちらは
# 「ラテン文字を含まない配信元は判断できないので通す」（通さないと日本・韓国・台湾の
# 面が空になる）。ここは英語の記事だけを集める面なので、同じ配信元を落とす。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NON_LATIN = re.compile(
    "[\u0370-\u03ff"   # ギリシャ
    "\u0400-\u04ff"    # キリル
    "\u0590-\u05ff"    # ヘブライ
    "\u0600-\u06ff"    # アラビア
    "\u0900-\u097f"    # デーヴァナーガリー
    "\u0980-\u09ff"    # ベンガル
    "\u0e00-\u0e7f"    # タイ
    "\u3040-\u30ff"    # ひらがな・カタカナ
    "\u3400-\u4dbf"    # 漢字（拡張A）
    "\u4e00-\u9fff"    # 漢字
    "\uac00-\ud7af]"   # ハングル
)

# 1文字混ざっただけで落とさない（`2μm` のような書き方があるため）。
# 割合で見るので、見出し全体が現地語のときだけ当たる
NON_LATIN_LIMIT = 0.15


def non_latin_ratio(text):
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if NON_LATIN.match(c)) / len(letters)


def is_local(item):
    """現地語のロケールから取ってきた記事か（`LOCALES` 由来）"""
    return item.get("lang") in LOCALES


def is_english(item):
    """英語の記事か。**見出し・本文・配信元の3か所を見る。**

    配信元まで見るのは、実測で残った3件が
    **「見出しも本文も英語だが、媒体が現地語」**という形だったため。

    **現地語のロケールから取ってきたものには掛けない。** あちらは非英語であることが
    分かったうえで取りに行っているので、ここで落とすと収集した意味が無くなる。
    """
    if is_local(item):
        return True
    if non_latin_ratio(item["title"]) > NON_LATIN_LIMIT:
        return False
    if non_latin_ratio(item.get("description")) > NON_LATIN_LIMIT:
        return False
    if non_latin_ratio(item.get("source")) > NON_LATIN_LIMIT:
        return False
    return True


# 点数。**通すかどうかではなく、40件に絞るときの並び順を決める**
SCORE_IR = 100
# 実務のフィードは「使い方」の担い手。IRより下、通信社より上に置く
SCORE_PRACTICE = 80
SCORE_MAJOR = 50
SCORE_HN = 20
# 現地の主要紙（`LOCAL_MAJOR`）。英語の主要紙と同じ重みにする
SCORE_LOCAL_MAJOR = 50
# 表に無い現地の媒体。**0にしない。** 表は網羅していないので、0にすると
# 良い記事が枠の取り合いで必ず負ける（英語側でホワイトリストを使わない理由と同じ）
SCORE_LOCAL = 30

# 一次情報のために必ず空けておく枠。点数が低くても、IRは最低これだけ選別に回す。
# **これが無いと、Googleニュースの物量にIRが常に負ける**（実測で50件すべて落ちた）
IR_RESERVED = 5

# 使い方の記事のために空けておく枠。**これが無いと業界ニュースの物量に負ける**
# （実測で、使い方らしい候補8件のうち40件に届いたのは2件だった）
HOWTO_RESERVED = 8

# 現地語の記事のためにロケールごとに空けておく枠。
# **これが無いと英語の物量に必ず負ける**（英語のクエリ16本 × 100件に対して、
# 現地語は6本ずつしかない）。3言語 × 3件で、40件のうち9件まで。
# 逆に大きくすると、英語の一次情報が押し出される
LOCAL_RESERVED = 3

# 使い方・実務らしい見出し。**点数ではなく枠の確保に使う**
PRACTICAL_TITLE = re.compile(
    r"\bhow (we|i|to)\b|\bwhat we learned\b|\blessons\b|\bin production\b"
    r"|\bguide\b|\bwe built\b|\bcase study\b|\bworkflow|\bplaybook\b|\btutorial\b"
    r"|\bbest practices\b|\bhands-on\b|\bdeep dive\b|\bwhy we\b|\bbuilding\b",
    re.I,
)


def looks_practical(item):
    """使い方の記事か。実務フィードから来たもの、または見出しがその形のもの"""
    return item.get("origin") in ("practice", "hacker_news") or bool(
        PRACTICAL_TITLE.search(item["title"])
    )


def score(item):
    """**LLMに渡す40件を選ぶための機械的な点数。** 中身は見ない（見るのは選別工程）"""
    origin = item.get("origin")
    if origin == "ir":
        return SCORE_IR
    if origin == "practice":
        return SCORE_PRACTICE
    if origin == "hacker_news":
        return SCORE_HN
    source = item.get("source") or ""

    # 現地語の記事は `news_sources` では判断できない（あの表は英語の
    # コンテンツファームを相手にしていて、現地の媒体について何も言えない）。
    # **ロケールごとの主要紙の表で見る**（`LOCAL_MAJOR`）
    if is_local(item):
        majors = LOCAL_MAJOR.get(item["lang"], [])
        return SCORE_LOCAL_MAJOR if any(m in source for m in majors) else SCORE_LOCAL

    # **`is_allowed` をそのまま使わない。** あれはラテン文字を含まない配信元を
    # 「判断できないので通す」で True にする（`news_sources` の設計）。
    # 落とす判定にはそれで正しいが、**点数に使うと現地語の媒体がすべて
    # 主要紙と同じ点になる**。実測でスポーツ紙の芸能記事が上位に来た
    if news_sources.is_undecidable(source):
        return 0
    return SCORE_MAJOR if news_sources.is_allowed(source) else 0


def is_excluded(item):
    """落とすもの。**除外リストと見出しの型だけ**で、ホワイトリスト漏れでは落とさない"""
    if JUNK_TITLE.search(item["title"]):
        return "見出しの型"
    # 現地語の「株価予想」系。**英語の `JUNK_TITLE` は当たらない**
    if is_local(item):
        junk = LOCAL_JUNK.get(item["lang"], [])
        if any(j in item["title"] for j in junk):
            return "見出しの型"
    source = news_sources.normalize(item.get("source"))
    for name in news_sources.KNOWN_EXCLUDED:
        if source and source.startswith(news_sources.normalize(name)):
            return "除外した配信元"
    return None


_WORD = re.compile(r"[a-z0-9]+")
# 見出しの語がこれ以上重なっていたら同じ話とみなす
SIMILAR_THRESHOLD = 0.6
# どの見出しにも出るので、重なりの判定から外す
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "with", "as", "at", "by", "its", "it", "that", "this", "from", "over", "after",
}


# **CJKは空白で語を切れない。** 日本語・中国語の見出しを `_WORD` に掛けると
# ラテン文字部分しか残らず、たいてい空集合になる。空集合どうしの重なりは0なので
# **`dedupe_similar` が一切効かなくなり、同じ発表が5件並ぶ**（英語側で実測した通り）。
# 2文字ずつの並び（バイグラム）で見れば、語を切らずに重なりを測れる。
# ハングルは空白で切れるが、ここでも同じ扱いで問題ない
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")


def words(title):
    tokens = {w for w in _WORD.findall(title.lower())
              if w not in _STOPWORDS and len(w) > 2}
    for run in _CJK.findall(title):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def dedupe_similar(items):
    """**同じ発表が配信元違いで並ぶのを1つに畳む。**

    実測で、1つの提訴のニュースが8件に増殖して枠を食っていた。見出しの文字が
    違うのでURLでも正規化した見出しでも落ちない。語の重なりで見るしかない。

    **点数の高いものを残す。** 呼ぶ前に点数順に並べておくこと。
    """
    kept = []
    kept_words = []
    dropped = 0
    for item in items:
        w = words(item["title"])
        if not w:
            kept.append(item)
            kept_words.append(w)
            continue
        duplicate = False
        for seen in kept_words:
            if not seen:
                continue
            overlap = len(w & seen) / min(len(w), len(seen))
            if overlap >= SIMILAR_THRESHOLD:
                duplicate = True
                break
        if duplicate:
            dropped += 1
            continue
        kept.append(item)
        kept_words.append(w)
    return kept, dropped


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def title_key(title):
    """見出しの正規化。**同じ発表が複数の配信元から来る**ので、URLだけでは重複が残る"""
    return _NORMALIZE.sub("", title.lower())[:80]


# **語の境界で照合する。** 素直に `k in text` で書くと `"ai"` が
# `chair` `maintain` `email` `said` に当たり、**判定がほぼ素通りになる**
# （実測で1357件中1件しか落ちていなかった）。トピックで引くようにして
# 雑音が増えたので、ここが効いていないと候補の枠を食われる
# 末尾の `s?` は複数形のため。`agent` と `agents`、`tool` と `tools` を
# 両方書き並べると表が二倍になり、片方だけ足し忘れる
KEYWORD_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in sorted(KEYWORDS, key=len, reverse=True))
    + r")s?(?![a-z0-9])",
    re.I,
)


def has_keyword(item):
    """見出し・本文に業界の語が1つも無ければ落とす。

    **現地語は `KEYWORD_RE` で照合できない。** あの正規表現は語の境界
    （`(?<![a-z0-9])`）で見ているが、日本語と中国語には語の境界が無い。
    `LOCAL_KEYWORDS` の素直な部分一致で見る（現地語側はクエリで既に絞ってある）。
    """
    text = f"{item['title']} {item.get('description', '')}"
    if is_local(item):
        keywords = LOCAL_KEYWORDS.get(item["lang"], [])
        if any(k in text for k in keywords):
            return True
        # `NVIDIA` `HBM` のようにラテン文字で書かれることも多いので、英語側も見る
        return bool(KEYWORD_RE.search(text))
    return bool(KEYWORD_RE.search(text))


def tickers_for(item):
    """記事に関係する銘柄。**表示用の事実として添えるだけ**で、推奨ではない。
    LLMには訊かない（作り話が混ざるより、当たらない方がまし）"""
    text = f"{item['title']} {item.get('description', '')}"
    found = []
    for name, ticker in TICKERS.items():
        if re.search(rf"\b{re.escape(name)}\b", text, re.I) and ticker not in found:
            found.append(ticker)
    return found[:4]


def preprocess(items, since):
    """LLMに渡す40件を作る。**ここではLLMを一切使わない**（最大の費用対策）。

    順番は「点数 → 新しさ」。**新しさだけで切るとIRが全滅する**（実測で50件すべてが
    Googleニュースに押し出された）ので、一次情報に枠を空けてある。
    """
    now = datetime.now(UTC)
    seen_urls = set()
    seen_titles = set()
    kept = []

    dropped = {"古い": 0, "重複": 0, "英語以外": 0, "キーワード外": 0,
               "見出しの型": 0, "除外した配信元": 0}
    for item in items:
        published = item.get("published_at")
        # **日時が読めなかった記事は残す。** 落とすと、日時を持たないフィードが丸ごと消える
        hours = LOOKBACK_BY_ORIGIN.get(item.get("origin"))
        limit = (now - timedelta(hours=hours)) if hours else since
        if published is not None and published < limit:
            dropped["古い"] += 1
            continue
        if item["url"] in seen_urls or title_key(item["title"]) in seen_titles:
            dropped["重複"] += 1
            continue
        # **英語の記事だけを要約に入れる。** キーワードより先に見る
        # （キーワードはラテン文字で書いてあるので、現地語の記事は素通りする）
        if not is_english(item):
            dropped["英語以外"] += 1
            continue
        if not has_keyword(item):
            dropped["キーワード外"] += 1
            continue
        reason = is_excluded(item)
        if reason:
            dropped[reason] += 1
            continue
        seen_urls.add(item["url"])
        seen_titles.add(title_key(item["title"]))
        item["tickers"] = tickers_for(item)
        item["score"] = score(item)
        kept.append(item)

    kept.sort(
        key=lambda i: (i["score"], i.get("published_at") or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )
    kept, similar = dedupe_similar(kept)
    dropped["似た見出し"] = similar

    # **枠は「最低の確保」ではなく「上限」。**
    #
    # 一次情報（IR）と使い方の記事は数が少ないので、点数と新しさだけで40件を切ると
    # 物量のある業界ニュースに埋め尽くされる。かといって点数を高くするだけだと、
    # 今度は**IRが上位を占めて企業中心の面に戻る**（OpenAIとGoogleを足したら
    # 実測で18/40がIRになった）。どちらにも上限を置いて配分する。
    #
    # **`in` で引かない。** 中身が辞書なので値の比較になり、遅いうえに
    # たまたま同じ内容の行があると両方消える
    picked = []
    taken = set()

    def take(rows, limit):
        for row in rows:
            if limit <= 0:
                break
            if id(row) in taken:
                continue
            picked.append(row)
            taken.add(id(row))
            limit -= 1

    take([i for i in kept if i.get("origin") == "ir"], IR_RESERVED)
    take([i for i in kept if looks_practical(i)], HOWTO_RESERVED)
    # **ロケールごとに別々に取る。** まとめて `LOCAL_RESERVED * 3` にすると、
    # 点数順に並んでいるぶん記事数の多い言語が枠を独占する
    for lang in LOCALES:
        take([i for i in kept if i.get("lang") == lang], LOCAL_RESERVED)
    # 残りは業界のニュースで埋める。**IRと使い方はもう上限まで入っている**ので、
    # ここでは点数の高い順＝通信社・主要紙から入る
    take([i for i in kept
          if i.get("origin") != "ir" and not looks_practical(i) and not is_local(i)],
         MAX_CANDIDATES - len(picked))

    selected = picked[:MAX_CANDIDATES]
    # 選別のプロンプトでは新しい順に並んでいた方が読みやすい
    selected.sort(
        key=lambda i: (i["score"], i.get("published_at") or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )

    print(f"  前処理: {len(items)} 件 → 候補 {len(kept)} 件 → 選別に渡す {len(selected)} 件")
    print(f"          落とした内訳: {dropped}")
    print(f"          うちIR: {sum(1 for i in selected if i.get('origin') == 'ir')} 件 / "
          f"使い方らしいもの: {sum(1 for i in selected if looks_practical(i))} 件 / "
          f"通信社・主要紙: {sum(1 for i in selected if i['score'] == SCORE_MAJOR)} 件")
    local_counts = {lang: sum(1 for i in selected if i.get("lang") == lang) for lang in LOCALES}
    print(f"          現地語: {local_counts}（0が続くならクエリかキーワードを疑う）")
    return selected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 選別・要約・翻訳
#
# **助言業の線を越えさせない。** 月額課金のアプリで個別銘柄の助言をすると
# 投資助言・代理業の登録が要る（金融庁の監督指針 VII-3-1(2)②イ）。
# `edgar.py` と同じ制約をここにも掛ける。**この構造を崩さないこと。**
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NO_ADVICE = """You are describing what happened. You are NOT giving investment advice.

Absolute constraints — the app is a paid subscription, and advice on individual
securities behind a paywall requires an investment-advisory registration:

- Never suggest buying or selling, and never imply it is a good or bad time to do either.
- Never give a price target, rating, score, or recommendation.
- Never predict that a share price will rise or fall.
- Describe what happened. Keep interpretation to the minimum needed to make the facts
  understandable.
- Do not state anything the source does not say. If a number is unclear, leave it out."""

SELECT_SYSTEM = f"""You curate a daily AI-and-semiconductor digest for a stock-watching app.

The digest covers two kinds of item, and a good day has both:

1. **What happened in the industry** — company announcements, official figures, capacity
   and manufacturing news, model releases, funding, regulation and export controls.
   Prefer primary information over opinion pieces, listicles, price-movement recaps, and
   rewrites of other outlets' reporting.

2. **How AI is actually being used** — practical write-ups on putting AI to work: agents
   and coding tools in real workflows, what a team learned adopting it, concrete
   technique. Category `howto`. These are explainers rather than news, and that is fine —
   what disqualifies one is being a thinly-disguised ad, a beginner listicle
   ("10 ChatGPT prompts"), or a piece with no specifics.

**Do not organise the digest around companies.** A reader wants to know where the
industry is going, not to follow particular firms. If several items are about the same
company, keep the strongest one and spend the remaining slots elsewhere.

**Some items are in Japanese, Korean or Traditional Chinese**, marked `[ja local]`,
`[ko local]` or `[zh_Hant local]`. They come from local outlets and are shown only to
readers of that language, so they are judged on their own merit — not against the
English items, and never dropped for being in another language. What earns a local item
a slot is covering something the English press does not: a domestic fab, a supplier, a
government programme, a named company's own announcement. Drop one that merely
re-reports a story already in the English list.

Drop near-duplicates: if several items cover the same announcement, keep only the one
with the most substantial source.

{NO_ADVICE}"""

SUMMARY_SYSTEM = f"""You write short news summaries for an AI-and-semiconductor digest in
a stock-watching app.

Write for a reader who follows the industry but is not an engineer. Lead with what
actually happened and the concrete numbers. No preamble, no "in a recent announcement".

**The source may be in Japanese, Korean or Traditional Chinese, not only English.**
Write the Japanese and English summaries from whatever language the item is in. Do not
translate word for word — write each one as a native speaker would.

{NO_ADVICE}"""

TRANSLATE_SYSTEM = f"""You translate short news summaries for an AI-and-semiconductor
digest in a stock-watching app.

Rules:
- Do NOT translate proper nouns: company names (NVIDIA, TSMC, SK hynix), tickers (NVDA),
  and product names (Blackwell, HBM3E) stay in their original form.
- Do NOT convert numbers, units, currencies or dates. Keep "$" and "%" as they are.
- Translate the meaning, not word for word. It must read as if written by a native
  speaker of the target language, using the vocabulary that retail investors in that
  market actually use.
- Keep roughly the same length as the source.

{NO_ADVICE}"""


def _usage(totals, model, response):
    """使ったトークンを足していく。**終わりに概算を出すため**"""
    usage = getattr(response, "usage", None)
    if not usage:
        return
    entry = totals.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
    entry["input"] += getattr(usage, "input_tokens", 0) or 0
    entry["output"] += getattr(usage, "output_tokens", 0) or 0
    entry["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0


def _call(client, totals, *, model, system, prompt, max_tokens, schema, _retry=True):
    """1コール。**出力の形をスキーマで固定する**（前置きが付いて json.loads が落ちるのを防ぐ）。

    **失敗はNoneに畳む。** 1コールのために実行ごと止めない。

    ⚠️ **思考を切ってある（`thinking: disabled`）。**
    Sonnet 5 は `thinking` を省略すると**アダプティブ思考が既定で動く**。
    思考のぶんも出力トークンとして数えられるので、`max_tokens` を要約の分量で
    見積もっていると**途中で切れてJSONが壊れる**。実測（2026-08-30）で
    出力が6,000（＝上限ぴったり）に張り付き、
    `Unterminated string starting at: line 1 column 1336` で実行ごと落ちた。
    ここは要約と翻訳の整形しかしていないので、思考は要らない。

    `edgar.py` が同じ `max_tokens=6000` で通っているのは Sonnet 4.6 だから。
    **4.6 は省略時に思考しない。モデルを上げるときはここを確かめること。**

    システムプロンプトは毎回同じなので `cache_control` を付けてある。
    ただし**いまの長さ（1000トークン未満）ではキャッシュに載らないことが多い**
    （最小のキャッシュ単位に届かない）。実際に効いているかは実行の終わりに出る
    `キャッシュ読み` で分かる。
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as e:
        print(f"    {model} の呼び出しに失敗: {e}")
        return None

    _usage(totals, model, response)

    # **切れたことを黙って通さない。** 切れた出力は必ずJSONとして壊れるので、
    # 下の except でも捕まるが、原因が「上限に当たった」だと分からない
    truncated = getattr(response, "stop_reason", None) == "max_tokens"
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)

    if not truncated and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"    {model} のJSONを読めなかった: {e}")
    elif truncated:
        print(f"    {model} が max_tokens({max_tokens}) に達して切れた")
    else:
        print(f"    {model} が本文を返さなかった")

    # **1回だけ広げてやり直す。** ここで諦めると、1コールの失敗で
    # その日のまとめが丸ごと出なくなる（実測で実際に起きた）
    if _retry:
        print(f"    上限を {max_tokens * 2} に広げてやり直す")
        return _call(client, totals, model=model, system=system, prompt=prompt,
                     max_tokens=max_tokens * 2, schema=schema, _retry=False)
    return None


SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    # AIフィードの絞り込み用。**選別と同じ1コールで取る**（R1）。
                    # 区分（`category`）とは別物で、こちらは粒度が細かく複数付く。
                    #
                    # ⚠️ **`maxItems` を書かないこと。** structured output のスキーマは
                    # 配列の `maxItems` に対応しておらず、**コールが400で丸ごと落ちる**
                    # （実測 2026-08-31: `For 'array' type, property 'maxItems' is not
                    # supported`）。選別が落ちるとまとめの面ごと中止になるので、
                    # **AIフィードだけでなく既存の配信も止まる。**
                    # 上限はプロンプトで伝え、`select` 側で切る
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": TOPIC_IDS},
                    },
                },
                "required": ["index", "category", "tags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


def select(client, totals, candidates, limit=PICK_COUNT):
    """**40件を1コールで捌く（R1）。** 1件ずつ投げるとシステムプロンプトを40回払う"""
    lines = []
    for i, item in enumerate(candidates):
        source = item.get("source") or item.get("origin", "")
        description = item.get("description", "")
        # **現地語の記事はそう分かるように出す。** 言語が混ざっていることを
        # 伝えないと、モデルが「関係のない記事が混ざっている」と判断して落とす
        tag = f" [{item['lang']} local]" if is_local(item) else ""
        line = f"[{i}]{tag} {item['title']} — {source}"
        if description:
            line += f"\n    {description[:200]}"
        lines.append(line)

    prompt = f"""Here are today's candidate items.

{chr(10).join(lines)}

Choose at most {limit} items to publish, best first. For each, return its index and
one category:

  chip    = semiconductors, fabs, capacity, memory, packaging, hardware supply
  model   = AI models, research results, capability or benchmark news
  funding = funding rounds, investments, acquisitions, capex commitments
  policy  = regulation, export controls, government action, litigation
  product = shipping products and services built on AI
  howto   = how AI is actually used in practice — agents and tools in real workflows,
            what a team learned adopting it, concrete technique

Include one or two `howto` items when the list contains good ones. Do not force it —
if nothing today qualifies, publish none rather than picking a weak one.

Also return `tags` for each item: 1 to {MAX_TAGS} ids from this list, most relevant first.

{TOPIC_HINTS}

Use ONLY ids from that list — never invent one. Tag what the item is actually about, not
every subject it mentions in passing. **Do not pad to {MAX_TAGS}**: one precise tag is
better than three loose ones, and a loose tag puts the item in front of a reader who
asked for something else. Return an empty list only if genuinely nothing fits.

Return fewer than {limit} if fewer are worth publishing. Do not pad the list."""

    data = _call(
        client, totals,
        model=MODEL_SELECT, system=SELECT_SYSTEM, prompt=prompt,
        max_tokens=MAX_TOKENS_SELECT, schema=SELECT_SCHEMA,
    )
    if not data:
        return []

    picked = []
    for pick in data.get("picks", [])[:limit]:
        index = pick.get("index")
        # **範囲外の番号を信じない。** 出てきた番号がずれていると別の記事を載せる
        if not isinstance(index, int) or not (0 <= index < len(candidates)):
            print(f"    選別が範囲外の番号を返した: {index}")
            continue
        item = dict(candidates[index])
        item["category"] = pick.get("category") if pick.get("category") in CATEGORIES else "other"
        # **表に無いタグは落とす。** スキーマで enum を掛けてあるので普通は通らないが、
        # 通すとアプリ側でラベルの引けないタグが残り、絞り込みに一生引っかからない
        tags, seen = [], set()
        for tag in pick.get("tags") or []:
            if tag in TOPIC_IDS and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        item["tags"] = tags[:MAX_TAGS]
        picked.append(item)
    return picked


def _text_schema(fields):
    props = {f: {"type": "string"} for f in fields}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": list(props),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


SUMMARY_SCHEMA = _text_schema(["title_ja", "summary_ja", "title_en", "summary_en"])
TRANSLATE_SCHEMA = _text_schema(["title", "summary"])


def summarize(client, totals, picked):
    """**日本語と英語を1コールで書かせる。**

    元記事が英語なので、英語だけ「日本語からの翻訳」にすると往復になって痩せる。
    入力は1回ぶんしか払わないので、増えるのは出力だけ。
    """
    lines = []
    for i, item in enumerate(picked):
        source = item.get("source") or ""
        lines.append(
            f"[{i}] {item['title']} — {source}\n"
            f"    {item.get('description', '') or '(no description in the feed)'}"
        )

    prompt = f"""Summarize each item below, in order.

{chr(10).join(lines)}

For each item return:
- title_ja / title_en: a headline of at most 40 characters stating what happened.
- summary_ja / summary_en: 2 sentences in plain language.

Write the Japanese natively — it must not read like a translation. Return exactly
{len(picked)} items, in the same order."""

    data = _call(
        client, totals,
        model=MODEL_SUMMARY, system=SUMMARY_SYSTEM, prompt=prompt,
        max_tokens=MAX_TOKENS_SUMMARY, schema=SUMMARY_SCHEMA,
    )
    if not data:
        return None

    rows = data.get("items", [])
    if len(rows) != len(picked):
        # **件数がずれたら畳まない。** 順番で突き合わせているので、ずれると
        # 別の記事の要約が付く（見た目には気付けない事故になる）
        print(f"    要約の件数がずれた: {len(rows)} / {len(picked)}")
        return None
    return rows


def translate(client, totals, language, name, rows):
    """**元記事ではなく、出来上がった英語の要約を訳す（R3）。**

    入力が1/3になり、単価も1/3になる。翻訳は要約より簡単なのでHaikuで足りる。

    **言語ごとに別コールにする。** 1コールで3言語を出させると、`max_tokens` を
    言語ごとに切れず、1言語が壊れたときに他まで巻き添えになる。
    """
    lines = []
    for i, row in enumerate(rows):
        lines.append(f"[{i}] {row['title_en']}\n    {row['summary_en']}")

    prompt = f"""Translate each item below into {name}.

{chr(10).join(lines)}

Return exactly {len(rows)} items, in the same order, each with a `title` and a `summary`."""

    data = _call(
        client, totals,
        model=MODEL_TRANSLATE, system=TRANSLATE_SYSTEM, prompt=prompt,
        max_tokens=MAX_TOKENS_TRANSLATE, schema=TRANSLATE_SCHEMA,
    )
    if not data:
        return None

    items = data.get("items", [])
    if len(items) != len(rows):
        print(f"    {language} の翻訳の件数がずれた: {len(items)} / {len(rows)}")
        return None
    return items


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Firestore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def already_processed(db, urls):
    """**要約の前に引く。** 費用が乗る前に捨てる（R7）"""
    stored = set()
    refs = [db.collection(PROCESSED_COLLECTION).document(url_hash(u)) for u in urls]
    for i in range(0, len(refs), 200):
        try:
            for doc in db.get_all(refs[i:i + 200]):
                if doc.exists:
                    stored.add(doc.id)
        except Exception as e:
            # 引けなくても止めない。**二度要約するだけ**で、壊れはしない
            print(f"  処理済みの引き当てに失敗したので飛ばす: {e}")
            return set()
    return stored


def mark_processed(db, urls):
    now = datetime.now(UTC)
    expires = now + timedelta(days=PROCESSED_RETENTION_DAYS)
    batch = db.batch()
    for url in urls:
        batch.set(
            db.collection(PROCESSED_COLLECTION).document(url_hash(url)),
            {"processedAt": now, "expiresAt": expires},
        )
    batch.commit()


def purge_expired(db, collection, field="expiresAt"):
    """保持期間を過ぎたものを消す。

    **TTLポリシーの代わり。** Sparkプラン（無料）のコンソールに項目そのものが無い。
    **消しすぎないよう上限を置く。** 消し残っても次の実行で片付く。
    """
    now = datetime.now(UTC)
    try:
        query = db.collection(collection)
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = query.where(filter=FieldFilter(field, "<", now))
        except ImportError:
            query = query.where(field, "<", now)
        stale = list(query.limit(PURGE_LIMIT).stream())
    except Exception as e:
        # 索引が無い・権限が無いなど。**取り込みは止めない**（掃除は次回でよい）
        print(f"{collection} の期限切れを引けなかったので掃除を飛ばす: {e}")
        return

    if not stale:
        return

    deleted = 0
    for i in range(0, len(stale), BATCH_SIZE):
        batch = db.batch()
        for doc in stale[i:i + BATCH_SIZE]:
            batch.delete(doc.reference)
        try:
            batch.commit()
            deleted += len(stale[i:i + BATCH_SIZE])
        except Exception as e:
            print(f"{collection} の削除に失敗したので中断: {e}")
            break
    print(f"{collection}: 期限切れを {deleted} 件消した"
          + ("（上限に達したので残りは次回）" if len(stale) == PURGE_LIMIT else ""))


def to_rows(picked, summaries, translations):
    """Firestoreに入れる形にする。**アプリの `SectorDigest.newsItem` が読む形**"""
    rows = []
    for i, item in enumerate(picked):
        # **現地語の記事はまとめに入れない。** まとめの面には出し分けの仕組みが無く、
        # 既に配信済みのバージョンを使っている利用者はアプリを更新しても絞れない
        # （韓国の国内話題が日本語に訳されて並ぶ）。フィードにだけ入れる
        if is_local(item):
            continue
        summary = summaries[i]
        row = {
            "source": item.get("source") or "",
            "url": item["url"],
            "tickers": item.get("tickers", []),
            "category": item.get("category", "other"),
            "ja": {"title": summary["title_ja"], "summary": summary["summary_ja"]},
            "en": {"title": summary["title_en"], "summary": summary["summary_en"]},
        }
        published = item.get("published_at")
        if published:
            row["published_at"] = published
        for language, items in translations.items():
            row[language] = {"title": items[i]["title"], "summary": items[i]["summary"]}
        rows.append(row)
    return rows


def write_digest(db, doc_id, rows):
    """**その日のドキュメントに足す形で書く。**

    二度走っても上書きで減らさない。`processed_urls` があるので二度目は
    たいてい0件になるが、そのときに `news: []` で潰さないようにする。
    """
    ref = db.collection(COLLECTION).document(doc_id)
    existing = ref.get()
    news = []
    if existing.exists:
        news = (existing.to_dict() or {}).get("news") or []

    known = {r.get("url") for r in news}
    added = [r for r in rows if r["url"] not in known]
    if not added:
        print("その日のドキュメントに足すものが無い")
        return 0

    now = datetime.now(UTC)
    ref.set({
        "news": news + added,
        "generated_at": now,
        "expiresAt": now + timedelta(days=RETENTION_DAYS),
    }, merge=True)
    return len(added)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AIフィード（`ai_feed/{sha1}`）
#
# **同じ収集・選別・要約の結果を、もう1つの形で置くだけ。** 追加のAPI呼び出しは
# ゼロなので、費用は増えない。日付で束ねた `sector_digest` は「その日のまとめ」を
# 読ませる面、こちらは**利用者が選んだトピックだけ**を流す面で、束ね方が違う。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def feed_doc_id(url):
    """設計メモ §3。**URLのSHA-1の先頭16文字。**

    `url_hash`（SHA-256）とは別に持つ。あちらは `processed_urls` のIDで、
    **すでに書かれたドキュメントのIDを変えられない**ので揃えられない。
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def to_feed_docs(picked, summaries, translations, now):
    """`ai_feed` に入れる形にする。**アプリの `AIFeedItem` が読む形。**

    ⚠️ **繁體中文のキーは `zh_Hant`。** 設計メモは `zhHant` と書いているが、
    このリポジトリとアプリは既に `zh_Hant`（`TRANSLATED` / `SectorDigest.languageKeys`）で
    通してある。**綴りを3つ目に増やすと、どれで書かれたか分からない項目がもう1つ増える。**
    アプリ側は `zh_Hant` / `zh` / `zhHant` のどれでも読めるようにしてある。

    **`title` は原文のまま置く**（設計メモの通り）。ただし訳した見出しは要約と
    同じコールで既に手元にあるので、`titles` にも入れておく。入れないと、隣の
    まとめの面が日本語の見出しなのに、フィードだけ英語の見出しが並ぶ。
    """
    docs = []
    for i, item in enumerate(picked):
        summary = summaries[i]
        titles = {"ja": summary["title_ja"], "en": summary["title_en"]}
        bodies = {"ja": summary["summary_ja"], "en": summary["summary_en"]}
        for language, rows in translations.items():
            titles[language] = rows[i]["title"]
            bodies[language] = rows[i]["summary"]

        # **`publishedAt` を必ず入れる。** アプリは `publishedAt` の降順で引くので、
        # 欠けているドキュメントは索引に載らず、**画面に一生出てこない。**
        # 日時を読めなかった記事も収集の窓の中にはあるので、いまの時刻に倒す
        published = item.get("published_at") or now

        doc = {
            "kind": "news",
            # 収集元の言語。**アプリはこれで出し分ける**（英語＝全員、
            # 現地語＝その言語で読んでいる利用者だけ）
            "lang": item.get("lang") or LANG_GLOBAL,
            "title": item["title"],
            "titles": titles,
            "url": item["url"],
            "source": item.get("source") or "",
            "publishedAt": published,
            "tags": item.get("tags", []),
            "summary": bodies,
            # 動画だけが持つ伸び率（設計メモ §4.3）。ニュースは0
            "score": 0,
            "createdAt": now,
            "expiresAt": published + timedelta(days=AI_FEED_RETENTION_DAYS),
            # まとめの面と同じ区分・銘柄も入れておく（同じ選別結果なので費用はかからない）
            "category": item.get("category", "other"),
            "tickers": item.get("tickers", []),
        }
        # **無い項目は書かない。** `thumbnailUrl: null` / `durationSec: null` を
        # 置くと、アプリ側で「取得に失敗した」のか「もともと無い」のか区別できない
        docs.append((feed_doc_id(item["url"]), doc))
    return docs


def write_ai_feed(db, docs):
    """**書き込む前に存在を確かめる**（設計メモ §3）。

    cronが二重に走っても、同じ記事が2つのドキュメントになることは無い（IDがURL由来）。
    それでも上書きしないのは、**既に出ているものの内容を後から変えない**ため。
    """
    if not docs:
        return 0

    refs = [db.collection(AI_FEED_COLLECTION).document(doc_id) for doc_id, _ in docs]
    existing = set()
    try:
        for i in range(0, len(refs), 200):
            for doc in db.get_all(refs[i:i + 200]):
                if doc.exists:
                    existing.add(doc.id)
    except Exception as e:
        # 引けなくても止めない。**上書きになるだけ**で、重複はIDで防げている
        print(f"  ai_feed の存在確認に失敗したので、そのまま書く: {e}")

    added = [(doc_id, doc) for doc_id, doc in docs if doc_id not in existing]
    if not added:
        print("ai_feed に足すものが無い")
        return 0

    batch = db.batch()
    for doc_id, doc in added:
        batch.set(db.collection(AI_FEED_COLLECTION).document(doc_id), doc)
    batch.commit()
    return len(added)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 取り込み
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def collect(since):
    print("収集")
    items = (collect_google_news() + collect_google_news_local()
             + collect_ir() + collect_practice() + collect_hacker_news(since))
    print(f"  合計 {len(items)} 件")
    return items


def report_cost(totals):
    if not totals:
        return
    print("\n使ったトークン")
    total = 0.0
    for model, u in totals.items():
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        cost = u["input"] / 1_000_000 * rate_in + u["output"] / 1_000_000 * rate_out
        total += cost
        print(f"  {model}: 入力 {u['input']:,} / 出力 {u['output']:,} "
              f"/ キャッシュ読み {u['cache_read']:,} → 約 ${cost:.4f}")
    print(f"  この実行の概算: 約 ${total:.4f}（月30日で約 ${total * 30:.2f}）")


def main(db, client, feed_only=False):
    """`feed_only` のときは `ai_feed` にだけ書く（まとめには触らない）。

    **窓（`LOOKBACK_HOURS`）は縮めない。** `processed_urls` が既出を落とすので、
    2時間おきに回しても同じ記事を二度要約することは無い。逆に窓を縮めると、
    cronが遅れた回（実測で4時間26分の遅延がある）に取りこぼす。
    """
    totals = {}
    now = datetime.now(UTC)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    dry_run = os.environ.get("DRY_RUN") == "1"
    limit = FEED_PICK_COUNT if feed_only else PICK_COUNT
    label = "フィードのみ" if feed_only else "まとめ＋フィード"
    print(f"実行: {label}（最大 {limit} 件）\n")

    # **取り込みより先に掃除する。** 取り込みが途中で落ちても掃除は済んでいる
    if not dry_run:
        purge_expired(db, COLLECTION)
        purge_expired(db, PROCESSED_COLLECTION)
        purge_expired(db, AI_FEED_COLLECTION)

    candidates = preprocess(collect(since), since)
    if not candidates:
        print("候補が無いので中止")
        return

    stored = already_processed(db, [c["url"] for c in candidates])
    fresh = [c for c in candidates if url_hash(c["url"]) not in stored]
    print(f"候補 {len(candidates)} 件 / 処理済み {len(stored)} 件 / 新規 {len(fresh)} 件")
    if not fresh:
        print("新しい記事が無いので中止")
        return

    print("選別")
    picked = select(client, totals, fresh, limit=limit)
    if not picked:
        print("載せるものが無いので中止")
        report_cost(totals)
        return
    for item in picked:
        print(f"  [{item['category']}] {item['title']} — {item.get('source', '')}")

    print("要約（日本語・英語）")
    summaries = summarize(client, totals, picked)
    if not summaries:
        print("要約できなかったので中止")
        report_cost(totals)
        return

    print("翻訳")
    translations = {}
    for language, name in TRANSLATED.items():
        items = translate(client, totals, language, name, summaries)
        if items:
            translations[language] = items
            print(f"  {language}: {len(items)} 件")
        else:
            # **1言語が壊れても他は出す。** アプリは訳の欠けた言語を英語に倒す
            print(f"  {language}: 訳せなかったので、この言語だけ英語に倒れる")

    rows = to_rows(picked, summaries, translations)
    doc_id = now.astimezone(JST).strftime("%Y-%m-%d")

    feed_docs = to_feed_docs(picked, summaries, translations, now)

    # **`rows` と `feed_docs` を zip しない。** まとめには現地語が入らないので
    # 長さが揃わず、別の記事どうしが対になる
    print(f"\nまとめ {len(rows)} 件 / ai_feed {len(feed_docs)} 件"
          + ("（今回はまとめに書かない）" if feed_only else ""))
    for _, doc in feed_docs:
        tags = ",".join(doc["tags"]) or "-"
        mark = "  " if doc["lang"] == LANG_GLOBAL else f"{doc['lang'][:2]}"
        print(f"  {mark} [{doc['category']:7}] {tags:34} {doc['titles']['ja']}")

    # **タグが1つも付かなかった記事を黙って通さない。** フィードは
    # タグでしか出し分けられないので、無タグの記事は誰の画面にも出ない
    untagged = sum(1 for _, doc in feed_docs if not doc["tags"])
    if untagged:
        print(f"  ⚠️ タグの付かなかった記事が {untagged} 件（AIフィードには出ない）")

    if dry_run:
        print("\nDRY_RUN のため書き込みませんでした")
        report_cost(totals)
        return

    # **feed_only はまとめに触らない。** 詳しくは `FEED_PICK_COUNT` のコメント
    added = 0 if feed_only else write_digest(db, doc_id, rows)
    feed_added = write_ai_feed(db, feed_docs)
    # **書き込みのあとに印を付ける。** 先に付けると、書き込みが落ちた日の記事が
    # 「処理済み」として二度と拾われなくなる
    # **`rows` ではなく `feed_docs` を見る。** まとめには現地語が入らないので、
    # `rows` で印を付けると現地語の記事が毎回「新規」になり、2時間おきに
    # 同じ記事を要約し続ける
    mark_processed(db, [doc["url"] for _, doc in feed_docs])
    print(f"書き込み まとめ {added} 件 / ai_feed {feed_added} 件")
    report_cost(totals)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 構造の確認（副作用なし）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def inspect():
    """**FirestoreにもClaudeにも触れない。** いつ走らせても副作用が無い"""
    print(f"User-Agent: {USER_AGENT}")
    print(f"見る範囲: 直近 {LOOKBACK_HOURS} 時間 / 選別に渡す上限: {MAX_CANDIDATES} 件\n")

    since = datetime.now(UTC) - timedelta(hours=LOOKBACK_HOURS)
    items = collect(since)

    by_origin = {}
    for item in items:
        by_origin[item.get("origin", "?")] = by_origin.get(item.get("origin", "?"), 0) + 1
    print(f"\n取得元の内訳: {by_origin}")

    candidates = preprocess(items, since)
    print(f"\n=== 選別に渡す {len(candidates)} 件（新しい順・上位15） ===")
    for i, item in enumerate(candidates[:15]):
        published = item.get("published_at")
        stamp = published.astimezone(JST).strftime("%m-%d %H:%M") if published else "  日時不明  "
        tickers = ",".join(item["tickers"]) or "-"
        print(f"  [{i:2}] {stamp} 点{item['score']:3} {tickers:12} {item['title'][:64]}")
        print(f"       {item.get('source', '')} / {item['url'][:80]}")

    if not candidates:
        print("\n⚠️ 候補が0件。取得元がすべて落ちているか、キーワードが厳しすぎる")
        return 1

    with_description = sum(1 for c in candidates if c.get("description"))
    print(f"\ndescriptionのある候補: {with_description}/{len(candidates)}")
    print("（要約はタイトル＋descriptionだけで作る。0に近いと中身の薄い要約になる）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="収集と前処理だけ試す")
    parser.add_argument("--feed-only", action="store_true",
                        help="ai_feed にだけ書く（まとめは触らない）。2時間おきの実行用")
    args = parser.parse_args()

    if args.inspect:
        sys.exit(inspect())

    import firebase_admin
    from anthropic import Anthropic
    from firebase_admin import credentials, firestore

    service_account = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(service_account))

    main(firestore.client(), Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip()),
         feed_only=args.feed_only)
