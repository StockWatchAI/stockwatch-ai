# StockWatch AI — リポジトリ概要

米国株ニュースを AI が日本語で要約する iOS アプリ「StockWatch AI」（提供元: StockWatch Labs）の
**サポートサイトと、朝のブリーフィング配信バッチ**を置くリポジトリ。

> **iOS アプリ本体のソースはここには無い。** Xcode プロジェクトは別の場所にある。
> このリポジトリでビルドや実機確認はできない。

## 構成

| パス | 役割 |
|---|---|
| `index.html` | マーケティングページ。現在は「Coming Soon」表示のまま |
| `privacy.html` | プライバシーポリシー |
| `terms.html` | 利用規約 |
| `support.html` | サポート・FAQ（連絡先: sh.hirokawa@icloud.com） |
| `scripts/morning_briefing.py` | 朝のブリーフィング生成・通知送信バッチ |
| `.github/workflows/morning_briefing.yml` | 上記を平日朝に実行する GitHub Actions |
| `docs/` | 調査記録と手順書 |

HTML は GitHub Pages で公開されている。App Store の審査ではプライバシーポリシー URL と
サポート URL が到達可能である必要があるため、**Pages を無効化しない**こと。

```
https://stockwatchai.github.io/stockwatch-ai/privacy.html
https://stockwatchai.github.io/stockwatch-ai/support.html
```

## 朝のブリーフィング配信

```
GitHub Actions (平日 20:30 UTC)
  → Firestore users から全ユーザーの watchlist を集約
  → Finnhub で株価とニュースを取得
  → Claude API で銘柄ごとに 60 字要約を生成
  → Firestore briefings/{uid} に本文を保存
  → FCM で「値動き最大の 1 銘柄」を通知
```

Firebase プロジェクトは `stockwatch-ai`（Spark プラン）。使用モデルは `claude-sonnet-4-6`。

必要な Secrets（リポジトリの Settings → Secrets）:

- `FINNHUB_API_KEY`
- `ANTHROPIC_API_KEY`
- `FIREBASE_SERVICE_ACCOUNT`（サービスアカウント JSON をそのまま格納）

## 注意点

**cron はすべて UTC で評価される。時刻だけでなく曜日も。**
JST の月〜金に配信するには UTC 日〜木（`0-4`）を指定する。過去に曜日の換算漏れで
JST 月曜が配信されない不具合があった（PR #1 で修正済み）。

**GitHub Actions のスケジュール実行は 50〜75 分遅延している。**
20:30 UTC 指定に対し実測 21:24〜21:44 UTC。混雑時は実行自体がスキップされることもある。
時刻の正確さが要る用途には向かない。

**`morning_briefing.py` はユーザーごとの例外を握りつぶす。**
通知が 1 通も送られなくても Actions は `success` になる。成否の判断はジョブのステータスではなく
ログ本文（`通知送信成功` / `スキップ`）で行うこと。

**FCM の「成功」は端末への到達を意味しない。**
`FCM応答=projects/...` は FCM が受理しただけ。APNs 以降は保証されない。

## ドキュメント

- [`docs/notification-troubleshooting.md`](docs/notification-troubleshooting.md) — 通知が届かないときの診断手順と調査記録
- [`docs/app-store-submission.md`](docs/app-store-submission.md) — App Store 申請の手順とチェックリスト
