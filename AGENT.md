# AGENT.md: Notion to Confluence 移行ツール 開発運用ポリシー & 制限事項

本ドキュメントは、本移行ツールの開発における重要要件、セキュリティ制約、設計思想、および運用ルールを定義・記録するものです。

---

## 1. プロジェクトの基本理念とミッション

- **公式移行ツール（Atlassian Notion Importer）を凌駕する高精度・高機能**:
  - 公式ツールの制限（数式がプレーンテキスト化される、一部添付ファイルや絵文字の欠落、インライン書式崩れなど）を克服し、Notion上の情報を忠実にConfluenceへ再現する。
- **文字情報の漏れなき移行（最重要原則）**:
  - 親ページ内のサブページリンク、トグル、コールアウト、表、コード、引用など、Notionページ内に含まれるあらゆるテキスト情報と構造を1文字も欠落させずに移行する。

---

## 2. 情報セキュリティ要件（外部サービス利用の完全禁止）

- **外部サービス・サードパーティAPIの利用禁止**:
  - 機密文書や社内ナレッジの安全性を担保するため、データ移行プロセスにおいて外部のWebサービス（外部数式レンダリングAPI、外部画像変換サービス、外部プロキシ等）の利用は**一切禁止**とする。
- **ローカル完結処理の徹底**:
  - 数式（LaTeX）のレンダリングは、ローカルPC内の `matplotlib` (mathtext) によりオフラインで高解像度PNGを生成し、Confluenceに添付ファイルとしてアップロード・埋め込みを行う。

---

## 3. 検証・品質保証ポリシー

### 3.1 テキストベース検証ツールの本体組み込み
- 移行ツール本体（CLIおよびライブラリ）には、**テキストベースの検証機能（`src/verifier.py`）**を組み込む。
- Notion側で抽出した文字情報、ブロック数、子ページ数、テーブル行数、チェックリスト、コードブロック等のデータと、Confluence APIから取得したStorage Format（XHTML）/ View HTMLを照合し、情報の欠落やマクロエラーを高速・軽量に自動検知する。

### 3.2 Playwright（ヘッドレスブラウザ）の利用制限
- Playwrightによる自動画面キャプチャ・視覚検証（`visual_verifier.py`）は、**開発時・デバッグ時の一時的な確認用途**に限定する。
- 移行ツール本体の通常実行プロセスやコア依存関係にはPlaywrightを含めない。

### 3.3 検証用ページの分離管理
- テスト検証時における無駄な再実行・再確認を防ぎ、テストサイクルを効率化するため、サンプルデータは明確に分離して管理する。
  1. **検証済み要素ページ**: 動作確認が完了した安定要素（見出し、リスト、テーブル、コード、トグル、数式、スマートリンク等）
  2. **未検証・新機能検証ページ**: これから開発・検証する新規要素（カバー画像、動画/音声、DBの高度プロパティ等）

---

## 4. アーキテクチャ設計原則

- **疎結合なパイプライン設計**:
  - `Extractor`（Notion APIからの抽出）
  - `Converter`（NotionブロックからConfluence Storage Format XHTMLへの変換）
  - `Importer`（Confluence REST APIへの階層作成・添付アップロード・プロパティ同期）
  - `Verifier`（NotionとConfluenceのテキスト・構造整合性検証）
- **HTMLエクスポート対応への拡張性**:
  - 将来「Notion APIが利用不可でHTMLエクスポートのみがある環境」にも対応できるよう、`NotionHtmlExtractor` を追加するだけで `Converter` や `Importer` を100%再利用できるデータモデル（`PageNode`）を堅持する。

---

## 5. Confluence固有の変換・レンダリング仕様

1. **数式（Equation）**:
   - Confluence Cloud標準ではLaTeXマクロが無効（エラー表示）となるため、ローカルPCで生成した300dpiの高解像度PNG画像を添付ファイル化し `<ac:image>` で配置。
2. **トグル見出し（Toggle Heading）**:
   - Expandマクロのtitle属性では見出し装飾が無効化されるため、見出しタグ（`<h1>`〜`<h3>`）を先行出力し、直下に折りたたみブロック（Expandマクロ）を配置する。
3. **子ページリンク / データベースリンク**:
   - `<ac:link ac:card-appearance="inline"><ri:page ri:content-title="..." /><ac:link-body>...</ac:link-body></ac:link>` によるスマートリンク（プレビュー対応）形式で出力。
4. **ページアイコン（Emoji）**:
   - ConfluenceのContent Properties（`emoji-title-published`, `emoji-title-draft`）にhexコードを設定して完全同期する。

---

## 6. プラットフォーム仕様差分に伴う近似対応と制限事項 (Limitations & Parity Notes)

NotionとConfluenceのアーキテクチャ・機能差分により、情報（テキスト・URL・データ）は欠落なく移行されるものの、表現や動作が異なる機能群について以下に仕様を記録する。

### 6.1 外部Web埋め込み（Embed / Bookmark / External Media）
- **仕様と移行動作**:
  - Notionの `embed`（外部iframe埋め込み）、`bookmark`（Webブックマーク）は、Confluence Cloudのセキュリティポリシー（CSP）および仕様に基づき、Atlassian Smart Links（`<a data-card-appearance="embed|block">`）または標準ハイパーリンクとして移行される。
- **制限事項**:
  - URLやテキスト情報は完全に保持されるが、Atlassian Smart Linksがインライン埋め込みに対応していないサービス（任意の外部Webサイト等）では、インタラクティブなiframeプレビューではなくカード形式やテキストリンクによるリンク表現となる。

### 6.2 同期ブロック（Synced Block）
- **仕様と移行動作**:
  - Notionの双方向同期ブロック（`synced_block`）は、Confluenceにブロック単位のリアルタイム双方向同期機能が存在しないため、**「同期元ブロックのコンテンツを各同期先ページへ静的展開・配置する」** アプローチを採用している。
- **制限事項**:
  - ページ内の情報やレイアウトは欠落なく完全に保持されるが、Confluence移行後にいずれかのページでそのブロックを編集しても、他のページに自動同期されることはない（静的展開の制限）。

### 6.3 メンション記法（Mention）
- **日付メンション**:
  - Confluenceの `<time datetime="YYYY-MM-DD" />` タグに変換され、閲覧時はConfluence標準の日付バッジとして表示される。ただし、Notionのリマインダー通知や動的な相対日付（「今日」「明日」等）は引き継がれず、移行時点の固定日時タイムスタンプとなる。
- **ページメンション（双方向バックリンク）**:
  - Confluenceの `<ac:link><ri:page ri:content-title="..." /></ac:link>` 形式に変換される。Confluence側でもページ詳細メニューの「受信リンク（Incoming Links）」からリンク元を辿ることができるが、Notionのようにページ本文上部にバックリンク一覧が自動インライン展開されるUIとは異なる。

### 6.4 データベース各種ビュー・グループ化（Database Views & Group by）
- **リストビュー（List View）**:
  - Confluence Databasesがサポートするネイティブレイアウトは `table`（表）、`board`（カンバン）、`gallery`（カード）の3種類のみであり、専用の `list` レイアウトは存在しない。そのため、NotionのリストビューはConfluence Databasesのテーブルビュー（`table`）として近似マッピングされる。
- **Group by（グループ化）**:
  - Confluence Databasesには単一ビュー内でのGroup by機能が存在しない。本ツールではNotionの `group_by` 設定を自動検出し、グループ値ごとのフィルタービューを自動プロビジョニングした上で、進行中・未着手グループを常時展開見出し（方式B）＋完了グループを折りたたみExpandマクロ（方式A）として組み合わせる**「ハイブリッド近似移植」** を自動生成する。


