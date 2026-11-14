# Notion to Confluence Migration Tool

NotionからConfluenceへ、ページ・階層ツリーおよびデータベース（DB）を移行するためのPythonツールです。

---

## 📁 ディレクトリ構成

```text
notion-to-confluence-mig/
├── src/
│   ├── extractor.py         # Notion REST API からのページ・ブロック・DBツリー再帰抽出モジュール
│   ├── html_extractor.py    # Notion 公式 HTML/CSV エクスポート (ZIP) からのオフライン抽出モジュール
│   ├── converter.py         # Notionブロック -> Confluence Storage Format (XHTML) 変換 (LaTeX数式画像化、近似ビュー)
│   ├── importer.py          # Confluence REST API経由でのページ・ネイティブDB・行ビュー作成モジュール
│   └── verifier.py          # 移行結果の自動突合・完全性検証モジュール
├── migrate.py               # 移行実行メインエントリポイント (REST API / HTML ZIP 対応)
├── visual_verifier.py       # Playwright による自動レンダリング撮影 & エラー診断ツール
├── requirements.txt         # 依存ライブラリ一覧
├── .env.example             # 環境変数テンプレート
├── SPECIFICATION.md         # 技術仕様書・制限事項・検証基準
└── README.md                # 本ドキュメント
```

---

## 🚀 使い方

### 1. 環境セットアップ
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境変数設定 (`.env`)
`.env.example` をコピーして `.env` を作成し、NotionとConfluenceの認証情報を設定します。

```bash
cp .env.example .env
```

```env
# Notion設定
NOTION_TOKEN=ntn_xxxx
NOTION_PARENT_PAGE_ID=xxxx

# Confluence設定
CONFLUENCE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_USERNAME=user@example.com
CONFLUENCE_API_TOKEN=xxxx
CONFLUENCE_SPACE_KEY=Notionmig
CONFLUENCE_PARENT_PAGE_ID=
```

### 3. 移行の実行

#### A. Notion HTML/CSV エクスポート (ZIP) からオフライン移行
Notion公式のエクスポート機能でダウンロードした ZIP ファイルから直接移行します（Notion API トークン不要）。
```bash
# 新しいページツリーとして移行（スペース直下に新規作成、デフォルトプレフィックス '[HTML] ' 付き）
python migrate.py --zip export.zip --new-tree

# タイトルプレフィックスを任意指定して移行
python migrate.py --zip export.zip --title-prefix "[Migrated] "
```

#### B. Notion REST API 経由でのオンライン移行
```bash
# 事前確認（Dry Run: Confluenceへ送信せず抽出ツリーをプレビュー）
python migrate.py --dry-run

# 本番移行実行
python migrate.py

# 特定のNotionページIDを指定して移行する場合
python migrate.py --notion-page-id <PAGE_ID>
```

実行後、対応するIDマップとURLが `migration_result.json` に出力されます。

> [!TIP]
> **Windows 環境での大規模移行時のご注意（Google Drive / OneDrive 等）**
> クラウド同期フォルダ配下で大規模移行を実行すると、チェックポイントファイル（`migration_checkpoint.json`）への保存時に同期クライアントやセキュリティソフトのファイルロック競合（`WinError 5` / `WinError 32`）が発生しやすくなります。実地運用では同期対象外のローカルフォルダで実行するか、以下のように `--checkpoint` 先を非同期のローカルパスへ指定することを推奨します。
> ```bash
> python migrate.py --zip export.zip --checkpoint C:\temp\migration_checkpoint.json
> ```

---

## 📋 機能仕様・近似対応と制限事項 (Parity & Limitations)

NotionとConfluenceのアーキテクチャ差分により、情報（テキスト・URL・データ構造）は欠落なく移行されるものの、Confluence上での表現や動作が異なる機能群について以下のように定義しています。

| Notion機能 | Confluence移行後の表現 | 移行仕様と制限事項 |
| :--- | :--- | :--- |
| **外部Web埋め込み** (`embed`, `bookmark`) | Atlassian Smart Links / URLリンク | リンクやタイトル情報は完全に保持されますが、Confluence Cloudのセキュリティ制約（CSP）上、一部の外部サービスではiframeによるインタラクティブ埋め込みではなくカード表示またはテキストリンクとなります。 |
| **同期ブロック** (`synced_block`) | 静的コンテンツ展開 | Confluenceにはブロック単位のリアルタイム双方向同期機能がないため、同期元ブロックの内容を各同期先ページへ静的に展開・配置します。情報は欠落しませんが、移行後にConfluence上で編集しても他ページへ自動同期はされません。 |
| **日付メンション** (`mention: date`) | 日付バッジ (`<time datetime="..." />`) | Confluence標準の日付バッジとして移行されます。Notionのリマインダー通知や動的な相対日付（「今日」等）は固定タイムスタンプとなります。 |
| **ページメンション** (`mention: page`) | ページリンク (`<ac:link>`) | 内部リンクとして移行されます。Confluenceでは「ページ情報 > 受信リンク」から逆参照可能ですが、Notionのようにページ本文上部にバックリンク一覧が自動展開されるUIとは異なります。 |
| **リストビュー** (`view: list`) | テーブルビュー (`table`) | Confluence Databasesにネイティブの「リスト」レイアウトが存在しないため（`table`, `board`, `gallery` の3種のみ）、最も近いテーブルビューとして近似マッピングされます。 |
| **Group by（グループ化）** | ハイブリッド複数ビュー展開 | Confluence Databasesには単一ビュー内Group byがないため、NotionのGroup by設定を検知してグループ別フィルタービューを自動作成し、未完了グループは常時展開見出し＋完了グループは折りたたみExpandマクロのハイブリッド構成として近似移植されます。 |

