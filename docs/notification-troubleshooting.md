# 朝の通知が届かないときの調べ方

StockWatch AI の「毎朝のブリーフィング通知」に関する調査手順と、これまでに判明した問題の記録。

## 通知が届くまでの経路

```
GitHub Actions (morning_briefing.yml)
   │  平日 20:30 UTC に発火
   ▼
scripts/morning_briefing.py
   │  ① Firestore users から全ユーザーの watchlist を集約
   │  ② Finnhub から株価とニュースを取得
   │  ③ Claude API で銘柄ごとに 60 字要約を生成
   │  ④ Firestore briefings/{uid} に本文を保存
   │  ⑤ FCM でユーザーごとに通知を 1 通送信
   ▼
Firebase Cloud Messaging
   ▼
APNs
   ▼
iPhone
```

通知は**ウォッチリスト内で値動きが最大の 1 銘柄**についてのみ送られる。全銘柄のまとめは通知本文ではなく `briefings/{uid}` に入る。

各段階で「成功」の意味が違う点に注意:

| 段階 | 「成功」が保証すること | 保証しないこと |
|---|---|---|
| GitHub Actions が `success` | Python が exit 0 で終了した | 通知が 1 通でも送られたか |
| `通知送信成功` ログ + `FCM応答=...` | FCM がメッセージを受理した | APNs が配送したか、端末が表示したか |
| Firestore に `fcmToken` がある | FCM 登録トークンは発行済み | APNs トークンが紐付いているか |

**FCM が成功を返しても端末に何も出ないことは普通に起きる。** ログだけで「届いた」と判断しない。

---

## 診断手順

上から順に実施すると切り分けが早い。

### 1. サーバーは自分に送ったか（GitHub Actions のログ）

https://github.com/StockWatchAI/stockwatch-ai/actions/workflows/morning_briefing.yml

最新の実行 → 左サイドバーの `send` → `Run python scripts/morning_briefing.py` を展開。末尾にユーザーごとの結果が出る。

```
通知送信成功: <UID> → AMZN -2.3% / FCM応答=projects/stockwatch-ai-12642/messages/...
<UID>: トークンかウォッチリストが空のためスキップ
```

- **「通知送信成功」側にいる** → サーバーは送っている。原因は端末側（手順 3 へ）
- **「スキップ」側にいる** → 送信すらされていない。原因は Firestore のデータ（手順 2 へ）

### 2. 自分のデータは揃っているか（Firestore）

Firebase コンソール → Firestore → `users` コレクション。

自分の UID が分からない場合は、`watchlist` がアプリに登録した銘柄と一致するドキュメントを探す。見つけたら `fcmToken` を確認:

- **`fcmToken` が空 / 存在しない** → 原因確定。アプリが FCM トークンを Firestore に保存できていない
- **`fcmToken` に値がある** → サーバー側は正常。手順 3 へ

`briefings/{uid}` の有無でも同じ判定ができる。このドキュメントはスキップ判定を通過したユーザーにだけ書き込まれるため、**存在して `createdAt` が当日なら、スキップされていない**。UID を特定できていなくても使える。

### 3. 端末まで届くか（Firebase からテスト送信）

翌朝を待たずに配送経路だけを検証できる。

1. Firestore から自分の `fcmToken` をコピー
2. Firebase コンソール → Messaging → 既存の下書きキャンペーンを開く → **「編集」**
   （**「公開」は押さない。全ユーザーに本番配信される**）
3. 編集画面右側の「テストメッセージを送信」
4. トークンを貼り付け → 「+」で追加 → チェック → 「テスト」

**送信前に iPhone でアプリを完全に閉じる。** フォアグラウンドだと iOS はバナーを表示しないため、切り分けにならない。

- **届く** → 配送経路は正常。原因はスケジュールやタイミング側
- **届かない** → 端末〜APNs 間で確定。手順 4 へ

### 4. 端末側の確認

以下を順に確認する。

**通知許可** — 設定 → 通知 → StockWatch AI。「通知を許可」と「ロック画面 / 通知センター / バナー」が ON か。ここが最頻出。

**Push Notifications capability** — Xcode → Signing & Capabilities に追加されているか。無いと `aps-environment` entitlement が生成されず、APNs トークンを取得できない。この状態でも **FCM 登録トークンだけは発行される**ため、Firestore にトークンがあっても届かない。

**APNs 認証情報** — Firebase コンソール → プロジェクトの設定 → Cloud Messaging → 「Apple アプリの構成」。

- **APNs 認証キー（.p8）** — Sandbox・本番の両方に対応。環境ミスマッチは起きにくい
- **APNs 証明書（.p12）** — 開発用と本番用が別。本番用しか登録していない場合、**Xcode から入れた Debug ビルドには届かない**

**環境の明示指定** — アプリ側に `Messaging.messaging().setAPNSToken(deviceToken, type: .prod)` のような記述があれば削除する。`Messaging.messaging().apnsToken = deviceToken` と代入するだけにすれば、Firebase が Provisioning Profile の `aps-environment` を読んで自動判定する。手動指定は環境ミスマッチの直接原因になる。

**登録失敗ログ** — AppDelegate に以下を仕込み、Xcode コンソールを確認する。

```swift
func application(_ application: UIApplication,
                 didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    print("APNsトークン取得:", deviceToken.map { String(format: "%02x", $0) }.joined())
    Messaging.messaging().apnsToken = deviceToken
}

func application(_ application: UIApplication,
                 didFailToRegisterForRemoteNotificationsWithError error: Error) {
    print("APNs登録失敗:", error)   // ここが出るならエラー内容が答え
}
```

どちらも出ないなら通知許可が下りていない。

---

## 調査記録: 2026-08-05

### 症状

実機で毎朝の通知が出てこない。

### 判明した事実

前日（2026-08-04 21:44 UTC 実行、日本時間 8/5 6:44 着）のログより:

- ユーザー 14 人中、**9 人に送信成功、5 人がスキップ**
- 株価取得・AI 要約・FCM 送信はすべて正常。過去 20 回の実行もすべて `success`
- スクリプトはユーザーごとの例外を握りつぶすため、**通知が 0 通でも Actions は `success` になる**

### 修正済み: cron の曜日が UTC のままだった

`morning_briefing.yml` の cron は時刻だけ JST に換算され、曜日フィールドが UTC 基準のままだった。

```
- cron: '30 20 * * 1-5'   # UTC 月〜金 = JST 火〜土
```

結果として **JST 月曜の朝は一度も配信されず**、逆に **JST 土曜の朝に不要な配信**が出ていた。曜日を `0-4`（UTC 日〜木）に変更して JST 月〜金に揃えた。

- PR #1 / commit `0286c70` でマージ済み
- **検証**: 8/8（土）に通知が来ないこと、8/10（月）に通知が来ることを確認する

### 未解決の問題

**スケジュール実行が毎回 50〜75 分遅延している。** 20:30 UTC 指定に対し、実測は 21:24〜21:44 UTC。日本時間で 5:30 ではなく 6:24〜6:44 に到達している。

```
UTC 08/04(火) 21:44 -> JST 08/05(水) 06:44   遅延 +74分
UTC 08/03(月) 21:35 -> JST 08/04(火) 06:35   遅延 +66分
UTC 07/31(金) 21:34 -> JST 08/01(土) 06:34   遅延 +64分
UTC 07/30(木) 21:38 -> JST 07/31(金) 06:38   遅延 +69分
UTC 07/29(水) 21:24 -> JST 07/30(木) 06:24   遅延 +54分
```

GitHub Actions のスケジュール実行は混雑時に遅延し、**実行自体がスキップされることもある**（仕様）。5:30 到達を保証したい場合の選択肢:

1. cron を `30 19` に前倒しする（遅延を見込む。ただし遅延幅は日によって変動する）
2. Cloud Scheduler + Cloud Functions に移行する（確実だが Blaze プランが必要）

**5 人がスキップされている。** `fcmToken` か `watchlist` のいずれかが空。現在のログは**どちらが欠けているか区別していない**ため、原因が特定できていない。ログ改善が必要（下記）。

### テスト送信の結果: 配送経路は正常

- 自分の Firestore ドキュメントに `fcmToken` は存在 → 送信成功 9 人側にいる
- Firebase コンソールからのテスト送信は **iPhone に届いた**

これにより以下が確定した:

- FCM → APNs → 端末の配送経路は正常
- 通知許可は下りている
- Push Notifications capability は設定済み
- Firebase の APNs 認証情報は正しい
- **APNs 環境のミスマッチではない**

サーバーは送っており、端末は受け取れる。にもかかわらず朝の通知に気づかなかったということは、**問題は配送ではなく時間帯にある**可能性が高い。

### 有力な仮説: 集中モードによる消音

朝の通知が到達しているのは 6:24〜6:44。この時間帯は睡眠フォーカス / おやすみモードが有効なことが多く、その場合 iOS は通知を**消音して通知センターに送るだけ**でバナーも音も出さない。テスト送信は日中に起きた状態で行ったため表示された、という違いで説明がつく。

確認すべき箇所:

- 設定 → 集中モード → 睡眠 / おやすみモード のスケジュール
- 設定 → 通知 → **スケジュールされた要約** — 有効だと通知がまとめられ、指定時刻まで表示されない
- 通知センターを下スワイプで開き、**過去の通知として残っていないか**（残っていれば「届いていたが気づかなかった」で確定）

### 対策候補

現在の `messaging.Message` は `notification` のみで `apns` ペイロードを持たないため、通知の割り込みレベルは既定値になる。集中モードを突破させたい場合は以下を付与する。

```python
msg = messaging.Message(
    notification=messaging.Notification(title=title, body=body),
    token=token,
    apns=messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                sound="default",
                interruption_level="time-sensitive",
            )
        )
    ),
)
```

`time-sensitive` を使うには Xcode 側で **Time Sensitive Notifications** capability の追加が必要。また、ユーザーが設定でこのアプリの時間指定通知を許可している必要がある。

株価の朝ブリーフィングが `time-sensitive` に値するかは判断が分かれる。濫用すると iOS 側で無視されるようになるため、まずは `sound="default"` だけを付けて様子を見るのが無難。

---

## 改善候補（未着手）

**スキップ理由を区別してログに出す。** 現状は `トークンかウォッチリストが空のためスキップ` としか出ず、UID だけでは誰なのかも分からない。以下のようにすれば、UID を調べなくても自分の行を特定できる。

```
<UID>: スキップ（fcmToken=無し, watchlist=['AAPL','TSLA']）
```

**`apns` ペイロードを追加する。** 現在の `messaging.Message` は `notification` のみで、sound / badge を指定していない（`scripts/morning_briefing.py`）。通知は表示されるが音は鳴らない。

**FCM の失敗トークンを掃除する。** 端末のアプリ削除や再インストールでトークンは無効化される。`messaging.send()` が `UNREGISTERED` を返した場合に Firestore の `fcmToken` を消す処理が無く、無効トークンが残り続ける。
