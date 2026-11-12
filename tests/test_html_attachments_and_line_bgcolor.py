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
        """Notionで行単位背景色が設定されたブロックがConfluence Storage Formatでタグ自体のstyleとしてレンダリングされること。"""
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
                "type": "paragraph",
                "paragraph": {
                    "color": "yellow_background",
                    "rich_text": [
                        {"type": "text", "text": {"content": "Normal "}, "annotations": {"color": "default"}},
                        {"type": "text", "text": {"content": "pink"}, "annotations": {"color": "pink_background"}},
                    ],
                },
            },
        ]

        html = self.converter.blocks_to_storage_format(blocks)

        # 段落全体に背景色スタイルが適用されていること
        self.assertIn('<p style="background-color: #fbf3db;', html)
        # 文字単位の <span style="background-color: #fbf3db;"> でラップされていないこと
        self.assertNotIn('<span style="background-color: #fbf3db;', html)

        # 見出し全体に背景色スタイルが適用されていること
        self.assertIn('<h1 style="background-color: #e7f3f8;', html)

        # 引用全体に背景色スタイルが適用されていること
        self.assertIn('<blockquote style="background-color: #f4f0f7;', html)

        # 箇条書き全体に背景色スタイルが適用されていること
        self.assertIn('<li style="background-color: #edf3ec;', html)

        # インライン指定 (pink_background) はそのまま保持されること
        self.assertIn('<span style="background-color: #fbf0f4;', html)

    def test_converter_pdf_block_single_macro_no_duplicate_link(self):
        """PDFブロックがview-fileマクロ単一で出力され、重複リンクが生成されないこと。"""
        blocks = [
            {
                "type": "pdf",
                "pdf": {"file": {"url": "test.pdf"}},
                "_attachment_filename": "test.pdf",
                "_attachment_uploaded": True,
            }
        ]
        html = self.converter.blocks_to_storage_format(blocks)
        self.assertIn('<ac:structured-macro ac:name="view-file"', html)
        self.assertEqual(html.count('<ac:structured-macro ac:name="view-file"'), 1)
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


if __name__ == "__main__":
    unittest.main()
