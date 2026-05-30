# qiita-claude-ranking

Qiita API v2 と GitHub Actions を使って、Qiita の `claude` / `ClaudeCode` タグが付いた記事を集計し、ストック数順のランキングを自動生成するリポジトリです。

生成したランキングは Markdown / CSV として `output/` に保存されます。
また、Qiita の既存記事を `PATCH` 更新することで、同じ記事URLを使い続けながら最新ランキングを自動更新できます。

## できること

* Qiita の `claude` / `ClaudeCode` タグ記事を取得
* 直近7日間に投稿された記事を対象に集計
* `stocks_count` の降順でランキング化
* Markdown と CSV を自動生成
* GitHub Actions で毎日自動実行
* Qiita の既存記事を自動更新
* 同じQiita記事URLで最新ランキングを維持

## ランキングの定義

このランキングは、以下の条件で集計しています。

* 対象タグ: `claude`, `ClaudeCode`, `MCP`
* 対象期間: 直近7日間に投稿された記事
* 集計基準: 集計時点の累計 `stocks_count`
* 並び順: `stocks_count` 降順
* 同一記事の重複: 記事IDで重複排除

注意点として、このランキングは「この1週間で増えたストック数ランキング」ではありません。
Qiita API v2 で取得できる `stocks_count` は、記事の現在の累計ストック数です。

## ファイル構成

```text
qiita-claude-ranking/
├── .github/
│   └── workflows/
│       └── weekly-ranking.yml
├── output/
│   ├── qiita_claude_ranking_YYYYMMDD.md
│   └── qiita_claude_ranking_YYYYMMDD.csv
├── fetch_ranking.py
├── update_qiita_item.py
├── .gitignore
└── README.md
```

## 各ファイルの役割

### `fetch_ranking.py`

Qiita API v2 から記事情報を取得し、ランキングを生成するスクリプトです。

主な処理内容は以下です。

* `claude` / `ClaudeCode` タグの記事を取得
* 直近7日間の記事に絞り込み
* 記事IDで重複排除
* ストック数順でランキング化
* Markdown / CSV を `output/` に出力

### `update_qiita_item.py`

生成された最新の Markdown ファイルを読み込み、既存の Qiita 記事を更新するスクリプトです。

新規投稿ではなく、Qiita API v2 の `PATCH /api/v2/items/:item_id` を使って既存記事を更新します。
そのため、Qiita記事URLは変わらず、記事数も増えません。

### `.github/workflows/weekly-ranking.yml`

GitHub Actions のワークフローファイルです。

主な処理内容は以下です。

1. Python環境を準備
2. `fetch_ranking.py` を実行
3. `output/` にランキング結果を生成
4. 生成結果をGitHubに自動コミット
5. `update_qiita_item.py` でQiita記事を更新

## GitHub Actions の実行タイミング

現在は、毎日自動実行する想定です。

```yaml
  schedule:
    # 毎日 09:37 JST に実行
    # GitHub Actions の cron は UTC 基準
    - cron: "37 0 * * *"
```

これは、毎日 09:37 JST に実行する設定です。

手動実行にも対応しています。

```yaml
workflow_dispatch:
```

そのため、GitHub の `Actions` タブから任意のタイミングで実行できます。

## 必要な GitHub Secrets

Qiita記事を自動更新するには、GitHub Secrets に以下を登録します。

| Secret名              | 内容                 |
| -------------------- | ------------------ |
| `QIITA_ACCESS_TOKEN` | Qiitaで発行したアクセストークン |
| `QIITA_ITEM_ID`      | 更新対象のQiita記事ID     |

### `QIITA_ACCESS_TOKEN`

Qiita の設定画面から発行するアクセストークンです。

記事を更新するため、スコープには `write_qiita` が必要です。

### `QIITA_ITEM_ID`

更新対象のQiita記事URLの末尾にあるIDです。

例えば、Qiita記事URLが以下の場合、

```text
https://qiita.com/your_name/items/abcdef1234567890abcd
```

`QIITA_ITEM_ID` に登録する値は以下です。

```text
abcdef1234567890abcd
```

## GitHub Secrets の登録場所

GitHubリポジトリで以下の順に進みます。

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

登録するSecretは以下の2つです。

```text
QIITA_ACCESS_TOKEN
QIITA_ITEM_ID
```

アクセストークンはパスワードと同じ扱いです。
README、ソースコード、GitHub Actions の YAML に直接書かないでください。

## ローカルで実行する場合

ランキング生成だけをローカルで実行する場合は、以下を実行します。

```bash
python fetch_ranking.py
```

実行後、`output/` に Markdown と CSV が生成されます。

```text
output/
├── qiita_claude_ranking_YYYYMMDD.md
└── qiita_claude_ranking_YYYYMMDD.csv
```

Qiita APIトークンをローカルで使う場合は、 `.env` を作成します。

```env
QIITA_ACCESS_TOKEN=your_qiita_access_token
```

`.env` はGitHubにアップロードしないでください。

## Qiita記事を更新する場合

GitHub Actions上では、以下の環境変数を使ってQiita記事を更新します。

```yaml
env:
  QIITA_ACCESS_TOKEN: ${{ secrets.QIITA_ACCESS_TOKEN }}
  QIITA_ITEM_ID: ${{ secrets.QIITA_ITEM_ID }}
  QIITA_POST_PRIVATE: "true"
```

`QIITA_POST_PRIVATE` の意味は以下です。

| 値         | Qiita記事の状態 |
| --------- | ---------- |
| `"true"`  | 限定共有       |
| `"false"` | 公開         |

初回テスト時は `"true"` を推奨します。
問題なく更新できることを確認してから `"false"` に変更します。

## 出力されるランキング形式

Markdownでは、以下のような形式で出力されます。

```markdown
## 1位 [記事タイトル](https://qiita.com/...)

**100ストック**　**120いいね**　/　[user_name](https://qiita.com/user_name) さん 2026-05-28 10時投稿

`Claude` `ClaudeCode` `AI` `LLM`

---
```

Qiita上では、ランキング形式で読みやすく表示されます。

## 運用方針

このリポジトリでは、Qiita記事を毎回新規投稿するのではなく、既存記事を上書き更新します。

そのため、以下の運用になります。

```text
毎日 09:13 JST
↓
GitHub Actions 起動
↓
Qiita API v2 から記事情報を取得
↓
ランキングを Markdown / CSV で生成
↓
GitHub の output/ に保存
↓
既存のQiita記事を PATCH 更新
↓
同じQiita記事URLで最新ランキングを表示
```

## 注意事項

* Qiita APIのレスポンス仕様変更により、スクリプト修正が必要になる可能性があります。
* `stocks_count` は集計時点の累計値です。
* 「週間で増えたストック数」を集計するには、日別スナップショットを保存して差分計算する必要があります。
* GitHub Actionsを手動実行すると、その時点のランキングでQiita記事が更新されます。
* アクセストークンを誤って公開した場合は、すぐにQiita側で削除し、新しいトークンを発行してください。
