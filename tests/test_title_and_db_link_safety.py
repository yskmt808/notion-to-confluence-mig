import unittest
from unittest.mock import MagicMock, patch

from src.extractor import PageNode
from src.converter import NotionToConfluenceConverter
from src.importer import ConfluenceImporter, ConfluenceClient


class TestTitleAndDbLinkSafety(unittest.TestCase):
    def test_resolve_unique_page_title_truncates_long_titles(self):
        """255文字を超える長いタイトルが安全に切り詰められ、重複サフィックス付与後も255文字以内に収まることを検証"""
        mock_client = MagicMock(spec=ConfluenceClient)
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_page_by_title.return_value = None

        importer = ConfluenceImporter(
            url="https://example.atlassian.net",
            username="test@example.com",
            token="dummy-token",
            space_key="TEST",
        )
        importer.client = mock_client

        long_title = "A" * 300
        assigned_title, existing = importer._resolve_unique_page_title(
            base_title=long_title,
            notion_node_id="node-1",
        )
        self.assertLessEqual(len(assigned_title), 245)
        self.assertTrue(assigned_title.endswith("..."))

        # 重複発生時のサフィックス付与テスト
        mock_client.get_page_by_title.side_effect = lambda space_key, title: {"id": "123"} if title == assigned_title else None
        assigned_title2, _ = importer._resolve_unique_page_title(
            base_title=long_title,
            notion_node_id="node-2",
            allow_existing_overwrite=False,
        )
        self.assertLessEqual(len(assigned_title2), 255)
        self.assertIn(" (2)", assigned_title2)

    def test_client_truncates_title_safely(self):
        """ConfluenceClient の各API呼び出しがタイトルを255文字以下にクリップすることを検証"""
        client = ConfluenceClient(
            base_url="https://example.atlassian.net",
            username="test@example.com",
            token="dummy",
        )
        long_title = "X" * 300

        with patch("requests.post") as mock_post:
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = {"id": "page-123"}
            mock_post.return_value = mock_res

            client.create_page("TEST", long_title, "<p>body</p>")
            payload = mock_post.call_args[1]["json"]
            self.assertLessEqual(len(payload["title"]), 255)
            self.assertTrue(payload["title"].endswith("..."))

    def test_database_csv_safeguards_page_link_column(self):
        """confluence-page-link 列にURL以外の文字列タイトルが混入せず、安全に空文字になることを検証"""
        conv = NotionToConfluenceConverter()
        db_node = PageNode(
            id="db-1",
            title="テストDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[
                PageNode(
                    id="row-1",
                    title="長いタイトル" * 10,
                    node_type="database_row",
                    properties={"名前": {"type": "title", "value": "行タイトル1"}},
                ),
                PageNode(
                    id="row-2",
                    title="正常な行",
                    node_type="database_row",
                    properties={"名前": {"type": "title", "value": "行タイトル2"}},
                ),
            ],
        )

        # row-1 はページ作成失敗等で link が無く、row-2 のみ有効なURLが存在する場合
        page_link_map = {
            "row-2": "https://example.atlassian.net/wiki/spaces/TEST/pages/99999",
        }

        csv_str = conv.generate_database_csv(db_node, page_link_map)
        lines = csv_str.splitlines()
        data_start = lines.index("<data>")

        # ヘッダー: リンク,名前
        row1_data = lines[data_start + 2]
        row2_data = lines[data_start + 3]

        # row-1 のリンク列は空文字列であること（タイトルが混入していないこと）
        self.assertTrue(row1_data.startswith('"",'), f"Expected empty page-link but got: {row1_data}")
        # row-2 のリンク列は有効なURLであること
        self.assertIn("https://example.atlassian.net/wiki/spaces/TEST/pages/99999", row2_data)

    def test_database_csv_normalizes_newlines_in_cells(self):
        """CSVカラム値に含まれる改行文字が半角スペースに正規化され、<data>セクションが厳格に1行1レコードを維持することを検証"""
        conv = NotionToConfluenceConverter()
        multiline_desc = "一行目\n二行目\r\n三行目"
        db_node = PageNode(
            id="db-2",
            title="改行テストDB",
            node_type="database",
            properties={
                "schema": {
                    "タスク名": {"type": "title"},
                    "詳細説明": {"type": "rich_text"},
                }
            },
            children=[
                PageNode(
                    id="row-1",
                    title="タスク1\nタイトル改行",
                    node_type="database_row",
                    properties={
                        "タスク名": {"type": "title", "value": "タスク1\nタイトル改行"},
                        "詳細説明": {"type": "rich_text", "value": multiline_desc},
                    },
                )
            ],
        )

        csv_str = conv.generate_database_csv(db_node, {"row-1": "https://example.atlassian.net/wiki/spaces/TEST/pages/1001"})
        lines = csv_str.splitlines()

        data_start = lines.index("<data>")
        data_end = lines.index("</data>")

        # <data> の直後はヘッダー行、その次がレコード行、その次が </data> となり、ちょうど2行であること
        data_content_lines = lines[data_start + 1 : data_end]
        self.assertEqual(len(data_content_lines), 2, f"<data> should only have header and 1 row line, got: {data_content_lines}")

        record_line = data_content_lines[1]
        self.assertNotIn("\n", record_line)
        self.assertNotIn("\r", record_line)
        self.assertIn("一行目 二行目 三行目", record_line)

    def test_html_extractor_handles_newlines_in_csv_values(self):
        """HTMLエクスポート内のCSVでカラム値に改行がある場合、text型として識別されファイル名マッチも成功することを検証"""
        import tempfile
        import zipfile
        from src.html_extractor import HtmlZipExtractor

        # 改行を含むタスク名および複数行説明のCSV
        csv_content = """名前,説明,ステータス
"タスクA\n改行付き","これは\n複数行の\n詳細です",進行中
"""
        # Notionエクスポートではファイル名中の改行が半角スペースに置換される
        child_html_content = "<html><head><title>タスクA 改行付き</title></head><body><div class='page-body'><p>本文</p></div></body></html>"
        db_html_content = "<html><head><title>テストDB</title></head><body></body></html>"

        tf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tf_path = tf.name
        tf.close()

        import os
        try:
            with zipfile.ZipFile(tf_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("テストDB 00000000000000000000000000000001.csv", csv_content.encode("utf-8-sig"))
                zf.writestr("テストDB 00000000000000000000000000000001.html", db_html_content.encode("utf-8"))
                zf.writestr("テストDB/タスクA 改行付き aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.html", child_html_content.encode("utf-8"))

            extractor = HtmlZipExtractor(tf_path)
            root = extractor.extract_tree()
            self.assertEqual(len(root.children), 1)
            db_row = root.children[0]

            # 改行がスペースに正規化されてタイトルに設定されていること
            self.assertEqual(db_row.title, "タスクA 改行付き")
            # HTMLファイルがマッチしてIDが取得できていること
            self.assertEqual(db_row.id, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            # 説明プロパティが select ではなく text として分類されていること
            self.assertEqual(db_row.properties["説明"]["type"], "text")
            self.assertIn("\n", db_row.properties["説明"]["value"])
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)


if __name__ == "__main__":
    unittest.main()
