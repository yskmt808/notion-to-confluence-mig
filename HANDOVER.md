# 引き継ぎドキュメント (Handover Document)

作成日時: 2026-11-11 01:32 JST
対象リポジトリ: `/Users/yskmt/notion-to-confluence-mig`

---

## 1. 対応対象の4要件と現在の進捗ステータス

| # | 要件内容 | 状態 | 概要 |
|---|---|---|---|
| 1 | **箇条書き中に配置されるコールアウト要素のフォーマット対応** | **抽出・変換実装済み / 残: ブロック色伝播・単体テスト・E2E移行実行** | `src/html_extractor.py` に `_extract_list_item_content()` を実装し、`ul.bulleted-list` / `ol.numbered-list` / `ul.to-do-list` 内の `<li>` 直下および `.indented` 配下のコールアウト (`<aside class="callout">`)・ネストされたリスト等を親テキストに混入させず `children_blocks` として分離抽出するよう修正済み。Confluence Cloud 実機APIにて `<li>` および `<ac:task-body>` 内の `<ac:structured-macro ac:name="panel">` が ADF / View 双方で保持・描画されることを検証済み。 |
| 2 | **コールアウト内の埋め込み画像対応（コールアウト外に出ないよう修正）** | **抽出・変換実装済み / 残: 単体テスト・E2E移行実行** | `src/html_extractor.py` の `# 8. コールアウト` に `_extract_callout_content()` を実装し、コールアウトのメインテキストと内部の子ブロック（`<figure class="image">`, `<p>`, リスト等）を分離抽出して `children_blocks` に格納するよう修正済み。Confluence Cloud 実機APIにて `<ac:structured-macro ac:name="panel">` の `<ac:rich-text-body>` 内に `<p><ac:image><ri:attachment .../></ac:image></p>` を配置した際、ADF (`panel` -> `mediaSingle`) および `body.view` (`.panelContent` 内 `<img>`) の両方で画像がコールアウト内部に保持されることを検証済み。 |
| 3 | **文字の背景色対応** | **インライン実装済み / 残: ブロック単位背景色 (`block.color`) の `src/converter.py` 反映** | `src/html_extractor.py` の `_parse_inline_rich_text()` で `data-notion-highlight` に加え `class="highlight-*_background"` / `class="block-color-*_background"` および Notion HTML 特有の `teal` → `green` / `teal_background` → `green_background` 正規化を実装済み。`src/converter.py` のカラーマップにも `teal` / `teal_background` を追加済み。**残作業**: Notion API モード等で段落・見出し・リスト項目自体のブロック属性 `block[b_type]["color"]` に `*_background` が設定されている場合（`rich_text` 側の `annotations.color` が `"default"` の場合）に `src/converter.py` 側でも背景色 `<span>` を適用する処理を追加する。 |
| 4 | **試験用Notionページ作成と移行テスト実行（API & ZIP）** | **未完了（次セッションで実行）** | `.env` の `NOTION_TOKEN` を用いて上記 1〜3 を網羅した試験用 Notion ページを作成し、`migrate.py` による API モード移行および HTML エクスポート ZIP モード移行の実行・`MigrationVerifier` 検証・`tests/` 単体テスト追加を行う。 |

---

## 2. 調査結果・根本原因と Confluence Cloud 実機検証結果

### 2.1 要件1: 箇条書き中のコールアウト要素
- **根本原因 (`src/html_extractor.py`)**:
  - Notion の HTML エクスポートでは、`ul.to-do-list > li` は子要素を `<div class="indented">` に入れる一方、`ul.bulleted-list > li` と `ol.numbered-list > li` は子ブロック（`<aside class="callout">`, `<ul>`, `<ol>` 等）を **`.indented` を介さず `<li>` 直下** に出力する。
  - 旧実装は `li.find(class_="indented")` のみを子ブロックとして探し、`self._parse_inline_rich_text(li, skip_classes=["indented"])` で `<li>` 全体を走査していたため、`<li>` 直下の `<aside class="callout">` の枠組みが失われ、コールアウト内テキストが箇条書きテキストにベタ書き結合されていた。
- **Confluence Cloud 実機検証結果**:
  - `<li>` 内および `<ac:task-body>` 内に `<ac:structured-macro ac:name="panel"><ac:parameter ac:name="bgColor">...</ac:parameter><ac:parameter ac:name="borderColor">...</ac:parameter><ac:rich-text-body>...</ac:rich-text-body></ac:structured-macro>` を配置して Confluence Cloud に PUT し、`expand="body.storage,body.atlas_doc_format,body.view"` で検証を実施。
  - `body.view` では `<li>` 内に `<div class="panel conf-macro output-block" style="background-color: ...; border-color: ...; border-width: 1px;">` が描画され、`body.atlas_doc_format` (ADF) でも `nestedContent` 内の `panel` ノードとして欠落せず保持されることを確認済み。

### 2.2 要件2: コールアウト内の埋め込み画像
- **根本原因 (`src/html_extractor.py`)**:
  - 旧実装の `# 8. コールアウト` (`src/html_extractor.py` 1153行付近) は `self._parse_inline_rich_text(el, skip_classes=["icon"])` のみを呼び出しており、`<aside class="callout">` 内部の `<div style="width:100%">` 等に含まれる `<figure class="image">` や追加 `<p>` 等の子ブロックを一切抽出していなかった（`children_blocks` が未設定だった）。
- **Confluence Cloud 実機検証結果**:
  - `src/converter.py` の `callout` 変換（717〜743行）は `b.get("children_blocks")` を `<ac:rich-text-body><p>{full_text}</p>{children_html}</ac:rich-text-body>` として `<ac:structured-macro ac:name="panel">` の内部に出力する。
  - `src/importer.py` の `_process_attachments()`（1256行）も再帰的に `b["children_blocks"]` の画像をアップロードする実装になっている。
  - Confluence Cloud 実機ページ (`622610`) にて、添付ファイル `image.png` を `<ac:structured-macro ac:name="panel">` 内に配置して検証した結果、ADF でも `panel.content` 内の `mediaSingle` として保持され、`body.view` でも `<div class="panelContent">` 内部に `<img>` が表示されることを確認済み。

### 2.3 要件3: 文字の背景色
- **根本原因 (`src/html_extractor.py` & `src/converter.py`)**:
  1. **HTML エクスポート抽出時**:
     - 旧 `_parse_inline_rich_text()` は `<mark data-notion-highlight="...">` 属性のみを参照していた。Notion HTML エクスポートでは `class="highlight-yellow_background"`（または緑背景が `highlight-teal_background` / `block-color-teal_background`）としてクラス名で出力されるケースや、段落・リスト自体に `class="block-color-*_background"` が付与されるケースがあり、これらが抽出漏れていた。
  2. **ブロック単位のカラー設定 (`src/converter.py`)**:
     - Notion でテキスト一部分のハイライトではなく「ブロック全体（段落・見出し・リスト項目等）」の背景色を変更した場合、Notion API では `rich_text[].annotations.color` は `"default"` のままで、ブロック直下の `block[b_type]["color"]`（例: `paragraph.color = "yellow_background"`）に色が入る。
     - 現在 `src/converter.py` は `callout` 以外のブロック（`paragraph`, `heading_1..3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, `toggle`）のブロックレベル `color` を参照していないため、ブロック背景色が反映されない。
- **Confluence Cloud 実機検証結果**:
  - `src/converter.py` が生成する `<span style="background-color: #fbf3db; padding: 2px 4px; border-radius: 3px;">テキスト</span>` を Confluence Cloud に送信すると、Confluence Cloud の Storage-to-ADF コンバータがネイティブの ADF `backgroundColor` マーク (`{"type": "backgroundColor", "attrs": {"color": "#fbf3db"}}`) に変換し、`body.storage` / `body.view` でも `<span style="background-color: rgb(251,243,219);">` として9色すべて保持されることを実機ページ (`622610`) で確認済み。

---

## 3. 変更済みファイル（ワーキングツリー内・未コミット）

1. **[src/html_extractor.py](file:///Users/yskmt/notion-to-confluence-mig/src/html_extractor.py)**
   - `# 3. 箇条書きリスト`, `# 4. 番号付きリスト`, `# 5. ToDoリスト` (1060〜1111行): `_extract_list_item_content()` を呼び出して `<li>` 自身の `rich_text` と配下の子ブロック `children_blocks` を分離。
   - `# 8. コールアウト` (1153〜1191行): `_extract_callout_content()` を呼び出してメインテキスト `callout_rich_text` と内部の埋め込み画像・段落等の `callout_children` (`children_blocks`) を分離。
   - `_is_block_tag()` (1510〜1537行): ブロック要素判定ヘルパーを追加。
   - `_extract_list_item_content()` (1539〜1612行): `<li>` からインライン要素と子ブロック要素（`.indented` 内および `<li>` 直下の `<aside>`, `<figure>`, `<ul>`, `<ol>` 等）を分離抽出するメソッドを追加。
   - `_extract_callout_content()` (1614〜1702行): `<aside class="callout">`（アイコン用 `div` + コンテンツ用 `div` 構造を含む）から先頭テキストと子ブロック（`<figure class="image">`, 追加 `<p>`, リスト等）を分離抽出するメソッドを追加。
   - `_parse_inline_rich_text()` (1704〜1797行):
     - 子ブロックタグ (`self._is_block_tag(child)`) の混入防止ガードを追加。
     - `data-notion-highlight` に加え、`class="highlight-*"` および `class="block-color-*"` からの色抽出と `teal` → `green` / `teal_background` → `green_background` の正規化を追加。

2. **[src/converter.py](file:///Users/yskmt/notion-to-confluence-mig/src/converter.py)**
   - `NOTION_TEXT_COLOR_MAP`, `NOTION_BG_COLOR_MAP`, `CALLOUT_PANEL_COLORS` (62〜105行): `teal` および `teal_background` のマッピングを追加。

3. **[SPECIFICATION.md](file:///Users/yskmt/notion-to-confluence-mig/SPECIFICATION.md)**
   - 仕様書への関連記載の差分あり（`git diff SPECIFICATION.md` で確認可能）。

---

## 4. 次セッションで実施すべき残タスク（再開手順）

### Step 1: ブロック単位の背景色・文字色 (`block-color-*` / API `block[type].color`) と引用ブロックの仕上げ
1. **`src/converter.py`**:
   - `convert_rich_text(self, rich_text_list, default_color="default")` のようにブロックレベルのデフォルト色を受け取れるようにするか、各ブロック（`paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, `toggle`）で `b.get(b_type, {}).get("color", "default")` を取得し、アイテム側の `annotations.color` が `"default"` の場合にブロックの `color` を適用するようにする。
   - また、`convert_rich_text` 内でリンク (`<a href=...>` / `<ac:link>`) と背景色 (`NOTION_BG_COLOR_MAP`) が同時に指定された場合も背景色 `<span>` が維持されることを確認する。
2. **`src/html_extractor.py`**:
   - `_extract_list_item_content()` で親の `<li>`（および親 `<ul>`/`<ol>`）に `block-color-*` クラス等が付いている場合、`inline_container` にクラスを引き継ぐかブロック辞書の `"color"` に保存する。
   - `# 7. 引用` (`blockquote`, 1138〜1150行) でも `_extract_list_item_content()` を使用し、`<blockquote>` 内の子 `<p>` や `<ul>` が `quote.rich_text` に重複混入しないようにする。
   - `_extract_callout_content()` で、コールアウト内に画像のみがあり先頭 `<p>` がない場合（または `<p>` の中に `<img>` / `<figure>` が入っている場合）でも `callout.rich_text` が空になった際に `src/converter.py` 側で不要な空 `<p></p>` を出さないか、自然に表示されるかを確認する。
3. **`src/verifier.py`**:
   - `verify_page_node()` の `block_types` チェック（90〜147行）およびテキスト網羅性チェック（200〜216行）がトップレベルの `node.blocks` のみを見ている箇所について、`children_blocks`（リスト内のコールアウトやコールアウト内の画像・テキスト）も再帰的に走査できるよう補助関数でフラット化して検証するようにしておくとより堅牢になる。

### Step 2: 単体テストの追加と実行
- `tests/` 配下（例: `tests/test_callout_list_image_bgcolor.py` または既存テストファイル）に以下の単体テストを追加する:
  1. `HtmlZipExtractor`:
     - `ul.bulleted-list > li`, `ol.numbered-list > li`, `ul.to-do-list > li` 内の `<aside class="callout">` が `children_blocks` に抽出され、`li` の `rich_text` に混入しないこと。
     - `<aside class="callout">` 内の `<figure class="image">`（および後続 `<p>`）が `callout` の `children_blocks` に抽出され、ZIP内画像バイナリ (`_attachment_bytes`) が正しく紐づくこと。
     - `<mark class="highlight-*_background">`, `<span class="highlight-teal_background">`, `<p class="block-color-yellow_background">` 等から全9色の背景色が抽出されること。
  2. `NotionToConfluenceConverter`:
     - リスト項目内の `callout` が `<li>...<ac:structured-macro ac:name="panel">...</ac:structured-macro></li>` に変換されること。
     - `callout` 内の `image` が `<ac:structured-macro ac:name="panel">...<ac:rich-text-body><p>...</p><p><ac:image><ri:attachment .../></ac:image></p></ac:rich-text-body></ac:structured-macro>` とパネル内部に変換されること。
     - 全9色の背景色（インラインおよびブロック単位）が `<span style="background-color: ...">` に変換されること。
- 実行コマンド:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```

### Step 3: 試験用 Notion ページの作成と E2E 移行テスト実行（要件4）
1. **試験用 Notion ページの作成**:
   - `.env` の `NOTION_TOKEN` を使用し、`エラーパターン` (`4137be77-284d-8041-89c8-d080073331b4`) 配下（または `エラーパターン子ページ` `4137be77-284d-801d-9fcf-c13c0989f376` / 新規子ページ）に以下の要素を含むテストページを Notion API で作成する:
     - 箇条書きリスト (`bulleted_list_item`)・番号付きリスト (`numbered_list_item`)・ToDoリスト (`to_do`) の子ブロックとして配置されたコールアウト (`callout`)
     - コールアウト (`callout`) の子ブロックとして配置された埋め込み画像 (`image`) とテキスト
     - 全9色の背景色（`gray_background`, `brown_background`, `orange_background`, `yellow_background`, `green_background`, `blue_background`, `purple_background`, `pink_background`, `red_background`）を持つリッチテキストおよびブロック
2. **ライブ Notion API からの移行実行と検証**:
   ```bash
   .venv/bin/python migrate.py --page-id 4137be77-284d-8041-89c8-d080073331b4
   ```
3. **HTML エクスポート ZIP からの移行実行と検証**:
   - 既存の `error_pattern_ExportBlock-71f6398d-14b2-410d-bb3a-36db77766366.zip` を用いた移行を実行し（テスト中に書き換えた Confluence ページ `622610` `[HTML] エラーパターン` および `720906` `[HTML] エラーパターン子ページ` もこれにより正常な移行状態に復元される）、さらに上記3パターンを含む試験用 HTML エクスポート ZIP でも移行・検証を行う:
   ```bash
   .venv/bin/python migrate.py --zip error_pattern_ExportBlock-71f6398d-14b2-410d-bb3a-36db77766366.zip --title-prefix "[HTML] "
   ```
4. **Confluence Cloud 上の `body.storage` / `body.atlas_doc_format` / `body.view` 最終確認**:
   - 移行された Confluence ページで `MigrationVerifier` が `PASS` になること、および `body.view` / `body.atlas_doc_format` でリスト内コールアウト・コールアウト内画像・9色すべての文字背景色が正しく出力されていることを確認する。

