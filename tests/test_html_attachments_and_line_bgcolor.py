import os
import io
import tempfile
import zipfile
import unittest
from unittest.mock import MagicMock

from src.html_extractor import HtmlZipExtractor
from src.converter import NotionToConfluenceConverter
from src.importer import detect_attachment_mime_type
from src.verifier import MigrationVerifier
from src.extractor import PageNode
from storage_xml_helper import assert_valid_storage_xml


class TestHtmlAttachmentsAndLineBgColor(unittest.TestCase):
    """HTMLエクスポート内の添付ファイル(pdf, ppt等)の処理・検証、
    およびNotion行単位背景色のConfluence Storage Format変換テスト。
    """

    def setUp(self):
        self.converter = NotionToConfluenceConverter()

    def test_importer_mime_type_detection(self):
        """各種拡張子のMIMEタイプが正しく判定されること。"""
        self.assertEqual(detect_attachment_mime_type("sample.pdf"), "application/pdf")
        self.assertEqual(detect_attachment_mime_type("slides.ppt"), "application/vnd.ms-powerpoint")
        self.assertEqual(detect_attachment_mime_type("deck.pptx"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertEqual(detect_attachment_mime_type("doc.docx"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(detect_attachment_mime_type("sheet.xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(detect_attachment_mime_type("archive.zip"), "application/zip")
        self.assertEqual(detect_attachment_mime_type("image.png"), "image/png")
        self.assertEqual(detect_attachment_mime_type("photo.jpeg"), "image/jpeg")

    def test_repro_html_extractor_skips_pdf_and_ppt_attachments(self):
        """HTMLエクスポート内のPDFやPPTX等の添付ファイルがZIP内からバイナリ抽出されること。"""
        html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Attachment Test</title></head>
<body>
  <article id="page-12345678-1234-1234-1234-123456789abc" class="page sans">
    <header><h1 class="page-title">Attachment Test</h1></header>
    <div class="page-body">
      <figure id="block-pdf-1" class="file">
        <a href="sample.pdf">Sample PDF Document</a>
      </figure>
      <figure id="block-ppt-2" class="file">
        <a href="presentation.pptx">Presentation Slides</a>
      </figure>
    </div>
  </article>
</body>
</html>"""
        pdf_bytes = b"%PDF-1.4 dummy pdf bytes"
        pptx_bytes = b"PK dummy pptx bytes"

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
            tmp_zip_path = tmp_zip.name

        try:
            with zipfile.ZipFile(tmp_zip_path, "w") as zf:
                zf.writestr("test_page.html", html_content)
                zf.writestr("sample.pdf", pdf_bytes)
                zf.writestr("presentation.pptx", pptx_bytes)

            extractor = HtmlZipExtractor(tmp_zip_path)
            tree = extractor.extract_tree()
            self.assertIsNotNone(tree)
            self.assertEqual(len(tree.blocks), 2)

            pdf_block = tree.blocks[0]
            self.assertEqual(pdf_block["type"], "pdf")
            self.assertEqual(pdf_block.get("_attachment_filename"), "sample.pdf")
            self.assertEqual(pdf_block.get("_attachment_bytes"), pdf_bytes)

            pptx_block = tree.blocks[1]
            self.assertEqual(pptx_block["type"], "file")
            self.assertEqual(pptx_block.get("_attachment_filename"), "presentation.pptx")
            self.assertEqual(pptx_block.get("_attachment_bytes"), pptx_bytes)
        finally:
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

    def test_repro_verifier_fails_when_attachment_missing_in_confluence(self):
        """Confluence側に添付ファイルが存在しない場合、検証がFAILになること。"""
        mock_client = MagicMock()
        # Confluenceのページ取得
        mock_client.get_page_by_id.return_value = {
            "id": "conf-page-123",
            "title": "Attachment Page",
            "body": {
                "storage": {
                    "value": '<p><ac:link><ri:attachment ri:filename="presentation.pptx" /></ac:link></p>'
                }
            },
        }
        # 添付ファイル一覧: presentation.pptx が存在しない (空リスト)
        mock_client.get_page_attachments.return_value = []

        verifier = MigrationVerifier(mock_client)
        node = PageNode(
            id="page-123",
            title="Attachment Page",
            node_type="page",
            blocks=[
                {
                    "id": "b1",
                    "type": "file",
                    "_attachment_filename": "presentation.pptx",
                    "file": {"file": {"url": "presentation.pptx"}},
                }
            ],
        )

        res = verifier.verify_page_node(node, "conf-page-123")
        # 添付ファイルがConfluenceに無いため FAIL になるべき
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("presentation.pptx" in issue for issue in res["issues"]))

    def test_verifier_passes_when_attachment_present_in_confluence(self):
        """Confluence側に添付ファイルが存在する場合、検証がPASSすること。"""
        mock_client = MagicMock()
        mock_client.get_page_by_id.return_value = {
            "id": "conf-page-123",
            "title": "Attachment Page",
            "body": {
                "storage": {
                    "value": '<p><ac:link><ri:attachment ri:filename="presentation.pptx" /></ac:link></p>'
                }
            },
        }
        mock_client.get_page_attachments.return_value = [
            {"title": "presentation.pptx", "id": "att-1"}
        ]

        verifier = MigrationVerifier(mock_client)
        node = PageNode(
            id="page-123",
            title="Attachment Page",
            node_type="page",
            blocks=[
                {
                    "id": "b1",
                    "type": "file",
                    "_attachment_filename": "presentation.pptx",
                    "file": {"file": {"url": "presentation.pptx"}},
                }
            ],
        )

        res = verifier.verify_page_node(node, "conf-page-123")
        self.assertEqual(res["status"], "PASS")
        self.assertTrue(any("presentation.pptx" in check for check in res["checks_passed"]))

    def test_repro_html_extractor_block_level_background_color(self):
        """HTMLエクスポートでblock-color-{color}が付与されたブロックの背景色がブロックのcolorとして抽出されること。"""
        html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Color Test</title></head>
<body>
  <article id="page-12345678-1234-1234-1234-123456789abc" class="page sans">
    <div class="page-body">
      <p id="b1" class="block-color-yellow_background">Line with yellow background</p>
      <h1 id="b2" class="block-color-blue_background">Heading with blue background</h1>
      <blockquote id="b3" class="block-color-purple_background">Quote with purple background</blockquote>
      <p id="b4" class="block-color-yellow_background">Normal <mark class="highlight-pink_background">pink text</mark> in yellow</p>
    </div>
  </article>
</body>
</html>"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
            tmp_zip_path = tmp_zip.name

        try:
            with zipfile.ZipFile(tmp_zip_path, "w") as zf:
                zf.writestr("test_color.html", html_content)

            extractor = HtmlZipExtractor(tmp_zip_path)
            tree = extractor.extract_tree()
            self.assertEqual(len(tree.blocks), 4)

            # b1
            b1 = tree.blocks[0]
            self.assertEqual(b1["type"], "paragraph")
            self.assertEqual(b1["paragraph"].get("color"), "yellow_background")
            # 本文のrich_textアノテーションはdefaultであること (行全体が黄色背景のため)
            self.assertEqual(b1["paragraph"]["rich_text"][0]["annotations"]["color"], "default")

            # b2
            b2 = tree.blocks[1]
            self.assertEqual(b2["type"], "heading_1")
            self.assertEqual(b2["heading_1"].get("color"), "blue_background")

            # b3
            b3 = tree.blocks[2]
            self.assertEqual(b3["type"], "quote")
            self.assertEqual(b3["quote"].get("color"), "purple_background")

            # b4
            b4 = tree.blocks[3]
            self.assertEqual(b4["paragraph"].get("color"), "yellow_background")
            rt = b4["paragraph"]["rich_text"]
            self.assertEqual(rt[0]["annotations"]["color"], "default")
            self.assertEqual(rt[1]["annotations"]["color"], "pink_background")
        finally:
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

    def test_repro_converter_line_level_background_color_rendering(self):
        """Notionで行単位背景色が設定されたブロックがConfluence panelマクロとして出力され、重複spanが生成されないこと。"""
        blocks = [
            {
                "type": "paragraph",
                "paragraph": {
                    "color": "yellow_background",
                    "rich_text": [{"type": "text", "text": {"content": "Line with yellow background"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "heading_1",
                "heading_1": {
                    "color": "blue_background",
                    "rich_text": [{"type": "text", "text": {"content": "Heading blue"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "quote",
                "quote": {
                    "color": "purple_background",
                    "rich_text": [{"type": "text", "text": {"content": "Quote purple"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "color": "green_background",
                    "rich_text": [{"type": "text", "text": {"content": "Bullet green"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "numbered_list_item",
                "numbered_list_item": {
                    "color": "red_background",
                    "rich_text": [{"type": "text", "text": {"content": "Numbered red"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "to_do",
                "to_do": {
                    "color": "orange_background",
                    "checked": False,
                    "rich_text": [{"type": "text", "text": {"content": "Todo orange"}, "annotations": {"color": "default"}}],
                },
            },
            {
                "type": "paragraph",
                "paragraph": {
                    "color": "yellow_background",
                    "rich_text": [
                        {"type": "text", "text": {"content": "Normal "}, "annotations": {"color": "default"}},
                        {"type": "text", "text": {"content": "same yellow"}, "annotations": {"color": "yellow_background"}},
                        {"type": "text", "text": {"content": " and "}, "annotations": {"color": "default"}},
                        {"type": "text", "text": {"content": "pink"}, "annotations": {"color": "pink_background"}},
                    ],
                },
            },
        ]

        html = self.converter.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, html)

        # 段落全体が panel マクロ（yellow: bgColor=#fbf3db, borderColor=#f4e6b5）で出力されること
        self.assertIn(
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#fbf3db</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#f4e6b5</ac:parameter>'
            '<ac:rich-text-body><p>Line with yellow background</p></ac:rich-text-body>'
            '</ac:structured-macro>',
            html,
        )
        # インライン CSS style や同一背景色 span が生成されないこと
        self.assertNotIn('<p style="background-color:', html)
        self.assertNotIn('<h1 style="background-color:', html)
        self.assertNotIn('<blockquote style="background-color:', html)
        self.assertNotIn('<li style="background-color:', html)
        self.assertNotIn('<span style="background-color: #fbf3db;', html)

        # 見出し全体が panel マクロ（blue: #e7f3f8 / #dbeaf6）で出力されること
        self.assertIn(
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#e7f3f8</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#dbeaf6</ac:parameter>'
            '<ac:rich-text-body><h1>Heading blue</h1></ac:rich-text-body>'
            '</ac:structured-macro>',
            html,
        )

        # 引用全体が panel マクロ（purple: #f4f0f7 / #eae3f2）で出力されること
        self.assertIn(
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#f4f0f7</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#eae3f2</ac:parameter>'
            '<ac:rich-text-body><blockquote><p>Quote purple</p></blockquote></ac:rich-text-body>'
            '</ac:structured-macro>',
            html,
        )

        # 箇条書き・番号付きリストが <ul>/<ol> -> <li> -> panel マクロの構造で出力されること
        self.assertIn(
            '<ul><li><ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#edf3ec</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#dbe8d9</ac:parameter>'
            '<ac:rich-text-body><p>Bullet green</p></ac:rich-text-body>'
            '</ac:structured-macro></li></ul>',
            html,
        )
        self.assertIn(
            '<ol><li><ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#fdebec</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#f9d3d6</ac:parameter>'
            '<ac:rich-text-body><p>Numbered red</p></ac:rich-text-body>'
            '</ac:structured-macro></li></ol>',
            html,
        )

        # ToDo が <ac:task-list> ごと panel マクロ（orange: #faece3 / #f4d8be）で出力されること
        self.assertIn(
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="bgColor">#faece3</ac:parameter>'
            '<ac:parameter ac:name="borderColor">#f4d8be</ac:parameter>'
            '<ac:rich-text-body><ac:task-list><ac:task><ac:task-status>incomplete</ac:task-status>'
            '<ac:task-body>Todo orange</ac:task-body></ac:task></ac:task-list></ac:rich-text-body>'
            '</ac:structured-macro>',
            html,
        )

        # 親と異なるインラインハイライト (pink_background) は保持され、同一背景 (yellow_background) の重複spanは抑止されること
        self.assertIn('<p>Normal same yellow and <span style="background-color: #fbf0f4;', html)

    def test_html_extractor_ignores_root_data_notion_highlight_and_converts_to_panel(self):
        """data-notion-highlight付きブロック抽出時にrich_textへ重複伝播せず、panelマクロへ変換されること。"""
        html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Highlight Dedup Test</title></head>
<body>
  <article id="page-12345678-1234-1234-1234-123456789abc" class="page sans">
    <div class="page-body">
      <p id="p1" class="block-color-yellow_background" data-notion-highlight="yellow_background">Paragraph both attrs</p>
      <p id="p2" data-notion-highlight="blue_background">Paragraph data attr only with <mark class="highlight-pink_background">pink mark</mark></p>
      <h2 id="h2" data-notion-highlight="teal_background">Heading teal attr</h2>
      <ul class="bulleted-list">
        <li id="li1" class="block-color-purple_background" data-notion-highlight="purple_background">List item with <span data-notion-highlight="green_background">green inline</span></li>
      </ul>
      <blockquote id="q1" data-notion-highlight="red_background">Quote red attr</blockquote>
    </div>
  </article>
</body>
</html>"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
            tmp_zip_path = tmp_zip.name

        try:
            with zipfile.ZipFile(tmp_zip_path, "w") as zf:
                zf.writestr("highlight_dedup.html", html_content)

            tree = HtmlZipExtractor(tmp_zip_path).extract_tree()
            self.assertEqual(len(tree.blocks), 5)

            # p1: ブロック色は抽出され、rich_text側は default のまま
            self.assertEqual(tree.blocks[0]["paragraph"]["color"], "yellow_background")
            self.assertEqual(tree.blocks[0]["paragraph"]["rich_text"][0]["annotations"]["color"], "default")

            # p2: data-notion-highlight のみでもブロック色として抽出され、内部 <mark> だけがハイライト保持される
            self.assertEqual(tree.blocks[1]["paragraph"]["color"], "blue_background")
            self.assertEqual(tree.blocks[1]["paragraph"]["rich_text"][0]["annotations"]["color"], "default")
            self.assertEqual(tree.blocks[1]["paragraph"]["rich_text"][1]["annotations"]["color"], "pink_background")

            # h2: teal_background -> green_background 正規化、rich_text側は default
            self.assertEqual(tree.blocks[2]["heading_2"]["color"], "green_background")
            self.assertEqual(tree.blocks[2]["heading_2"]["rich_text"][0]["annotations"]["color"], "default")

            # li1: purple_background が親、子 span の green_background のみインライン保持
            self.assertEqual(tree.blocks[3]["bulleted_list_item"]["color"], "purple_background")
            self.assertEqual(tree.blocks[3]["bulleted_list_item"]["rich_text"][0]["annotations"]["color"], "default")
            self.assertEqual(tree.blocks[3]["bulleted_list_item"]["rich_text"][1]["annotations"]["color"], "green_background")

            # q1: quote red_background
            self.assertEqual(tree.blocks[4]["quote"]["color"], "red_background")
            self.assertEqual(tree.blocks[4]["quote"]["rich_text"][0]["annotations"]["color"], "default")

            # Storage Format 変換検証
            storage = self.converter.blocks_to_storage_format(tree.blocks)
            assert_valid_storage_xml(self, storage)
            self.assertNotIn('<span style="background-color: #fbf3db;', storage)
            self.assertNotIn('<span style="background-color: #e7f3f8;', storage)
            self.assertNotIn('<span style="background-color: #f4f0f7;', storage)
            self.assertNotIn('<span style="background-color: #fdebec;', storage)
            self.assertIn('<span style="background-color: #fbf0f4;', storage)
            self.assertIn('<span style="background-color: #edf3ec;', storage)
            self.assertEqual(storage.count('<ac:structured-macro ac:name="panel">'), 5)
        finally:
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

    def test_error_pattern_zip_blocks_convert_to_valid_panels_without_inline_dup(self):
        """error_pattern_ExportBlock.zip 内の色付きブロックが整形式panelマクロに変換され、重複spanがないこと。"""
        zip_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "error_pattern_ExportBlock.zip")
        if not os.path.exists(zip_path):
            self.skipTest("error_pattern_ExportBlock.zip not found")

        tree = HtmlZipExtractor(zip_path).extract_tree()
        all_nodes = []

        def _walk(node):
            all_nodes.append(node)
            for c in node.children:
                _walk(c)

        _walk(tree)
        self.assertGreater(len(all_nodes), 0)

        panel_count = 0
        for node in all_nodes:
            storage = self.converter.blocks_to_storage_format(node.blocks)
            assert_valid_storage_xml(self, storage, label=node.title)
            panel_count += storage.count('<ac:structured-macro ac:name="panel">')
            self.assertNotIn('<span style="background-color:', storage)

        self.assertGreater(panel_count, 0)

    def test_converter_pdf_block_single_macro_no_duplicate_link(self):
        """PDFブロックがviewpdfインラインプレビューマクロ単一で出力され、重複リンクが生成されないこと。"""
        blocks = [
            {
                "type": "pdf",
                "pdf": {
                    "file": {"url": "test.pdf"},
                    "caption": [{"type": "text", "text": {"content": "資料キャプション"}, "plain_text": "資料キャプション"}],
                },
                "_attachment_filename": "test.pdf",
                "_attachment_uploaded": True,
            }
        ]
        html = self.converter.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, html)
        self.assertIn('<ac:structured-macro ac:name="viewpdf">', html)
        self.assertIn('<ri:attachment ri:filename="test.pdf" />', html)
        self.assertIn('<p><em>資料キャプション</em></p>', html)
        self.assertEqual(html.count('<ac:structured-macro ac:name="viewpdf"'), 1)
        self.assertNotIn('<p><ac:structured-macro ac:name="viewpdf"', html)
        self.assertNotIn('<ac:link>', html)

    def test_converter_custom_link_text_no_card_appearance(self):
        """自ページへのリンクやアンカー指定リンクで ac:card-appearance="inline" が付与されずカスタム名（例: 熊本城）が維持されること。"""
        self.converter.id_to_title_map = {"p123": "エラーパターン"}
        self.converter.current_node_id = "p123"
        rich_texts = [
            {
                "type": "text",
                "text": {"content": "熊本城", "link": {"url": "/p123"}},
                "plain_text": "熊本城",
                "page_id": "p123",
            }
        ]
        html = self.converter.convert_rich_text(rich_texts)
        assert_valid_storage_xml(self, html)
        self.assertIn('ri:content-title="エラーパターン"', html)
        self.assertIn('<ac:link-body>熊本城</ac:link-body>', html)
        self.assertNotIn('ac:card-appearance="inline"', html)

        # アンカー指定リンクの場合も card-appearance が付与されないこと
        self.converter.id_to_title_map["4117be77-284d-8159-abc8-fa6cce1ccf00"] = "別ページ"
        self.converter.current_node_id = "other-page"
        rich_anchor = [
            {
                "type": "text",
                "text": {"content": "セクションA", "link": {"url": "https://app.notion.com/p/Block-A-4117be77284d8159abc8fa6cce1ccf00#sec-a"}},
                "plain_text": "セクションA",
            }
        ]
        html_anchor = self.converter.convert_rich_text(rich_anchor)
        assert_valid_storage_xml(self, html_anchor)
        self.assertIn('ac:anchor="sec-a"', html_anchor)
        self.assertNotIn('ac:card-appearance="inline"', html_anchor)

    def test_html_extractor_unwraps_synced_block_div(self):
        """同期ブロック（ネストされたdiv）内のコールアウトが正常に抽出されること。"""
        html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
  <article id="p-1" class="page">
    <div class="page-body">
      <div dir="auto">
        <div dir="auto">
          <aside class="block-color-blue_background callout" id="callout-synced">
            <p>同期ブロック１</p>
          </aside>
        </div>
      </div>
    </div>
  </article>
</body>
</html>"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
            tmp_zip_path = tmp_zip.name

        try:
            with zipfile.ZipFile(tmp_zip_path, "w") as zf:
                zf.writestr("test_sync.html", html_content)

            extractor = HtmlZipExtractor(tmp_zip_path)
            tree = extractor.extract_tree()
            self.assertEqual(len(tree.blocks), 1)
            b = tree.blocks[0]
            self.assertEqual(b["type"], "callout")
            self.assertEqual(b["id"], "callout-synced")
            self.assertEqual(b["callout"]["rich_text"][0]["plain_text"], "同期ブロック１")
        finally:
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

    def test_importer_skips_empty_database_row(self):
        """空のデータベース行（無題・本文なし・プロパティなし）が検出されスキップされること。"""
        from src.importer import ConfluenceImporter

        empty_row = PageNode(
            id="row-empty",
            title="Untitled Page",
            node_type="database_row",
            properties={"schema": {}, "title": {}},
            blocks=[],
        )
        self.assertTrue(ConfluenceImporter.is_empty_database_row(empty_row))

        non_empty_row = PageNode(
            id="row-data",
            title="タスクA",
            node_type="database_row",
            properties={"Status": "完了"},
            blocks=[],
        )
        self.assertFalse(ConfluenceImporter.is_empty_database_row(non_empty_row))

    def test_verifier_detects_smart_card_overwriting_custom_text(self):
        """ac:card-appearance="inline" がカスタムリンク名を上書きする状況を検証がFAILとして検出すること。"""
        mock_client = MagicMock()
        mock_client.get_page_by_id.return_value = {
            "id": "p-1",
            "title": "エラーパターン",
            "body": {
                "storage": {
                    "value": '<p><ac:link ac:card-appearance="inline"><ri:page ri:content-title="エラーパターン" /><ac:link-body>熊本城</ac:link-body></ac:link></p>'
                }
            },
        }
        verifier = MigrationVerifier(mock_client)
        node = PageNode(id="p-1", title="エラーパターン", node_type="page", blocks=[])
        res = verifier.verify_page_node(node, "p-1")
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("overwriting custom link text '熊本城'" in issue for issue in res["issues"]))

    def test_html_extractor_pdf_attachment_ref_normalization_and_icon_skip(self):
        """attachment:<uuid>:<filename> 表記の正規化・ZIP解決・PDFアイコンimg誤判定回避・キャプション保持を検証。"""
        html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>PDF Pattern Test</title></head>
<body>
  <article id="page-12345678-1234-1234-1234-123456789abc" class="page sans">
    <header><h1 class="page-title">PDF Pattern Test</h1></header>
    <div class="page-body">
      <figure id="pdf-icon-1" class="pdf">
        <div class="source">
          <a href="subaru_studres_2016_p01.pdf"><img class="icon" src="https://app.notion.com/icons/pdf.svg" />attachment:80d7f95d-3e53-49a8-af77-d98c350732be:subaru_studres_2016_p01.pdf</a>
        </div>
        <figcaption>attachment:80d7f95d-3e53-49a8-af77-d98c350732be:資料補足</figcaption>
      </figure>
      <figure id="pdf-uri-href" class="file">
        <a href="attachment:80d7f95d-3e53-49a8-af77-d98c350732be:subaru_studres_2016_p01.pdf">attachment:80d7f95d-3e53-49a8-af77-d98c350732be:subaru_studres_2016_p01.pdf</a>
      </figure>
      <p id="inline-ref">参照: <a href="subaru_studres_2016_p01.pdf">attachment:80d7f95d-3e53-49a8-af77-d98c350732be:subaru_studres_2016_p01.pdf</a></p>
    </div>
  </article>
</body>
</html>"""
        pdf_bytes = b"%PDF-1.4 subaru bytes"
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "pdf_patterns.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("エラーパターン/page.html", html_content)
                zf.writestr("エラーパターン/subaru_studres_2016_p01.pdf", pdf_bytes)

            tree = HtmlZipExtractor(zip_path).extract_tree()
            self.assertEqual(len(tree.blocks), 3)

            b0 = tree.blocks[0]
            self.assertEqual(b0["type"], "pdf")
            self.assertEqual(b0.get("_attachment_filename"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b0.get("_display_name"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b0.get("_attachment_bytes"), pdf_bytes)
            self.assertEqual(b0["pdf"]["caption"][0]["text"]["content"], "資料補足")

            b1 = tree.blocks[1]
            self.assertEqual(b1["type"], "pdf")
            self.assertEqual(b1.get("_attachment_filename"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b1.get("_display_name"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b1.get("_attachment_bytes"), pdf_bytes)

            b2 = tree.blocks[2]
            self.assertEqual(b2["type"], "paragraph")
            link_rt = b2["paragraph"]["rich_text"][1]
            self.assertEqual(link_rt["plain_text"], "subaru_studres_2016_p01.pdf")
            self.assertNotIn("page_id", link_rt)

    def test_html_extractor_pdf_nfd_nfc_and_column_nesting_with_converter(self):
        """カラム内にネストされたNFD日本語ファイル名PDFがNFC正規化で抽出・viewpdf変換されること。"""
        import unicodedata
        nfd_name = unicodedata.normalize("NFD", "A10_SPWorkshop_2019_ご案内.pdf")
        nfc_name = unicodedata.normalize("NFC", "A10_SPWorkshop_2019_ご案内.pdf")
        encoded_nfc = "A10_SPWorkshop_2019_%E3%81%94%E6%A1%88%E5%86%85.pdf"

        html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Column PDF Test</title></head>
<body>
  <article id="4137be77-284d-801d-9fcf-c13c0989f376" class="page sans">
    <header><h1 class="page-title">Column PDF Test</h1></header>
    <div class="page-body">
      <div class="column-list" id="col-list-1">
        <div class="column" id="col-1">
          <figure id="pdf-col-1" class="pdf">
            <div class="source"><a href="エラーパターン/{encoded_nfc}">{nfc_name}</a></div>
          </figure>
        </div>
        <div class="column" id="col-2">
          <p>右カラムテキスト</p>
        </div>
      </div>
    </div>
  </article>
</body>
</html>"""
        pdf_bytes = b"%PDF-1.4 a10 workshop bytes"
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "nfd_col.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("プライベート/エラーパターン.html", html_content)
                zf.writestr(f"プライベート/エラーパターン/{nfd_name}", pdf_bytes)

            tree = HtmlZipExtractor(zip_path).extract_tree()
            self.assertEqual(len(tree.blocks), 1)
            col_list = tree.blocks[0]
            self.assertEqual(col_list["type"], "column_list")
            pdf_block = col_list["children_blocks"][0]["children_blocks"][0]
            self.assertEqual(pdf_block["type"], "pdf")
            self.assertEqual(pdf_block.get("_attachment_filename"), nfc_name)
            self.assertEqual(pdf_block.get("_attachment_bytes"), pdf_bytes)

            # Storage Format変換・XML整形式検証
            storage_xml = self.converter.blocks_to_storage_format(tree.blocks)
            assert_valid_storage_xml(self, storage_xml)
            self.assertIn('<ac:structured-macro ac:name="section">', storage_xml)
            self.assertIn('<ac:structured-macro ac:name="column">', storage_xml)
            self.assertIn(f'<ac:structured-macro ac:name="viewpdf"><ac:parameter ac:name="name"><ri:attachment ri:filename="{nfc_name}" /></ac:parameter></ac:structured-macro>', storage_xml)

    def test_importer_uploads_nested_column_pdf_with_nfc_filename(self):
        """ConfluenceImporter._process_attachmentsがカラム内ネストPDFをNFCファイル名でアップロードすること。"""
        import unicodedata
        from unittest.mock import patch
        from src.importer import ConfluenceImporter

        nfd_name = unicodedata.normalize("NFD", "A10_SPWorkshop_2019_ご案内.pdf")
        nfc_name = unicodedata.normalize("NFC", "A10_SPWorkshop_2019_ご案内.pdf")
        pdf_bytes = b"%PDF-1.4 dummy"

        pdf_block = {
            "id": "pdf-nested",
            "type": "pdf",
            "pdf": {"type": "file", "file": {"url": nfd_name}, "caption": []},
            "_attachment_filename": nfd_name,
            "_attachment_bytes": pdf_bytes,
        }
        col_list = {
            "id": "col-list",
            "type": "column_list",
            "children_blocks": [
                {"id": "c1", "type": "column", "children_blocks": [pdf_block]},
                {"id": "c2", "type": "column", "children_blocks": []},
            ],
        }
        with patch("src.importer.ConfluenceClient") as mock_cli_cls:
            mock_cli = mock_cli_cls.return_value
            mock_cli.base_url = "https://example.atlassian.net/wiki"
            mock_cli.upload_attachment.return_value = {"results": [{"id": "att-1"}]}

            importer = ConfluenceImporter("https://example.atlassian.net/wiki", "u", "t", "SPACE")
            importer._process_attachments([col_list], "page-999")

            mock_cli.upload_attachment.assert_called_once_with(
                page_id="page-999",
                filename=nfc_name,
                file_bytes=pdf_bytes,
                mime_type="application/pdf",
            )
            self.assertEqual(pdf_block["_attachment_filename"], nfc_name)
            self.assertTrue(pdf_block["_attachment_uploaded"])

            # アップロード失敗(None返却)時は _attachment_uploaded が False になること
            pdf_fail = {
                "id": "pdf-fail",
                "type": "pdf",
                "pdf": {"type": "file", "file": {"url": "fail.pdf"}, "caption": []},
                "_attachment_filename": "fail.pdf",
                "_attachment_bytes": b"%PDF-1.4 fail",
            }
            mock_cli.upload_attachment.return_value = None
            importer._process_attachments([pdf_fail], "page-999")
            self.assertFalse(pdf_fail["_attachment_uploaded"])

    def test_verifier_pdf_viewpdf_nested_column_and_view_error_detection(self):
        """MigrationVerifierがカラム内PDFのviewpdf検証、添付欠落検知、view_bodyエラー検知を行うこと。"""
        import unicodedata
        nfc_name = unicodedata.normalize("NFC", "A10_SPWorkshop_2019_ご案内.pdf")
        nfd_name = unicodedata.normalize("NFD", "A10_SPWorkshop_2019_ご案内.pdf")
        col_node = PageNode(
            id="p-pdf",
            title="PDF Column Page",
            node_type="page",
            blocks=[
                {
                    "id": "col-list",
                    "type": "column_list",
                    "children_blocks": [
                        {
                            "id": "col-1",
                            "type": "column",
                            "children_blocks": [
                                {
                                    "id": "pdf-1",
                                    "type": "pdf",
                                    "pdf": {"type": "file", "file": {"url": nfc_name}},
                                    "_attachment_filename": nfc_name,
                                    "_attachment_uploaded": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        )

        # 1. 正常系 (viewpdf + section/column + attachment あり + view_body 正常)
        mock_client = MagicMock()
        mock_client.get_page_by_id.return_value = {
            "id": "conf-1",
            "title": "PDF Column Page",
            "body": {
                "storage": {
                    "value": (
                        '<ac:structured-macro ac:name="section"><ac:rich-text-body>'
                        '<ac:structured-macro ac:name="column"><ac:rich-text-body>'
                        f'<ac:structured-macro ac:name="viewpdf"><ac:parameter ac:name="name"><ri:attachment ri:filename="{nfc_name}" /></ac:parameter></ac:structured-macro>'
                        '</ac:rich-text-body></ac:structured-macro>'
                        '</ac:rich-text-body></ac:structured-macro>'
                    )
                },
                "view": {
                    "value": '<div class="conf-macro output-block" data-macro-name="viewpdf"><div class="office-container"></div></div>'
                },
            },
        }
        mock_client.get_page_attachments.return_value = [{"title": nfd_name, "id": "att-1"}]
        verifier = MigrationVerifier(mock_client)
        res_ok = verifier.verify_page_node(col_node, "conf-1")
        self.assertEqual(res_ok["status"], "PASS")
        self.assertTrue(any("viewpdf" in c for c in res_ok["checks_passed"]))

        # 2. view_body にレンダリングエラーがある場合 FAIL になること
        col_node_err = PageNode(
            id="p-pdf-err",
            title="PDF Column Page",
            node_type="page",
            blocks=[
                {
                    "id": "pdf-1",
                    "type": "pdf",
                    "pdf": {"type": "file", "file": {"url": nfc_name}},
                    "_attachment_filename": nfc_name,
                    "_attachment_uploaded": True,
                }
            ],
        )
        mock_client.get_page_by_id.return_value = {
            "id": "conf-2",
            "title": "PDF Column Page",
            "body": {
                "storage": {
                    "value": f'<ac:structured-macro ac:name="viewpdf"><ac:parameter ac:name="name"><ri:attachment ri:filename="{nfc_name}" /></ac:parameter></ac:structured-macro>'
                },
                "view": {
                    "value": '<div class="error">マクロ \'viewpdf\' のレンダリング中にエラーが発生しました : Failed to find attachment</div>'
                },
            },
        }
        res_err = verifier.verify_page_node(col_node_err, "conf-2")
        self.assertEqual(res_err["status"], "FAIL")
        self.assertTrue(any("Macro load/render error" in i for i in res_err["issues"]))

    def test_html_extractor_distinguishes_preview_disabled_file_pdf_vs_embedded_pdf(self):
        """Notion上のプレビューなしPDF(data-notion-file付与)はfileリンク、PDF埋め込みはviewpdfとして区別変換されること。"""
        import unicodedata
        nfd_a10 = unicodedata.normalize("NFD", "A10_SPWorkshop_2019_ご案内.pdf")
        nfc_a10 = unicodedata.normalize("NFC", "A10_SPWorkshop_2019_ご案内.pdf")
        nfc_a10_alias = unicodedata.normalize("NFC", "A10 SPWorkshop 2019_ご案内.pdf")
        nfd_a10_alias = unicodedata.normalize("NFD", "A10 SPWorkshop 2019_ご案内.pdf")

        html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>PDF Preview vs File Test</title></head>
<body>
  <article id="4137be77-284d-8041-89c8-d080073331b4" class="page sans">
    <header><h1 class="page-title">PDF Preview vs File Test</h1></header>
    <div class="page-body">
      <figure id="4157be77-284d-800c-a0d2-cb01a1db6288" dir="ltr">
        <div class="source">
          <a href="エラーパターン/{nfd_a10}" data-notion-file="エラーパターン/{nfd_a10}" data-notion-file-alias="{nfd_a10_alias}">{nfd_a10_alias}</a>
        </div>
      </figure>
      <figure id="4177be77-284d-80f1-acc0-e8ea00b4f270" dir="ltr">
        <div class="source">
          <a href="エラーパターン/subaru_studres_2016_p01.pdf">attachment:bee699b7-a0e3-490b-9fe9-cbabec976562:subaru_studres_2016_p01.pdf</a>
        </div>
      </figure>
    </div>
  </article>
</body>
</html>"""
        a10_bytes = b"%PDF-1.4 a10 bytes"
        subaru_bytes = b"%PDF-1.4 subaru bytes"
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "distinguish.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("プライベート/エラーパターン.html", html_content)
                zf.writestr(f"プライベート/エラーパターン/{nfd_a10}", a10_bytes)
                zf.writestr("プライベート/エラーパターン/subaru_studres_2016_p01.pdf", subaru_bytes)

            tree = HtmlZipExtractor(zip_path).extract_tree()
            self.assertEqual(len(tree.blocks), 2)

            b_file = tree.blocks[0]
            self.assertEqual(b_file["type"], "file")
            self.assertEqual(b_file.get("_attachment_filename"), nfc_a10)
            self.assertEqual(b_file.get("_display_name"), nfc_a10_alias)
            self.assertEqual(b_file.get("_attachment_bytes"), a10_bytes)

            b_pdf = tree.blocks[1]
            self.assertEqual(b_pdf["type"], "pdf")
            self.assertEqual(b_pdf.get("_attachment_filename"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b_pdf.get("_display_name"), "subaru_studres_2016_p01.pdf")
            self.assertEqual(b_pdf.get("_attachment_bytes"), subaru_bytes)

            storage_xml = self.converter.blocks_to_storage_format(tree.blocks)
            assert_valid_storage_xml(self, storage_xml)
            self.assertIn(f'<ac:link><ri:attachment ri:filename="{nfc_a10}" />', storage_xml)
            self.assertIn(f'<![CDATA[📎 {nfc_a10_alias} (添付ファイルをダウンロード)]]>', storage_xml)
            self.assertNotIn(f'ac:name="viewpdf"><ac:parameter ac:name="name"><ri:attachment ri:filename="{nfc_a10}"', storage_xml)
            self.assertIn('<ac:structured-macro ac:name="viewpdf"><ac:parameter ac:name="name"><ri:attachment ri:filename="subaru_studres_2016_p01.pdf" /></ac:parameter></ac:structured-macro>', storage_xml)


if __name__ == "__main__":
    unittest.main()
