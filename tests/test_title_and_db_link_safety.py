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


if __name__ == "__main__":
    unittest.main()
