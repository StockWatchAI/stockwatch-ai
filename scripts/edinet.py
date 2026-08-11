#!/usr/bin/env python3
"""EDINETの提出書類から大量保有報告書を拾い、対象銘柄を登録している人に通知する。

    python3 scripts/edinet.py --inspect   # 取得だけして構造を出す（書き込み・通知なし）
    python3 scripts/edinet.py             # 取り込んで通知まで送る

大量保有報告書は「誰かがその会社の株を5%以上持った／持ち分が1%以上動いた」ときに
提出される。株価やニュースより早く需給の変化が表に出ることがある。

**突き合わせが空売りより厄介。** 大量保有報告書の提出者は"買った側"（運用会社など）で、
`secCode` は提出者自身の証券コードになる。対象の会社は `issuerEdinetCode`
（EDINETコード、E00000形式）にしか入っていない。ウォッチリストは `7203.T` 形式なので、
EDINETコード → 証券コードの対応表が要る。対応表はEDINETが公開している
Edinetcode.zip（APIキー不要）から作れる。

2026年8月10日ぶんで実測した内容は次のとおり。

    提出書類          468 件
    大量保有(350)      39 件 / 訂正(360) 8 件
    issuerEdinetCode  47/47 埋まっている
    対応表            3,824 件。証券コードに 47/47 解決できた

**日付は決め打ちにしない。** 当日ぶんは夜になっても件数0で返ることがあり、
公表には遅れがある（8月10日ぶんの更新日時は8月11日01:09だった）。
`date.today()` を信じると空振りするので、**書類が見つかる日まで遡って探し、
前回処理した日と同じなら何もしない**。これなら公表の遅れ方が変わっても壊れない。

**社名は対応表から取る。** アプリ同梱の名簿（JPStocks.json）はJPXの月次更新に
依存していて、新規上場が数週間載らない。実際 `607A エブリー` は名簿に無かった。
対応表は社名を持っているので、そちらを使えば新規上場でもコードのまま出さずに済む。
"""

import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# 書類種別コード。350が大量保有報告書、360がその訂正
LARGE_HOLDING = {"350", "360"}

SNAPSHOT_DOC = ("market", "edinet_large_holdings")

# 通知の本文に載せる上限。多いと本文に収まらない
TOP_N = 5

JST = timezone(timedelta(hours=9))

TIMEOUT = 30


def fetch_documents(day, api_key):
    """指定日の提出書類一覧を取る。type=2 で一覧本体まで返る（1はメタデータのみ）"""
    res = requests.get(
        DOCUMENTS_URL,
        params={"date": day.isoformat(), "type": "2", "Subscription-Key": api_key},
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    return res.json()


def load_code_map():
    """EDINETコード → 証券コード の対応表を作る。

    APIキーは要らない公開ファイル。ZIPの中にCSVが1つ入っている。
    文字コードはCP932。見出しの表記に揺れがあるため、**列番号では拾わない。**
    """
    res = requests.get(CODELIST_URL, timeout=TIMEOUT)
    res.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            return {}, "ZIPの中にCSVが無い"
        raw = z.read(name).decode("cp932", errors="replace")

    lines = raw.splitlines()
    # 1行目に説明が入り、2行目が見出しであることが多い。見出しらしい行を探す
    header_index = next(
        (i for i, line in enumerate(lines[:10]) if "ＥＤＩＮＥＴコード" in line or "EDINETコード" in line),
        None,
    )
    if header_index is None:
        return {}, "見出し行が見つからない"

    reader = csv.reader(lines[header_index:])
    header = next(reader)

    def find(*keywords):
        for i, col in enumerate(header):
            if any(k in col for k in keywords):
                return i
        return None

    i_edinet = find("ＥＤＩＮＥＴコード", "EDINETコード")
    i_sec = find("証券コード")
    i_name = find("提出者名", "名称")
    if i_edinet is None or i_sec is None:
        return {}, f"必要な列が無い（見出し: {header[:8]}）"

    mapping = {}
    for row in reader:
        if len(row) <= max(i_edinet, i_sec):
            continue
        code, sec = row[i_edinet].strip(), row[i_sec].strip()
        if not code or not sec:
            continue
        name = row[i_name].strip() if i_name is not None and len(row) > i_name else ""
        mapping[code] = {"sec": sec, "name": name}
    return mapping, None


def recent_weekday(days_back=0):
    """直近の平日。EDINETは土日に提出が無い"""
    day = date.today() - timedelta(days=days_back)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def inspect():
    api_key = os.environ.get("EDINET_API_KEY")
    if not api_key:
        print("EDINET_API_KEY が無い")
        return 1

    # 当日は提出が少ないことがあるので、遡って中身のある日を探す
    for back in range(0, 7):
        day = recent_weekday(back)
        print(f"=== ファイル日付 {day} ===")
        try:
            data = fetch_documents(day, api_key)
        except Exception as e:
            print(f"  取得に失敗: {e}")
            continue

        meta = data.get("metadata") or {}
        status = (meta.get("status") or "").strip()
        print(f"  status={status} message={meta.get('message')}")
        print(f"  更新日時={meta.get('processDateTime')}")
        results = data.get("results") or []
        print(f"  件数={len(results)}")
        if not results:
            print("  提出が無い日。1日戻る")
            continue

        by_type = Counter(r.get("docTypeCode") for r in results)
        print("\n  書類種別コードの内訳（多い順・上位10）:")
        for code, n in by_type.most_common(10):
            print(f"    {code}: {n} 件")

        large = [r for r in results if r.get("docTypeCode") in LARGE_HOLDING]
        print(f"\n  大量保有報告書（350/360）: {len(large)} 件")
        if not large:
            print("  この日は大量保有報告書が無い。1日戻る")
            continue

        print("\n  先頭5件の中身:")
        for r in large[:5]:
            print(f"    docID={r.get('docID')} type={r.get('docTypeCode')}")
            print(f"      提出者     filerName={r.get('filerName')}")
            print(f"      提出者証券 secCode={r.get('secCode')}")
            print(f"      発行会社   issuerEdinetCode={r.get('issuerEdinetCode')}")
            print(f"      概要       {str(r.get('docDescription'))[:70]}")
            print(f"      提出日時   {r.get('submitDateTime')}")

        filled = sum(1 for r in large if r.get("issuerEdinetCode"))
        print(f"\n  issuerEdinetCode が入っている割合: {filled}/{len(large)}")

        print("\n  === 対応表（Edinetcode.zip）===")
        mapping, err = load_code_map()
        if err:
            print(f"  対応表を作れなかった: {err}")
            return 1
        print(f"  対応表の件数（証券コードを持つEDINETコード）: {len(mapping)}")

        resolved = []
        for r in large:
            code = r.get("issuerEdinetCode")
            if code and code in mapping:
                resolved.append((r, mapping[code]))
        print(f"  証券コードに解決できた: {len(resolved)}/{len(large)}")

        print("\n  解決できた例（先頭8件）:")
        for r, m in resolved[:8]:
            # 証券コードは5桁（末尾0）で入る。ウォッチリストは4桁+.T
            sec = m["sec"]
            symbol = sec[:4] + ".T" if len(sec) == 5 and sec.endswith("0") else sec
            print(f"    {symbol:10} {m['name'][:24]:24} ← {r.get('filerName')[:28]}")

        unresolved = [r for r in large if r.get("issuerEdinetCode") not in mapping]
        if unresolved:
            print(f"\n  解決できなかった例（先頭5件）:")
            for r in unresolved[:5]:
                print(f"    issuer={r.get('issuerEdinetCode')} 概要={str(r.get('docDescription'))[:50]}")
        return 0

    print("7日分さかのぼっても大量保有報告書が見つからなかった")
    return 1


def to_symbol(sec_code):
    """証券コードをウォッチリストの表記に合わせる。

    対応表の証券コードは末尾に0を足した5桁（`72030`）で入る。
    英字を含む新形式（`607A0`）も同じ形なので、桁数と末尾だけで判定する。
    """
    if not sec_code or len(sec_code) != 5 or not sec_code.endswith("0"):
        return None
    return sec_code[:4] + ".T"


def collect(day, api_key, code_map):
    """指定日の大量保有報告書を銘柄ごとにまとめる。

    **訂正報告書は除く。** 同じ内容が二度飛ぶのを避ける。変更報告書は
    保有比率が動いたときに出る本物の変化なので残す。

    **銘柄ごとにまとめる。** 同じ会社に複数の提出者が同じ日に出すことがあり
    （実測では1銘柄に5件）、書類ごとに通知すると1銘柄で5通になる。
    """
    data = fetch_documents(day, api_key)
    results = data.get("results") or []

    by_symbol = {}
    for r in results:
        if r.get("docTypeCode") not in LARGE_HOLDING:
            continue
        description = str(r.get("docDescription") or "")
        if description.startswith("訂正報告書"):
            continue
        issuer = r.get("issuerEdinetCode")
        entry = code_map.get(issuer) if issuer else None
        if not entry:
            continue
        symbol = to_symbol(entry["sec"])
        if not symbol:
            continue

        item = by_symbol.setdefault(
            symbol,
            {"symbol": symbol, "name": entry["name"], "filers": [], "docIDs": []},
        )
        filer = str(r.get("filerName") or "").strip()
        if filer and filer not in item["filers"]:
            item["filers"].append(filer)
        item["docIDs"].append(r.get("docID"))

    return len(results), by_symbol


def load_previous_date(db):
    doc = db.collection(SNAPSHOT_DOC[0]).document(SNAPSHOT_DOC[1]).get()
    if not doc.exists:
        return None
    return (doc.to_dict() or {}).get("sourceDate")


def save_snapshot(db, items, source_date):
    db.collection(SNAPSHOT_DOC[0]).document(SNAPSHOT_DOC[1]).set(
        {
            "filings": list(items.values()),
            "sourceDate": source_date,
            "updatedAt": datetime.now(JST).isoformat(),
        }
    )


def notify(db, items):
    """対象の銘柄を登録している人にだけ送る。

    空売りと同じで、全員に同じ一覧を送っても持っていない銘柄の話にしかならない。
    設定が無い利用者は既定でオンとして扱う（アプリを更新していない人にも届かせる）。
    """
    users = list(db.collection("users").stream())
    print(f"ユーザー数: {len(users)}")

    sent = failed = skipped = 0
    for u in users:
        data = u.to_dict()
        token = data.get("fcmToken")
        if not token:
            continue
        if data.get("notifyLargeHolding", True) is False:
            skipped += 1
            continue

        hits = [items[s] for s in (data.get("watchlist") or []) if s in items]
        if not hits:
            continue
        hits = hits[:TOP_N]

        title = "大量保有報告書が提出されました"
        body = "\n".join(
            f"{h['name']}（{'、'.join(h['filers'][:2])}）" for h in hits
        )
        try:
            res = messaging.send(
                messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={"symbol": hits[0]["symbol"]},
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
    api_key = os.environ.get("EDINET_API_KEY")
    if not api_key:
        print("EDINET_API_KEY が無いため中止")
        return

    code_map, err = load_code_map()
    if err:
        # 対応表が作れないと銘柄に紐付けられない。誤った宛先に送るより送らない
        print(f"対応表を作れないため中止: {err}")
        return
    print(f"対応表: {len(code_map)} 件")

    previous_date = load_previous_date(db)

    # 公表の遅れ方が一定でないため、書類が見つかる日まで遡る
    for back in range(0, 7):
        day = recent_weekday(back)
        source_date = day.isoformat()
        try:
            total, items = collect(day, api_key, code_map)
        except Exception as e:
            print(f"{source_date}: 取得に失敗したため中止 {e}")
            return

        if total == 0:
            print(f"{source_date}: 提出が無い。1日戻る")
            continue

        print(f"{source_date}: 提出書類 {total} 件 / 大量保有の対象銘柄 {len(items)} 件")

        if previous_date == source_date:
            print(f"{source_date} は取り込み済みのため中止")
            return

        for item in list(items.values())[:10]:
            print(f"  {item['symbol']:8} {item['name'][:20]:20} ← {'、'.join(item['filers'][:2])}")

        save_snapshot(db, items, source_date)

        if not items:
            print("対象銘柄が無いため通知は送らない")
            return
        if previous_date is None:
            print("前回ぶんが無いため、保存だけして通知は送らない")
            return
        if os.environ.get("DRY_RUN") == "1":
            print("DRY_RUN のため通知は送りません")
            return
        notify(db, items)
        return

    print("7日分さかのぼっても提出書類が見つからなかった")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="取得と解釈だけ試す")
    args = parser.parse_args()

    if args.inspect:
        sys.exit(inspect())

    import firebase_admin
    from firebase_admin import credentials, firestore, messaging

    service_account = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(service_account))
    db = firestore.client()

    main()
