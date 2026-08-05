# App Store 申請手順

StockWatch AI を App Store に申請するための手順とチェックリスト。

> **このリポジトリにアプリ本体は入っていない。** ここにあるのはサポートサイト（`index.html` / `privacy.html` / `terms.html` / `support.html`）と朝のブリーフィング用スクリプトのみ。Xcode プロジェクトは別の場所にあるため、ビルドとアーカイブは手元の Mac で行う。

---

## 1. 事前準備

- [ ] **Apple Developer Program** に加入（年額 $99 / 約 15,800 円）
- [ ] **Xcode 26 以降**でビルドできる状態にする
      2026 年 4 月 28 日以降、App Store Connect へのアップロードは iOS 26 SDK 以上が必須。古い Xcode ではアップロード自体が弾かれる（Deployment Target は下げたままでよい）
- [ ] **Bundle ID** を登録（例: `com.stockwatchlabs.stockwatchai`）
- [ ] **1024×1024 のアプリアイコン**を用意（アルファチャンネル無し・角丸無し）
- [ ] サポート URL / プライバシーポリシー URL が**公開状態で開けること**を確認

最後の項目は審査で必ず見られる。GitHub Pages が有効になっていないと落ちるので、ブラウザで以下が開くか確認する。

```
https://stockwatchai.github.io/stockwatch-ai/privacy.html
https://stockwatchai.github.io/stockwatch-ai/support.html
```

開かない場合は GitHub の Settings → Pages で `main` ブランチを公開する。

---

## 2. Xcode でのアップロード

1. **Signing & Capabilities** → Team を選択、"Automatically manage signing" にチェック
2. **General** → Version（例 `1.0`）と Build（例 `1`）を設定
   - 更新版なら Version を上げる。**Build 番号は過去に使った番号を再利用できない**
3. **Info.plist** に `ITSAppUsesNonExemptEncryption` = `NO` を追加
   - HTTPS 標準の暗号しか使っていない前提。毎回の輸出コンプライアンス質問をスキップできる
4. ビルド先を **Any iOS Device (arm64)** に変更（シミュレータのままでは Archive できない）
5. **Product → Archive**
6. Organizer で **Distribute App → App Store Connect → Upload**
7. 15〜30 分ほどで App Store Connect の TestFlight にビルドが現れる

---

## 3. App Store Connect の入力

[appstoreconnect.apple.com](https://appstoreconnect.apple.com) → マイ App → 「+」→ 新規 App

| 項目 | 内容 |
|---|---|
| App 名 | `StockWatch AI`（30 字以内・**他アプリと重複不可**なので事前に確認） |
| サブタイトル | `米国株ニュースをAIが日本語で要約`（30 字以内） |
| カテゴリ | ファイナンス（第 2 カテゴリ: ニュース） |
| 説明文 | 最大 4,000 字 |
| キーワード | 100 字・カンマ区切り |
| スクリーンショット | **iPhone 6.9 インチが必須**（1290×2796 または 1320×2868）最大 10 枚。iPad 対応なら 13 インチも必須 |
| サポート URL | `https://stockwatchai.github.io/stockwatch-ai/support.html` |
| プライバシーポリシー URL | `https://stockwatchai.github.io/stockwatch-ai/privacy.html` |
| 価格 | 無料 |

忘れやすい 3 項目:

- **App プライバシー（栄養ラベル）** — ウォッチリストと AI 設定は端末内の UserDefaults に保存され、自社サーバーには送信していない。原則「データを収集しません」で申告できる。ただし Analytics 系 SDK を組み込んでいる場合は別途申告が必要
- **年齢レーティング**のアンケート
- **EU のトレーダーステータス申告** — DSA 対応で必須。未申告だと EU 圏で配信されない。個人開発でも申告が要る

---

## 4. このアプリ固有の審査リスク

利用規約とプライバシーポリシーによれば、ニュースは Finnhub、要約は OpenAI（朝のブリーフィングは Claude API）を利用している。その前提での注意点。

### API キーの埋め込み（審査より実害）

Finnhub / OpenAI のキーをアプリ本体にハードコードしていると、バイナリから抽出される。公開後に第三者に叩かれて課金が膨らむのが典型的な被害。中継サーバー（Cloudflare Workers 等）を挟むのが本来の形。

### 投資助言の免責をアプリ内に表示

利用規約には記載済みだが、**アプリ内（初回起動時や要約画面）にも**「本情報は投資助言ではありません」の表示が必要。ファイナンスカテゴリは審査で見られる。

### カスタムプロンプトの UGC 扱い

自由入力のカスタムプロンプト機能があるため、審査で UGC（ユーザー生成コンテンツ）とみなされ、通報・フィルタ機能を求められる可能性がある。実際は個人設定なので、**審査メモに「カスタムプロンプトは端末内に保存される個人設定であり、他ユーザーへの共有・公開機能はありません」と明記**しておくと通りやすい。

### 第三者 AI へのデータ送信の同意

2026 年のガイドライン更新で、第三者 AI サービスへの個人データ共有には明示的な同意が必要になった。本アプリは個人情報を送らない設計なので、その旨をプライバシーポリシーと審査メモの両方に書いておく。

### Guideline 4.2（最低限の機能性）

「ニュースを要約するだけ」と見なされないよう、AI 性格の切り替えやブリーフィング機能をスクリーンショットで見せる。

### 審査メモに書くこと

- ログイン不要である旨
- 動作確認用のティッカー例（`AAPL`, `NVDA` など）
- 上記のカスタムプロンプトと第三者 AI に関する説明

---

## 5. 提出と審査

「審査へ提出」後、通常 24〜48 時間で結果が出る。リジェクトされた場合は Resolution Center で反論・修正して再提出する。初回審査は落ちる前提で見ておく。

### アップデート申請の場合

1. App Store Connect で「＋ バージョンまたはプラットフォーム」→ 新バージョン番号を作成
2. 「このバージョンの新機能」を記入（必須）
3. ビルドを選択して提出

段階的リリース（Phased Release）を有効にしておくと、不具合時の被害を抑えられる。

---

## 6. リリース時に忘れないこと

- [ ] `index.html` の **「🚀 Coming Soon」を App Store バッジとリンクに差し替える**
- [ ] `privacy.html` の最終更新日を必要に応じて更新する
- [ ] サポートサイトの記述と実際のアプリの挙動に齟齬が無いか確認する
      （例: `support.html` の FAQ には「機種変更時にデータを引き継げない」と書かれている）
