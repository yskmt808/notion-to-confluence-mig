import os
import tempfile
import zipfile
import unittest
from unittest.mock import MagicMock

from src.html_extractor import HtmlZipExtractor
from src.converter import NotionToConfluenceConverter
from src.importer import ConfluenceImporter, ConfluenceClient
from src.extractor import PageNode


class TestHtmlExtractorMetadataAndOrdering(unittest.TestCase):
    def _create_sample_zip_file(self) -> str:
        tf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tf_path = tf.name
        tf.close()

        with zipfile.ZipFile(tf_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1. ルートHTML
            root_html = """
            <html>
            <head><title>Root Page</title></head>
            <body>
              <header><h1 class="page-title">Root Page</h1></header>
              <div class="page-body"><p>Root page content</p></div>
            </body>
            </html>
            """
            zinfo_root = zipfile.ZipInfo("Root Page 00000000000000000000000000000001.html", (2026, 11, 7, 22, 46, 44))
            zf.writestr(zinfo_root, root_html.encode("utf-8"))

            # 2. プロパティ付き子ページHTML
            child_html = """
            <html>
            <head><title>Child Page</title></head>
            <body>
              <header><h1 class="page-title">Child Page</h1></header>
              <div class="page-body">
                <table class="properties">
                  <tr><th>カテゴリ</th><td>開発</td></tr>
                </table>
                <p>Child content</p>
              </div>
            </body>
            </html>
            """
            zinfo_child = zipfile.ZipInfo("Root Page/Child Page 00000000000000000000000000000002.html", (2026, 11, 7, 23, 0, 0))
            zf.writestr(zinfo_child, child_html.encode("utf-8"))

            # 3. データベースCSVおよび行HTML
            db_csv = "名前,オーナー,ステータス\nRow Item 1,PM_User_A,Done\n"
            zinfo_csv = zipfile.ZipInfo("Root Page/Sample DB 00000000000000000000000000000003.csv", (2026, 11, 7, 22, 50, 0))
            zf.writestr(zinfo_csv, db_csv.encode("utf-8-sig"))

            db_html = """
            <html><head><title>Sample DB</title></head><body><h1>Sample DB</h1></body></html>
            """
            zinfo_db = zipfile.ZipInfo("Root Page/Sample DB 00000000000000000000000000000003.html", (2026, 11, 7, 22, 50, 0))
            zf.writestr(zinfo_db, db_html.encode("utf-8"))

            row_html = """
            <html><head><title>Row Item 1</title></head><body><div class="page-body"><p>Row body</p></div></body></html>
            """
            zinfo_row = zipfile.ZipInfo("Root Page/Sample DB/Row Item 1 00000000000000000000000000000004.html", (2026, 11, 7, 22, 55, 0))
            zf.writestr(zinfo_row, row_html.encode("utf-8"))

        return tf_path

    def test_html_zip_no_synthetic_metadata_header(self):
        """HTMLエクスポートでは不正確なZIPタイムスタンプを補完せず、メタデータヘッダを出力しないことの検証"""
        zip_path = self._create_sample_zip_file()
        try:
            extractor = HtmlZipExtractor(zip_path)
            root = extractor.extract_tree()

            # ルートノードの作成日時は補完されない（None）
            self.assertEqual(root.title, "Root Page")
            self.assertIsNone(root.created_time)
            self.assertIsNone(root.created_by)

            # HTMLエクスポート移行モード（metadata_template=""）ではヘッダが空文字になること
            conv = NotionToConfluenceConverter(metadata_template="")
            header_html = conv._render_metadata_header(root)
            self.assertEqual(header_html, "")

            # デフォルトコンバータでも、作成者・作成日時が未設定のノードではメタデータヘッダが出力されないこと
            conv_default = NotionToConfluenceConverter()
            header_default = conv_default._render_metadata_header(root)
            self.assertEqual(header_default, "")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    def test_row_view_redundant_static_table_removed(self):
        """ネイティブ行ビューが利用可能な場合、重複する静的プロパティ一覧テーブルが出力されないことの検証"""
        conv = NotionToConfluenceConverter()
        row_node = PageNode(
            id="row-1",
            title="Sample Row",
            node_type="database_row",
            properties={"Status": {"type": "select", "value": "Done"}},
            blocks=[{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Text"}}]}}],
        )
        setattr(row_node, "_entry_embed_url", "https://example.atlassian.net/wiki/database/123?entryId=456")

        content = conv.generate_page_content(row_node)
        self.assertIn("entryId=456", content)
        self.assertNotIn("📋 プロパティ一覧", content)
        self.assertNotIn('<table class="wrapped">', content)

    def test_sibling_pages_reordered_to_match_notion_definition_order(self):
        """兄弟ノード並行作成完了後、Notion元の定義順序に合わせてConfluenceのMove APIで整列されることの検証"""
        mock_client = MagicMock()
        mock_client.create_page.side_effect = [
            {"id": "cid-b", "title": "Block B"},
            {"id": "cid-a", "title": "Block A"},
            {"id": "cid-c", "title": "Block C"},
        ]
        mock_client.get_page_by_title.return_value = None
        mock_client.move_page_position.return_value = True

        importer = ConfluenceImporter(
            url="https://test.atlassian.net",
            username="test",
            token="token",
            space_key="TEST",
            page_workers=4,
        )
        importer.client = mock_client

        parent = PageNode(id="root", title="Parent", node_type="page")
        child_a = PageNode(id="a", title="Block A", node_type="page")
        child_b = PageNode(id="b", title="Block B", node_type="page")
        child_c = PageNode(id="c", title="Block C", node_type="page")

        # children は A, B, C の順
        importer._import_children_concurrently(
            parent_node=parent,
            children=[child_a, child_b, child_c],
            parent_id="parent-123",
            overwrite=True,
        )

        calls = mock_client.move_page_position.call_args_list
        self.assertEqual(len(calls), 2)
        # 1回目: B を A の後に移動
        self.assertEqual(calls[0][0], (importer.migration_map["b"]["confluence_id"], "after", importer.migration_map["a"]["confluence_id"]))
        # 2回目: C を B の後に移動
        self.assertEqual(calls[1][0], (importer.migration_map["c"]["confluence_id"], "after", importer.migration_map["b"]["confluence_id"]))


if __name__ == "__main__":
    unittest.main()
