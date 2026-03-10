# X-Agent 運用ガイド: 躓きポイント・垢バン回避・デプロイ知見

> 作成: 2026-03-10 | Nagi (@koffeeNagi) デプロイ経験に基づく

---

## 1. アーキテクチャ概要

```
[Claude Sonnet API]
       |  判断 (like/post/reply/quote)
       v
  [x_agent.py]  ── 自律ループ (8-15分サイクル)
       |
       v
[x_agent_browser.py]  ── Playwright Chromium
       |
       v
  [X/Twitter]  ── Cookie認証 (API不使用)
```

**設計思想**: X API を一切使わず、ブラウザCookie認証 + Playwrightで人間のブラウザ操作を再現。トラフィックは通常のChrome利用者と区別不能。

---

## 2. 開発中に躓いたポイント（時系列）

### 2.1 コンストラクタ不一致 (`67782d5`)

**症状**: エージェント起動時にTypeError
**原因**: `XAgentBrowser.__init__()` に存在しないパラメータを渡していた
**教訓**: Producer側（x_agent.py）とConsumer側（x_agent_browser.py）のインターフェースを必ず突合せよ

### 2.2 フィールド名不一致 — `text` vs `content` (`67782d5`)

**症状**: ツイート内容がLLMプロンプトに渡らない（空文字）
**原因**: ブラウザパーサーは `tweet["content"]` として格納するが、エージェント側は `tweet["text"]` で参照
**教訓**: dict のキー名は型チェックで検出できない。`/verify-contract` で Consumer 側を実読せよ

### 2.3 tweet_id キー不一致 (`ab135ad`)

**症状**: アクションログと承認キューに tweet_id が常に空文字
**原因**: パーサーが `tweet_id` で格納 → エージェントが `id` で参照
**教訓**: 同上。contract audit (`data/contract_audit.txt`) で発見

### 2.4 double-@ ハンドル (`ab135ad`)

**症状**: LLMプロンプトで `(@@koffeeNagi)` と二重@
**原因**: パーサーが `@handle` 付きで格納 → プロンプト構築で再度 `@` を付加
**修正**: `.lstrip("@")` で正規化
**教訓**: 文字列の正規化は格納時に一度だけ行う

### 2.5 投稿ボタンセレクタ (`68c712c`)

**症状**: `data/screenshots/post_tweet_error.png` — ツイート投稿が失敗
**原因**: X の UI は文脈（新規投稿/リプライ/引用）によって投稿ボタンの `data-testid` が変わる
**修正**: `tweetButton` → `tweetButtonInline` のフォールバック追加 + ボタン有効化待機 1.5秒
**教訓**: X の DOM は不安定。セレクタは複数候補 + フォールバックが必須

### 2.6 リプライ機能の不安定性 (`68c712c`)

**症状**: `data/screenshots/reply_error.png` — リプライ操作が失敗
**判断**: リプライを `require_approval` に格下げし、like + post のみ自律実行
**教訓**: 全機能を一度に自律化しない。安定した操作から段階的に自律度を上げる

### 2.7 Cookie 抽出の困難（`da034df`, `126793f`）

**症状**: RemotePC 上で Chrome cookie の抽出ができない
**試行1**: Chrome CDP (`--remote-debugging-port=9222`) で接続 → SSH セッションからはデスクトップにアクセスできない
**試行2**: PsExec で Session 1（デスクトップ）に Chrome を起動 → 成功するが手順が複雑
**試行3**: DPAPI 直接復号 (`extract_cookies_direct.py`) → Chrome の SQLite DB を直接読み、Windows DPAPI + AES-256-GCM で復号 → **ブラウザ起動不要、SSH 経由で動作**
**教訓**: Windows のデスクトップセッション制約を理解せよ。SSH = Session 0（非対話）、デスクトップ = Session 1。GUI が必要な操作は PsExec `-i 1` か Scheduled Task が必要

---

## 3. 垢バン回避の設計原則

### 3.1 実装済みの対策

| カテゴリ | 対策 | 実装箇所 |
|---------|------|---------|
| **タイピング** | 1文字20-80ms、句読点後100-400ms追加 | `x_agent_browser.py:_sync_human_type_blocking()` |
| **思考時間** | 操作前に2-8秒のランダム待機 | `x_agent_browser.py:_human_pause()` |
| **スクロール** | 300-700px のランダムスクロール | `x_agent_browser.py:_human_scroll()` |
| **操作間隔** | 最低30秒（ハード制限） | `x_agent_browser.py:_MIN_ACTION_INTERVAL_S` |
| **サイクル間隔** | 8-15分のランダム間隔 | `configs/nagi.yaml:timing` |
| **睡眠スケジュール** | 0-7時完全停止、22-24時いいねのみ | `x_agent.py:_is_active_hour()` |
| **日次上限** | 100アクション/日 | `configs/nagi.yaml:autonomy` |
| **投稿頻度** | 自発投稿は3時間に1回 | `configs/nagi.yaml:timing.post_interval_minutes` |
| **UA偽装** | Chrome 120 on Windows 10 | `x_agent_browser.py` |
| **Cookie認証** | X API 不使用 → API レート制限回避 | 全体設計 |

### 3.2 設計哲学

1. **「人間のように遅く」**: 速度を上げるのではなく、意図的に遅くする
2. **「人間は寝る」**: 24時間稼働しない。夜は活動を減らし、深夜は完全停止
3. **「人間はすべてに反応しない」**: 関心スコア + 信頼度閾値で大半のツイートをスキップ
4. **「人間は完璧ではない」**: LLM プロンプトに "Be human. Be imperfect" を含む

### 3.3 未実装だが検討すべき対策

| 対策 | 優先度 | 説明 |
|------|--------|------|
| **IP ローテーション** | 高 | 現状 Tailscale IP 固定。Mullvad VPN でトンネリング推奨 |
| **ブラウザフィンガープリント対策** | 中 | Canvas/WebGL/AudioContext のランダム化 |
| **Cookie 自動更新** | 高 | Cookie 有効期限切れ時の自動再取得 |
| **CAPTCHA 検出 + 通知** | 高 | CAPTCHA が出たら人間に通知 |
| **エラー時のバックオフ** | 高 | 連続失敗時に指数的に間隔を広げる |
| **アカウントロック検出** | 高 | ロック画面の DOM 検出 → 即停止 + 通知 |
| **セッション分離** | 中 | Playwright の persistent context でフィンガープリント固定 |

---

## 4. RemotePC デプロイ手順

### 4.1 前提条件

- RemotePC に Tailscale インストール済み
- OpenSSH Server 有効化済み (`scripts/enable_ssh.ps1`)
- Python 3.10+ インストール済み
- Mullvad VPN がインストール・設定済み（新ペルソナでは必須）

### 4.2 デプロイフロー

```
1. ローカルで x-agent リポジトリを準備
   └── configs/<persona>.yaml 作成
   └── personas/<name>/ ディレクトリ作成

2. RemotePC に SSH 接続
   └── ssh USER@<tailscale-ip>

3. リポジトリを clone or pull
   └── git clone / git pull

4. 依存関係インストール
   └── pip install -e .
   └── playwright install chromium

5. Cookie を取得・配置
   └── 方法A: DPAPI 直接復号 (SSH経由可)
   └── 方法B: 手動ログイン (デスクトップアクセス必要)

6. 環境変数設定
   └── ANTHROPIC_API_KEY

7. dry-run で動作確認
   └── python run.py --config configs/<persona>.yaml --dry-run

8. 本番起動
   └── python run.py --config configs/<persona>.yaml

9. Scheduled Task 登録（再起動永続化）
```

### 4.3 Windows SSH セッションの制約

```
SSH Session (Session 0)          Desktop Session (Session 1)
├── CLI 操作 OK                  ├── GUI 操作 OK
├── ファイル操作 OK              ├── Chrome 起動 OK
├── Python スクリプト OK         ├── Cookie 復号 OK (DPAPI)
├── Chrome 起動 NG (※)          └── ...
└── DPAPI 復号 OK
    (ただし対話型ユーザーセッション必要)

※ Chrome はデスクトップに GUI を描画する必要がある。
   SSH Session から Chrome を起動するには:
   - PsExec -i 1 で Session 1 に委任
   - Scheduled Task で Session 1 に起動
```

### 4.4 Cookie 管理の注意点

- Cookie の有効期限は **不定** (X が任意にセッションを無効化する可能性)
- `auth_token` が最重要。これが無効化 = 再ログインが必要
- `ct0` は CSRF トークン。リクエストごとに変わる可能性あり
- **定期的な Cookie 鮮度チェックを推奨** (例: ホームタイムライン取得に成功するか)

---

## 5. ペルソナ追加手順

### 5.1 必要なファイル

```
personas/<name>/
├── SOUL.md            # 核心アイデンティティ（名前、経歴、ミッション）
├── origin.yaml        # 生い立ち・バックストーリー
├── psyche.yaml        # 信念体系・内なる声・認知バイアス
├── value_matrix.yaml  # 関心キーワード（高/中/無視）
└── voice.yaml         # 文体ルール（プラットフォーム別）

configs/<name>.yaml    # エージェント設定（自律度、タイミング、LLM）
data/<name>_x_cookies.json  # セッション Cookie
```

### 5.2 新アカウント運用時の注意

1. **新規アカウントは特に監視が厳しい** — 作成直後のアカウントは自動化検出の閾値が低い
2. **最初の1-2週間は手動操作を推奨** — アカウントの「信頼スコア」を蓄積
3. **自律化は段階的に**:
   - Week 1-2: 完全手動（アカウント育成期間）
   - Week 3-4: いいねのみ自動（低リスク操作）
   - Week 5+: いいね + 投稿自動（段階的に拡大）
4. **電話番号認証は必須** — 認証なしアカウントは即座にロックされる可能性大
5. **プロフィールを完全に埋める** — アイコン、ヘッダー、自己紹介、場所
6. **既存の人間アカウントとの交流を最初に確保** — 孤立したアカウントは怪しまれる

---

## 6. Mullvad VPN トンネリング（新ペルソナ向け）

### 6.1 なぜ VPN が必要か

- 同一 IP から複数の X アカウントが自動操作 → 一括バンのリスク
- Nagi と新ペルソナは **異なる IP** から操作すべき
- Mullvad は「アカウント番号のみ」で契約可能 → 身元紐付けなし

### 6.2 推奨構成

```
RemotePC
├── Nagi: Tailscale IP (直接接続) or Mullvad Server A
└── 新ペルソナ: Mullvad Server B (異なる出口ノード)
```

### 6.3 Mullvad Split Tunneling

- アプリケーション単位で VPN を適用可能
- Nagi のプロセスは VPN 外、新ペルソナのプロセスは VPN 内
- **ただし**: Playwright (Chromium) のプロセスは同一実行ファイル → split tunneling でのプロセス分離が困難
- **推奨**: 別のユーザーアカウント or VM で分離するのが確実

---

## 7. セキュリティ注意事項

### 現在のリスク

| リスク | 状態 | 対策 |
|--------|------|------|
| SSH パスワード平文 | `scripts/_remote_exec.py` に `Password123!` | SSH 鍵認証に移行 |
| Cookie 値がスクリプトに平文 | `scripts/_upload_cookies.py` | 環境変数 or 暗号化ストレージ |
| `.gitignore` の網羅性 | `data/*.json` はイグノアされているか要確認 | `.gitignore` 監査 |

---

## 8. トラブルシューティング索引

| 症状 | 原因 | 解決 |
|------|------|------|
| エージェントが起動しない | Cookie ファイルがない | `scripts/extract_cookies_direct.py` で抽出 |
| ツイートが投稿できない | ボタンセレクタ変更 | `data/screenshots/` を確認し、セレクタを更新 |
| LLM に内容が渡らない | フィールド名不一致 | `data/contract_audit.txt` のパターンで監査 |
| SSH で Chrome が起動しない | Session 0/1 の制約 | PsExec or Scheduled Task で Session 1 に委任 |
| Cookie 期限切れ | X がセッション無効化 | DPAPI 復号 or 手動再ログイン |
| 日次上限に到達 | `max_actions_per_day` 超過 | 翌日自動リセット (JSONL は日付でフィルタ) |

---

## 変更履歴

| 日付 | 内容 |
|------|------|
| 2026-03-10 | 初版作成（Nagi デプロイ経験に基づく） |
