# Notion ページツリー調査ツール (`plan_tree.py`) マニュアル

## 1. 概要

`plan_tree.py` は、Notion から Confluence への移行計画を立案するための**事前調査専用ツール**です。
Notion ワークスペース（または特定のルートページ配下）のページ・データベース階層を調査し、以下の 2 ファイルを出力します。

1. **`pages.csv`**（UTF-8-BOM 付き CSV）: 1ページ 1行の明細データ（Excel / Google スプレッドシートでそのままフィルタ・ソート・計画記入が可能）
2. **`summary.md`**（Markdown）: 全体統計、階層深さ分布、第1階層 (`level_1`) ごとの規模集計、データベース行数ランキング

### 2つの調査モード
- **API 調査モード (`--api`)**: Notion API (`POST /v1/search`) を利用して最小限のリクエスト数でツリーを構築します。レートリミット制御（`Retry-After` 対応）とキャッシュによる中断再開に対応しています。
- **エクスポート ZIP 分析モード (`--zip` / `--zip-dir`)**: Notion の HTML エクスポート ZIP（単一または複数）を解析し、**API コール 0 回**でツリーと規模を集計します。

---

## 2. 前提条件

- Python 仮想環境（`.venv`）がセットアップ済みであること。
- **API 調査モード (`--api`) を使う場合**:
  - Notion Integration Token が必要です。コマンド実行時に `--notion-token` で指定するか、環境変数 `NOTION_TOKEN`（または `NOTION_API_KEY`、プロジェクト直下の `.env` ファイルも自動読込）に設定してください。
  - 調査対象のページ・データベースに対して、対象 Notion インテグレーションが「コネクト（接続）」されている必要があります。

---

## 3. 基本的な使い方

### 3.1 API 調査モード (`--api`)

```bash
# アクセス可能なワークスペース全体を調査（DB行は明細CSVから除外して軽量化）
.venv/bin/python plan_tree.py --api --exclude-db-rows --output-dir reports/tree_plan

# 特定のルートページ配下のみを調査（サブツリー外やDB行には入らない高速モード）
.venv/bin/python plan_tree.py --api --root-page-id 11111111-1111-1111-1111-111111111111 --subtree-only

# 特定のルートページ配下を調査（search API でDB行数も含めて集計する場合）
.venv/bin/python plan_tree.py --api --root-page-id 11111111-1111-1111-1111-111111111111

# レート制限を下げて実行し、中断した場合はキャッシュから再開
.venv/bin/python plan_tree.py --api --rate-limit 2.5 --output-dir reports/tree_plan
.venv/bin/python plan_tree.py --api --resume --output-dir reports/tree_plan
```

### 3.2 エクスポート ZIP 分析モード (`--zip` / `--zip-dir`)

```bash
# 単一 ZIP を分析
.venv/bin/python plan_tree.py --zip export.zip --output-dir reports/tree_plan

# 複数 ZIP をまとめて分析
.venv/bin/python plan_tree.py --zip part1.zip part2.zip --exclude-db-rows

# ディレクトリ内の全 ZIP を一括分析（特定ページ配下のみ抽出）
.venv/bin/python plan_tree.py --zip-dir ./notion_zips --root-page-id 11111111-1111-1111-1111-111111111111 --output-dir reports/tree_plan
```

### 3.3 保存済み API キャッシュから事後抽出 (`--from-cache`)

`--api` モード（`--subtree-only` なし）で一度全件取得すると、`<output-dir>/api_search_cache.json` に全ページ・DB の検索結果と親ブロック情報が保存されます。
このキャッシュを使えば、後から**API 呼び出し 0 回・トークン不要**で任意のサブツリーや別条件の CSV を即座に切り出せます。

```bash
# 保存済みキャッシュから特定サブツリーだけを切り出す
.venv/bin/python plan_tree.py --from-cache reports/tree_plan/api_search_cache.json --root-page-id 11111111-1111-1111-1111-111111111111 --exclude-db-rows --output-dir reports/subtree_a

# 同じキャッシュから全体（DB行あり）を再出力する
.venv/bin/python plan_tree.py --from-cache reports/tree_plan/api_search_cache.json --output-dir reports/tree_plan_full
```

---

## 4. コマンドラインオプション一覧

※ `--api`、`--zip`、`--zip-dir`、`--from-cache` のいずれか 1 つを必ず指定してください（排他指定）。

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `--api` | - | Notion API (`POST /v1/search`) でツリーを調査するモード |
| `--zip <PATH ...>` | - | Notion HTML エクスポート ZIP ファイルのパス（複数指定可） |
| `--zip-dir <DIR>` | - | 複数の Notion HTML エクスポート ZIP が格納されたディレクトリ |
| `--from-cache <PATH>` | - | 保存済みの `api_search_cache.json` から API 呼び出し 0 回でツリー／サブツリーを抽出するモード |
| `--notion-token <TOKEN>` | 環境変数 `NOTION_TOKEN` / `NOTION_API_KEY` | (`--api` 時) Notion Integration Token |
| `--root-page-id <UUID>` | なし（全体） | (`--api` / `--from-cache` / `--zip` / `--zip-dir` 時) 調査対象とするルートページ/DB の UUID |
| `--subtree-only` | `False` | (`--api` + `--root-page-id` 時) `POST /v1/search` を使わず `blocks/{id}/children` でサブツリー内のみを直接探索（DB行には入らない高速モード） |
| `--rate-limit <FLOAT>` | `2.8` | (`--api` 時) 1秒あたりの最大 API リクエスト数 |
| `--cache-file <PATH>` | `<output-dir>/api_search_cache.json` | (`--api` 時) API 検索結果の中間キャッシュ JSON パス |
| `--resume` | `False` | (`--api` 時) キャッシュファイルから中断箇所を再開する |
| `--output-dir <DIR>` | `reports/tree_plan` | `pages.csv` と `summary.md` の出力先ディレクトリ |
| `--exclude-db-rows` | `False` | `pages.csv` の明細行から DB 行レコード (`database_row`) を除外する（`db_row_count` 列と `summary.md` の実数は維持） |
| `-v`, `--verbose` | `False` | デバッグ用の詳細ログを出力する |

---

## 5. 出力ファイルの仕様

### 5.1 `pages.csv`（1ページ 1行・UTF-8-BOM）

Excel や Google スプレッドシートのオートフィルタ・ピボットテーブルがそのまま使えるよう、**サマリー行やヘッダ以外の装飾行は一切含みません**。

| 列名 | 内容 |
| :--- | :--- |
| `tree_order` | 深さ優先探索（DFS）の連番（ツリー順の並び替え用） |
| `node_id` | Notion UUID |
| `parent_id` | 親ノードの UUID（ルートは空欄） |
| `depth` | ルートからの階層の深さ（`0`, `1`, `2`...） |
| `node_type` | `page`（通常ページ） / `database`（DB本体） / `database_row`（DB行レコード） |
| `title` | ページ／データベースのタイトル |
| `path` | パンくず文字列（例: `全社ポータル > 開発ガイド > タスクDB`） |
| `level_1`, `level_2`, `level_3` | 第1〜第3階層のタイトル（親単位での集計・フィルタ用） |
| `direct_children_count` | 直下の子ノード数 |
| `subtree_node_count` | 配下にぶら下がる全ノード数（移行単位の規模見積もり用） |
| `db_row_count` | `database` の場合の保有レコード数（`--exclude-db-rows` 指定時も実数を出力） |
| `last_edited_time` | 最終更新日時（ISO8601。ZIP モード時は空欄の場合あり） |
| `archived` | アーカイブ / ゴミ箱状態（`true` / `false`） |
| `notion_url` | Notion 上の URL |
| `migration_action` | **（計画記入欄）** 例: `移行する`, `対象外`, `統合` |
| `phase` | **（計画記入欄）** 例: `Phase1`, `Phase2` |
| `owner` | **（計画記入欄）** 担当者 |
| `notes` | **（計画記入欄）** 備考・確認事項 |

### 5.2 `summary.md`（サマリーレポート）

コンソールにも同じ内容が出力されます。

1. **全体統計**: 出力ノード総数、種別ごとの件数（`page` / `database` / `database_row` 実数）、アーカイブ数、最大階層深さ
2. **階層深さ別分布**: `depth` ごとのノード数
3. **移行単位候補（第1階層 `level_1` 別）**: 総ノード数降順での `level_1` 配下の内訳（ページ数・DB数・DB行数・最終更新日）
4. **データベース一覧（行数降順 Top 20）**: 行数が多い大規模 DB のリスト（`db_triage` 検討用）

---

## 6. レートリミット対策と大規模ワークスペースでの運用 Tips

- **なぜ高速かつ低負荷か**:
  `--api` モードは `blocks/{id}/children` を全ページに対して再帰呼び出しせず、**`POST /v1/search`（`page_size=100`）で一括取得**してからメモリ上で親子関係（`parent`）を組み立てます。トグルやカラム等のブロック配下にある子ページ（`parent.type == "block_id"`）のみ、`GET /v1/blocks/{id}` で最小限に親ページを逆引きします。
- **`--exclude-db-rows` の活用**:
  レコード数が多い DB を含むワークスペースで移行計画表（`pages.csv`）を作りたい場合、`--exclude-db-rows` を付けると「ページ」と「DB本体」のみに絞られた見通しの良い一覧になります（DB の行数は `db_row_count` 列と `summary.md` で確認できます）。
- **429 エラーが出た場合**:
  ツール側で `Retry-After` ヘッダに従って自動待機・リトライしますが、頻繁に発生する場合は `--rate-limit 2.0` のようにリクエスト頻度を下げて実行してください。
- **中断・再開**:
  検索ページネーションの結果は `<output-dir>/api_search_cache.json` に逐次保存されます。途中で停止した場合は `--resume` を付けて再実行すると続きから再開します。

---

## 7. トラブルシューティング

- **`エラー: --api モードには --notion-token または環境変数 NOTION_TOKEN が必要です。`**
  - `.env` に `NOTION_TOKEN=...` を記載するか、`--notion-token` オプションで指定してください。
- **`Root page/database ID '...' not found in Notion search results.`**
  - 指定したルートページ（または親ページ）に Notion インテグレーションが接続されていない可能性があります。Notion 上の対象ページ右上のメニュー「接続（Connections）」からインテグレーションを追加してください。
- **`エラー: ディレクトリ '...' にZIPファイルが見つかりません。`**
  - `--zip-dir` で指定したディレクトリ直下に `.zip` ファイルが存在するか確認してください。
