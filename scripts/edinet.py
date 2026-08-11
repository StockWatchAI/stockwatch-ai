#!/usr/bin/env python3
"""EDINETの提出書類から大量保有報告書を拾う。

    python3 scripts/edinet.py --inspect   # 取得だけして構造を出す（書き込み・通知はしない）

**まだ --inspect しか無い。** 実物のレスポンスを見てから作りを決めるため、
取り込みと通知は様式を確認した後に足す。JPXの空売りも同じ順序で作った。

大量保有報告書は「誰かがその会社の株を5%以上持った／持ち分が1%以上動いた」ときに
提出される。株価やニュースより早く需給の変化が表に出ることがある。

**突き合わせが空売りより厄介。** 大量保有報告書の提出者は"買った側"（運用会社など）で、
`secCode` は提出者自身の証券コードになる。対象の会社は `issuerEdinetCode`
（EDINETコード、E00000形式）にしか入っていない。ウォッチリストは `7203.T` 形式なので、
EDINETコード → 証券コードの対応表が要る。対応表はEDINETが公開している
Edinetcode.zip（APIキー不要）から作れる。

--inspect で確かめたいのは次の4点。

    1. 書類一覧APIが返す件数と、書類種別コードの内訳
    2. 大量保有報告書（350）と訂正（360）が実際にどう入っているか
    3. issuerEdinetCode がどれくらい埋まっているか
    4. 対応表で証券コードに解決できる割合（ここが低いと通知先を決められない）
"""

import argparse
import csv
import io
import os
import sys
import zipfile
from collections import Counter
from datetime import date, timedelta

import requests

DOCUMENTS_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# 書類種別コード。350が大量保有報告書、360がその訂正
LARGE_HOLDING = {"350", "360"}

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="取得と解釈だけ試す")
    args = parser.parse_args()

    if args.inspect:
        sys.exit(inspect())

    print("まだ --inspect しか実装していない")
    sys.exit(1)
