"""通知の文面を利用者の言語で作る。

アプリは起動時と言語を変えたときに `users/{uid}` へ `language` と `aiLanguage` を
書く（アプリ側の `WatchlistSync.update(language:aiLanguage:)`）。bot はそれを読んで
文面を切り替える。**アプリ側だけ英語にしても、ここが対応するまで通知は日本語のまま**
だった、という状態を解消するための仕組み。

**値が無い利用者は日本語として扱う**（既存の利用者がほぼ日本語のため）。
ver2.1 で繁体字と韓国語を足した。アプリが `traditionalChinese` / `korean` を書く。 アプリを更新していない人がまだ大半で、
既定を英語にすると、これまで日本語で受け取っていた人の通知が突然英語になる。

**固定の文言とAIの文章で経路を分ける。** 固定の文言（「日経平均の終値」など）は
下の表から引く。AIが書く文章は「この言語で書け」と指示するだけで済むので、
訳を持たずに15言語へ広げられる。アプリ側の `Localization.swift` と同じ考え方。
"""

JA = "ja"
EN = "en"
ZH_HANT = "zh-Hant"
KO = "ko"

# アプリ側 `AppLanguage` の rawValue → botで使う言語コード。
# アプリは `.system` を解決してから書くので、ここに `system` は来ない
_APP_LANGUAGE = {
    "japanese": JA,
    "english": EN,
    "traditionalChinese": ZH_HANT,
    "korean": KO,
}

# アプリ側 `AIOutputLanguage` の rawValue → プロンプトに書く言語名（英語表記）。
# **アプリに言語を足したらここにも足す。** 知らない値は表示言語に倒れるので、
# 足し忘れても通知が止まることはない（その言語で書かれなくなるだけ）
_AI_LANGUAGE_NAME = {
    "english": "English",
    "japanese": "Japanese",
    "simplifiedChinese": "Simplified Chinese",
    "traditionalChinese": "Traditional Chinese",
    "korean": "Korean",
    "spanish": "Spanish",
    "portuguese": "Portuguese",
    "french": "French",
    "german": "German",
    "hindi": "Hindi",
    "indonesian": "Indonesian",
    "thai": "Thai",
    "vietnamese": "Vietnamese",
    "arabic": "Arabic",
}


# 表示言語に合わせるときにAIへ渡す言語名
_FOLLOW_APP_NAME = {
    JA: "Japanese",
    EN: "English",
    ZH_HANT: "Traditional Chinese",
    KO: "Korean",
}


def display_language(user):
    """表示言語。固定の文言をどちらで出すかを決める"""
    raw = (user.get("language") or "").strip()
    return _APP_LANGUAGE.get(raw, JA)


# 固定の文言。**英語をキーにする**（アプリ側の `Translations.swift` と同じ考え方）。
# 訳が無ければ英語に倒れるので、足し忘れても通知が止まらない
_FIXED = {
    ZH_HANT: {
        "Large shareholding report filed": "已提交大量持股申報",
        "Nikkei 225 close": "日經225收盤",
        "US market close": "美股收盤",
        "Morning briefing": "晨間簡報",
        "Short interest increased": "空頭餘額增加",
    },
    KO: {
        "Large shareholding report filed": "대량보유 보고서가 제출되었습니다",
        "Nikkei 225 close": "닛케이225 종가",
        "US market close": "미국 증시 종가",
        "Morning briefing": "모닝 브리핑",
        "Short interest increased": "공매도 잔고 증가",
    },
}


def t(user, ja, en):
    """固定の文言を利用者の言語で返す。アプリ側の `L.t` と同じ役割。

    **英語をキーに訳を引き、無ければ日本語か英語に倒す。** アプリ側と同じで、
    訳を書いていない文言は英語で出るだけなので通知が止まらない。
    """
    language = display_language(user)
    translated = _FIXED.get(language, {}).get(en)
    if translated:
        return translated
    return ja if language == JA else en


def ai_language_name(user):
    """AIに書かせる言語の名前。プロンプトにそのまま埋める。

    `followApp`・未設定・表に無い値は、すべて表示言語に倒す。
    """
    raw = (user.get("aiLanguage") or "").strip()
    if raw and raw != "followApp":
        name = _AI_LANGUAGE_NAME.get(raw)
        if name:
            return name
    return _FOLLOW_APP_NAME.get(display_language(user), "English")
