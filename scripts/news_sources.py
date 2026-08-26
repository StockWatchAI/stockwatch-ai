"""ニュースの配信元を絞る、ホワイトリストとその照合。

**ブラックリストにしない。** 除きたいのはアグリゲーターとコンテンツファーム
（"3 Reasons to Buy NVDA Now" 系）で、名前を変えていくらでも増える。
追いかけ続けるより、通す側を数えたほうが運用が保つ。

**この表はアプリ側にも同じものがある**（`NewsSourceFilter.swift`）。
米国株のニュースはbotがここで絞ってFirestoreへ書き、米国以外の
GoogleニュースRSSはアプリが同じ規則で絞る。**照合の規則を片方だけ変えない。**

Firestoreの `config/newsSources` に置いた一覧が優先される。アプリの更新を
待たずに調整できるようにするため（仕様書の「ハードコードせずFirestoreに置く」）。
下の既定値は、その読み取りが失敗したときの土台。
"""

import re

# 通す配信元。**照合は正規化した前方一致**なので、"reuters" は
# "Reuters" にも "reuters.com" にも当たる（下の `normalize` を参照）
DEFAULT_ALLOWED = [
    # 通信社
    "Reuters", "Associated Press", "AP", "Bloomberg", "Nikkei", "Dow Jones",
    # プレスリリース配信（会社が自分で出す＝一次情報に近い）
    "PR Newswire", "Business Wire", "GlobeNewswire", "ACCESSWIRE",
    "Globe Newswire", "PRNewswire", "Businesswire",
    # 一次情報
    "SEC", "Company Press Release",
    # 主要経済紙
    "The Wall Street Journal", "Wall Street Journal", "WSJ",
    "Financial Times", "FT", "CNBC", "Barron's", "Barrons",
]

# **落としたいものの控え。** ホワイトリスト方式なので黙って落ちるが、
# 判定ログで「意図して落ちた」のか「表に無いだけ」なのかを見分けるために置く
KNOWN_EXCLUDED = [
    "Motley Fool", "Zacks", "Simply Wall St", "InvestorPlace",
    "Benzinga", "TipRanks", "24/7 Wall St", "GuruFocus",
]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(name):
    """照合用に均す。

    `Reuters` / `reuters.com` / `REUTERS` / `Reuters ` をすべて `reuters` にする。
    ドメインで返る配信元と表示名で返る配信元が混ざるため、記号を落として比べる。
    """
    if not name:
        return ""
    s = _NON_ALNUM.sub("", str(name).lower())
    # `www.` `https` がドメインの頭に残ることがある
    for prefix in ("httpswww", "httpwww", "https", "http", "www"):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    return s


def _keys(allowed):
    return [normalize(a) for a in allowed if normalize(a)]


def is_undecidable(source):
    """ラテン文字を含まない配信元か。

    **`日本経済新聞` や `한국경제` を落とさないための逃げ道。** 正規化は
    ラテン英数字しか残さないので、これらは空文字になり、素直に前方一致を
    掛けると**すべて落ちる**。日本・韓国・台湾の面がまるごと空になり、
    0件のフォールバックが毎回走ることになる。

    そもそもこの表が相手にしているのは英語のコンテンツファームで、
    現地語の配信元について言えることを何も持っていない。
    **判断できないものは通す**（黙って消すより良い）。
    """
    return normalize(source) == "" and bool(str(source or "").strip())


def is_allowed(source, allowed=None):
    """通してよい配信元か。

    **前方一致で見る。** `reuters.com` は `reuters` で始まり、
    `Bloomberg Law` は `bloomberg` で始まる。逆に `Benzinga Insights` は
    どの通す名前でも始まらないので落ちる。
    """
    if is_undecidable(source):
        return True
    s = normalize(source)
    if not s:
        return False
    return any(s.startswith(k) for k in _keys(allowed or DEFAULT_ALLOWED))


def filter_items(items, allowed=None, source_key="source"):
    """一覧を絞る。`(通ったもの, 落としたものの配信元の内訳)` を返す。

    **落とした内訳を返すのは、ログに出して表を育てるため。** 通す価値のある
    配信元が落ちていないかは、実際に落ちた名前を見ないと分からない。
    """
    kept, dropped = [], {}
    for item in items:
        name = item.get(source_key) if isinstance(item, dict) else getattr(item, source_key, "")
        if is_allowed(name, allowed):
            kept.append(item)
        else:
            dropped[name] = dropped.get(name, 0) + 1
    return kept, dropped
