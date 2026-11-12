import os
import unittest
from unittest.mock import MagicMock, patch

from src.extractor import PageNode
from src.converter import NotionToConfluenceConverter
from src.importer import ConfluenceImporter
from src.verifier import MigrationVerifier
from src.html_extractor import HtmlZipExtractor


class TestDuplicateDbAndPageLinks(unittest.TestCase):
    """同名データベースの重複タイトル解決・UUID識別およびページ間/ページ内リンクの振る舞いを検証するテスト。"""

    def setUp(self):
        self.converter = NotionToConfluenceConverter()
        self.mock_client = MagicMock()
        self.mock_client.base_url = "https://example.atlassian.net/wiki"

    def test_duplicate_named_databases_resolved_with_suffix(self):
        """同一ツリー内に同名DBが2つ存在する場合、2つ目のDBにサフィックスが付与され両方安全に作成されること。"""
        self.mock_client.get_space_id.return_value = "space-123"
        self.mock_client.get_database_by_title.return_value = None
        self.mock_client.get_page_by_title.return_value = None
        self.mock_client.create_database.side_effect = [
            {"id": "db-conf-1", "_links": {"webui": "/spaces/TEST/database/db-conf-1"}},
            {"id": "db-conf-2", "_links": {"webui": "/spaces/TEST/database/db-conf-2"}},
        ]
        self.mock_client.get_database_views.return_value = [{"id": "v-table", "type": "table", "name": "デフォルト"}]
        self.mock_client.get_database_entries.return_value = []

        importer = ConfluenceImporter("https://example.atlassian.net/wiki", "user", "token", space_key="TEST")
        importer.client = self.mock_client

        db1 = PageNode(id="db-1", title="同じ名前", node_type="database", properties={"schema": {}}, children=[])
        db2 = PageNode(id="db-2", title="同じ名前", node_type="database", properties={"schema": {}}, children=[])
        parent = PageNode(id="p-parent", title="親ページ", node_type="page", children=[db1, db2])

        importer._import_node_recursive(parent, parent_id="root-1", overwrite=True)

        # 2つのDBタイトルがユニーク化されていること
        self.assertEqual(db1.title, "同じ名前")
        self.assertEqual(db2.title, "同じ名前 (2)")

        # 両方のDBが作成されていること
        self.assertEqual(self.mock_client.create_database.call_count, 2)
        first_call = self.mock_client.create_database.call_args_list[0]
        second_call = self.mock_client.create_database.call_args_list[1]
        self.assertEqual(first_call.kwargs["title"], "同じ名前")
        self.assertEqual(second_call.kwargs["title"], "同じ名前 (2)")

        # migration_mapに両方登録されていること
        self.assertIn("db-1", importer.migration_map)
        self.assertIn("db-2", importer.migration_map)
        self.assertEqual(importer.migration_map["db-1"]["title"], "同じ名前")
        self.assertEqual(importer.migration_map["db-2"]["title"], "同じ名前 (2)")

    def test_duplicate_named_databases_view_embed_matches_correct_uuid(self):
        """同名DBのビューブロックが、タイトル前方一致ではなくUUID完全一致で正確に対応するDBに紐付くこと。"""
        self.converter.migration_map = {
            "4157be77-284d-8021-b09e-f9e9a1a7475d": {
                "confluence_id": "conf-db-1",
                "title": "同じ名前",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/database/conf-db-1",
                "node_type": "database",
                "views": {"同じ名前": "view-111"},
            },
            "4167be77-284d-8019-9cbe-e3bfa907e63e": {
                "confluence_id": "conf-db-2",
                "title": "同じ名前 (2)",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/database/conf-db-2",
                "node_type": "database",
                "views": {"同じ名前": "view-222"},
            },
        }

        # 1つ目のDBビューブロック
        block1 = {
            "id": "4157be77-284d-8021-b09e-f9e9a1a7475d",
            "type": "child_database",
            "is_linked_database": True,
            "data_sources": [{"name": "同じ名前", "id": "4157be77-284d-8021-b09e-f9e9a1a7475d"}],
            "views": [{"name": "同じ名前", "type": "table"}],
        }
        # 2つ目のDBビューブロック（Notion上の名前は同じ「同じ名前」）
        block2 = {
            "id": "4167be77-284d-8019-9cbe-e3bfa907e63e",
            "type": "child_database",
            "is_linked_database": True,
            "data_sources": [{"name": "同じ名前", "id": "4167be77-284d-8019-9cbe-e3bfa907e63e"}],
            "views": [{"name": "同じ名前", "type": "table"}],
        }

        out1 = self.converter.blocks_to_storage_format([block1])
        out2 = self.converter.blocks_to_storage_format([block2])

        # out1 は conf-db-1 に紐付き、out2 は conf-db-2 に紐付いていること
        self.assertIn("conf-db-1", out1)
        self.assertIn("view-111", out1)
        self.assertNotIn("conf-db-2", out1)

        self.assertIn("conf-db-2", out2)
        self.assertIn("view-222", out2)
        self.assertNotIn("conf-db-1", out2)

    def test_page_links_and_title_prefix_behavior(self):
        """別ページリンクはプレフィックス有無に関わらず card-appearance="inline" となり、自ページ/アンカーリンクは通常リンクとなること。"""
        self.converter.current_node_id = "4137be77-284d-8041-89c8-d080073331b4"
        self.converter.current_page_title = "[20261112-TEST] エラーパターン"
        self.converter.migration_map = {
            "4137be77-284d-8041-89c8-d080073331b4": {
                "confluence_id": "p-self",
                "title": "[20261112-TEST] エラーパターン",
                "node_type": "page",
            },
            "4137be77-284d-801d-9fcf-c13c0989f376": {
                "confluence_id": "p-child",
                "title": "[20261112-TEST] エラーパターン子ページ",
                "node_type": "page",
            },
        }

        # 1. 自ページへのリンク（カスタム名「熊本城」）
        self_link = [
            {
                "type": "text",
                "text": {"content": "熊本城", "link": {"url": "https://app.notion.com/p/4137be77284d804189c8d080073331b4"}},
                "plain_text": "熊本城",
            }
        ]
        out_self = self.converter.convert_rich_text(self_link)
        self.assertNotIn('ac:card-appearance="inline"', out_self)
        self.assertIn('<ac:link-body>熊本城</ac:link-body>', out_self)

        # 2. 別ページへのリンク（プレフィックス付き）
        cross_link = [
            {
                "type": "text",
                "text": {"content": "エラーパターン子ページ", "link": {"url": "https://app.notion.com/p/4137be77284d801d9fcfc13c0989f376"}},
                "plain_text": "エラーパターン子ページ",
            }
        ]
        out_cross = self.converter.convert_rich_text(cross_link)
        self.assertIn('<ac:link ac:card-appearance="inline">', out_cross)
        self.assertIn('<ri:page ri:content-title="[20261112-TEST] エラーパターン子ページ" />', out_cross)

    def test_verifier_accepts_cross_page_inline_smart_card_and_dedup_suffix(self):
        """verifierが別ページのインラインスマートカードやサフィックス付きDBタイトルをPASSと判定すること。"""
        verifier = MigrationVerifier(self.mock_client)

        # 1. 別ページへのインラインリンクオブジェクトを含むページ
        self.mock_client.get_page_by_id.return_value = {
            "id": "p-1",
            "title": "[20261112-TEST] エラーパターン",
            "body": {
                "storage": {
                    "value": (
                        '<p><ac:link ac:card-appearance="inline">'
                        '<ri:page ri:content-title="[20261112-TEST] エラーパターン子ページ" />'
                        '<ac:link-body>エラーパターン子ページ</ac:link-body>'
                        '</ac:link></p>'
                        '<p><ac:link>'
                        '<ri:page ri:content-title="[20261112-TEST] エラーパターン" />'
                        '<ac:link-body>熊本城</ac:link-body>'
                        '</ac:link></p>'
                    )
                }
            },
        }
        node_page = PageNode(
            id="4137be77-284d-8041-89c8-d080073331b4",
            title="[20261112-TEST] エラーパターン",
            node_type="page",
            blocks=[
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "熊本城"}]}},
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "エラーパターン子ページ"}]}},
            ],
        )
        res_page = verifier.verify_page_node(node_page, "p-1")
        self.assertEqual(res_page["status"], "PASS")

        # 2. サフィックス付きDBノード（同じ名前 (2)）の検証
        with patch("requests.get") as mock_get:
            mock_res = MagicMock()
            mock_res.ok = True
            mock_res.json.return_value = {
                "id": "conf-db-2",
                "title": "[20261112-TEST] 同じ名前 (2)",
                "parentId": "p-1",
            }
            mock_get.return_value = mock_res
            self.mock_client.get_database_views.return_value = [{"id": "v-1", "type": "table", "name": "Default"}]
            self.mock_client.get_database_entries.return_value = []

            node_db = PageNode(
                id="4167be77-284d-8019-9cbe-e3bfa907e63e",
                title="[20261112-TEST] 同じ名前",
                node_type="database",
                children=[],
            )
            res_db = verifier.verify_database_node(node_db, "conf-db-2", expected_parent_id="p-1")
            self.assertEqual(res_db["status"], "PASS")
            self.assertTrue(any("Database title match" in chk for chk in res_db["checks_passed"]))

    def test_actual_error_pattern_zip_extraction(self):
        """提供された error_pattern ZIP から2つの同名DBが抽出され、ページ内/ページ間リンクが期待通り変換されること。"""
        zip_path = "error_pattern_ExportBlock-80d7f95d-3e53-49a8-af77-d98c350732be.zip"
        if not os.path.exists(zip_path):
            self.skipTest(f"ZIP file {zip_path} not found.")

        extractor = HtmlZipExtractor(zip_path)
        tree = extractor.extract_tree()
        self.assertEqual(tree.title, "エラーパターン")
        self.assertEqual(len(tree.children), 1)

        child_page = tree.children[0]
        self.assertEqual(child_page.title, "エラーパターン子ページ")
        self.assertEqual(len(child_page.children), 2)
        self.assertEqual(child_page.children[0].title, "同じ名前")
        self.assertEqual(child_page.children[1].title, "同じ名前")
        self.assertNotEqual(child_page.children[0].id, child_page.children[1].id)


if __name__ == "__main__":
    unittest.main()
