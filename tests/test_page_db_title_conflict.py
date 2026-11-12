import unittest
from unittest.mock import MagicMock

from src.extractor import PageNode
from src.converter import NotionToConfluenceConverter
from src.importer import ConfluenceImporter


class TestPageDbTitleConflict(unittest.TestCase):
    """ページ名とデータベース名のコンフリクト解決・保護動作を網羅的に検証するテスト。"""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.base_url = "https://example.atlassian.net/wiki"
        self.mock_client.get_space_id.return_value = "space-123"

    def test_same_name_page_and_database_in_tree(self):
        """同一ツリー内に同名のページとデータベースが存在する場合、両方がユニーク化され安全に作成されること"""
        # 初回はサーバー上に何も存在しない
        self.mock_client.get_page_by_title.return_value = None
        self.mock_client.get_database_by_title.return_value = None
        self.mock_client.create_page.return_value = {
            "id": "conf-page-1",
            "title": "議事録",
            "_links": {"webui": "/spaces/TEST/pages/conf-page-1"},
        }
        self.mock_client.create_database.return_value = {
            "id": "conf-db-1",
            "title": "議事録 (2)",
            "_links": {"webui": "/spaces/TEST/database/conf-db-1"},
        }
        self.mock_client.get_database_views.return_value = [{"id": "v1", "name": "デフォルト", "type": "table"}]
        self.mock_client.get_database_entries.return_value = []
        self.mock_client.update_page.return_value = {"id": "conf-page-1"}
        self.mock_client.get_page_by_id.return_value = {"id": "conf-page-1", "version": {"number": 1}}

        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="user",
            token="token",
            space_key="TEST",
            page_workers=1,
        )
        importer.client = self.mock_client

        # ページ「議事録」とDB「議事録」
        page_node = PageNode(id="p-1", title="議事録", node_type="page", children=[])
        db_node = PageNode(id="db-1", title="議事録", node_type="database", properties={"schema": {}}, children=[])
        root = PageNode(id="root", title="ルート", node_type="page", children=[page_node, db_node])

        importer.import_tree(root, parent_id=None, overwrite=True)

        # ページは「議事録」、DBは「議事録 (2)」としてユニーク化されていること
        self.assertEqual(page_node.title, "議事録")
        self.assertEqual(db_node.title, "議事録 (2)")

        # ページの削除 (delete_page) が一度も呼ばれていないこと（誤削除防止）
        self.mock_client.delete_page.assert_not_called()

        # DB作成時に「議事録 (2)」が渡されていること
        self.mock_client.create_database.assert_called_once()
        db_call = self.mock_client.create_database.call_args
        self.assertEqual(db_call.kwargs["title"], "議事録 (2)")

        # migration_mapに両方正しく登録されていること
        self.assertEqual(importer.migration_map["p-1"]["title"], "議事録")
        self.assertEqual(importer.migration_map["p-1"]["node_type"], "page")
        self.assertEqual(importer.migration_map["db-1"]["title"], "議事録 (2)")
        self.assertEqual(importer.migration_map["db-1"]["node_type"], "database")

    def test_database_does_not_overwrite_existing_page(self):
        """Confluence上に既存ページが存在する場合、同名DB移行時に既存ページを上書き・削除せずサフィックスが付与されること"""
        # Confluence上に既存ページ「プロジェクト管理」が存在する
        def mock_get_page(space_key, title):
            if title == "プロジェクト管理":
                return {"id": "existing-page-999", "title": "プロジェクト管理"}
            return None

        self.mock_client.get_page_by_title.side_effect = mock_get_page
        self.mock_client.get_database_by_title.return_value = None
        self.mock_client.create_database.return_value = {
            "id": "conf-db-999",
            "title": "プロジェクト管理 (2)",
            "_links": {"webui": "/spaces/TEST/database/conf-db-999"},
        }

        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="user",
            token="token",
            space_key="TEST",
        )
        importer.client = self.mock_client

        target_title, existing_item = importer._resolve_unique_page_title(
            base_title="プロジェクト管理",
            notion_node_id="db-node-1",
            allow_existing_overwrite=True,
            is_database=True,
        )

        # 既存ページが存在するため、上書きされず「プロジェクト管理 (2)」になること
        self.assertEqual(target_title, "プロジェクト管理 (2)")
        self.assertIsNone(existing_item)

        # 既存ページの誤削除が起きないこと
        self.mock_client.delete_page.assert_not_called()

    def test_page_does_not_overwrite_existing_database(self):
        """Confluence上に既存データベースが存在する場合、同名ページ移行時に既存DBを上書きせずサフィックスが付与されること"""
        # Confluence上に既存DB「タスク一覧」が存在する
        def mock_get_db(space_key, title):
            if title == "タスク一覧":
                return {"id": "existing-db-888", "title": "タスク一覧"}
            return None

        self.mock_client.get_database_by_title.side_effect = mock_get_db
        self.mock_client.get_page_by_title.return_value = None

        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="user",
            token="token",
            space_key="TEST",
        )
        importer.client = self.mock_client

        target_title, existing_item = importer._resolve_unique_page_title(
            base_title="タスク一覧",
            notion_node_id="page-node-1",
            allow_existing_overwrite=True,
            is_database=False,
        )

        # 既存DBが存在するため、上書きされず「タスク一覧 (2)」になること
        self.assertEqual(target_title, "タスク一覧 (2)")
        self.assertIsNone(existing_item)

    def test_links_to_page_and_database_with_same_base_title(self):
        """同名ベースのページとデータベースへのリンクが、それぞれの解決済みタイトル/URLで正確に出力されること"""
        converter = NotionToConfluenceConverter()
        converter.confluence_base_url = "https://example.atlassian.net/wiki"
        converter.space_key = "TEST"
        converter.migration_map = {
            "p-uuid-1": {
                "confluence_id": "111",
                "title": "ロードマップ",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/pages/111",
                "node_type": "page",
            },
            "db-uuid-1": {
                "confluence_id": "222",
                "title": "ロードマップ (2)",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/database/222",
                "node_type": "database",
            },
        }

        # 1. ページメンション
        page_mention = [
            {
                "type": "mention",
                "mention": {"type": "page", "page": {"id": "p-uuid-1"}},
                "plain_text": "@ロードマップ",
            }
        ]
        page_html = converter.convert_rich_text(page_mention)
        self.assertIn('ri:content-title="ロードマップ"', page_html)
        self.assertIn("<ac:link", page_html)

        # 2. データベースメンション
        db_mention = [
            {
                "type": "mention",
                "mention": {"type": "database", "database": {"id": "db-uuid-1"}},
                "plain_text": "@ロードマップ",
            }
        ]
        db_html = converter.convert_rich_text(db_mention)
        self.assertIn("https://example.atlassian.net/wiki/spaces/TEST/database/222", db_html)
        self.assertIn('data-card-appearance="inline"', db_html)


if __name__ == "__main__":
    unittest.main()
