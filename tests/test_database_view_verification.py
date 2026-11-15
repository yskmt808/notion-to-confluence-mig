"""DBビュー検証 (MigrationVerifier.verify_database_node) と importer.py ビュー生成条件の整合性テスト (TCK_00018)。"""

import unittest
from unittest.mock import MagicMock, patch

from src.extractor import PageNode
from src.verifier import MigrationVerifier


def _make_db_response(db_id: str, title: str, parent_id: str = "100") -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {"id": db_id, "title": title, "parentId": parent_id}
    return resp


class TestDatabaseViewVerification(unittest.TestCase):
    def _build_db_node(
        self,
        db_id: str,
        title: str,
        schema: dict,
        rows_props: list,
        extra_props: dict = None,
    ) -> PageNode:
        children = [
            PageNode(
                id=f"{db_id}-row-{idx}",
                title=f"Row {idx}",
                node_type="database_row",
                properties=rp,
            )
            for idx, rp in enumerate(rows_props)
        ]
        props = {"schema": schema}
        if extra_props:
            props.update(extra_props)
        return PageNode(
            id=db_id,
            title=title,
            node_type="database",
            properties=props,
            children=children,
        )

    @patch("src.verifier.requests.get")
    def test_non_status_select_and_over_5_rows_passes_with_table_view_only(self, mock_get):
        """代表ステータス以外の select 列（例: '選択'）や 6行以上でも、他ページ参照がなければテーブルビューのみで PASS になること。"""
        mock_get.return_value = _make_db_response("3375675", "同じ名前 (2)")
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.auth = ("u", "t")
        mock_client.get_database_views.return_value = [
            {"id": "v1", "name": "すべてのデータ", "type": "table"},
        ]
        mock_client.get_database_entries.return_value = [{"id": f"e{i}"} for i in range(6)]
        mock_client.get_database_fields.return_value = []

        rows = [
            {"選択": {"type": "select", "value": f"Option{i % 3}"}}
            for i in range(6)
        ]
        db_node = self._build_db_node(
            "4157be77-284d-8021-b09e-f9e9a1a7475d",
            "同じ名前",
            schema={
                "名前": {"type": "title"},
                "選択": {"type": "select"},
            },
            rows_props=rows,
        )

        res = MigrationVerifier(mock_client).verify_database_node(db_node, "3375675", expected_parent_id="100")
        self.assertEqual(res["status"], "PASS", f"Unexpected warnings/issues: {res}")
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["issues"], [])

    @patch("src.verifier.requests.get")
    def test_smart_status_column_warns_when_board_and_filtered_views_missing(self, mock_get):
        """代表ステータス列（値2〜7件）があるDBでボード/フィルタービューが欠落している場合は WARN になること。"""
        mock_get.return_value = _make_db_response("4001", "タスク管理")
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.auth = ("u", "t")
        mock_client.get_database_views.return_value = [
            {"id": "v1", "name": "すべてのデータ", "type": "table"},
        ]
        mock_client.get_database_entries.return_value = [{"id": "e0"}, {"id": "e1"}]
        mock_client.get_database_fields.return_value = []

        db_node = self._build_db_node(
            "aaaa1111-1111-1111-1111-111111111111",
            "タスク管理",
            schema={"名前": {"type": "title"}, "ステータス": {"type": "status"}},
            rows_props=[
                {"ステータス": {"type": "status", "value": "未着手"}},
                {"ステータス": {"type": "status", "value": "進行中"}},
            ],
        )

        res = MigrationVerifier(mock_client).verify_database_node(db_node, "4001", expected_parent_id="100")
        self.assertEqual(res["status"], "WARN")
        self.assertIn("Board view layout not found in Confluence database.", res["warnings"])
        self.assertIn("Group by filtered views not found in Confluence database.", res["warnings"])
        self.assertFalse(any("Gallery" in w for w in res["warnings"]))

    @patch("src.verifier.requests.get")
    def test_verify_tree_detects_missing_requested_gallery_and_board_views(self, mock_get):
        """他ページからギャラリー・ボードビューが明示参照されているDBで該当ビューが未作成の場合は WARN になること。"""
        mock_get.return_value = _make_db_response("5002", "ギャラリーDB", parent_id="5001")
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.auth = ("u", "t")
        mock_client.get_page_by_id.return_value = {
            "id": "5001",
            "title": "Hub",
            "ancestors": [{"id": "100"}],
            "body": {"storage": {"value": "<p>Hub</p>"}, "view": {"value": "<p>Hub</p>"}},
        }
        mock_client.get_page_attachments.return_value = []
        mock_client.get_database_views.return_value = [
            {"id": "v1", "name": "すべてのデータ", "type": "table"},
        ]
        mock_client.get_database_entries.return_value = [{"id": "e0"}]
        mock_client.get_database_fields.return_value = []

        db_id = "bbbb2222-2222-2222-2222-222222222222"
        db_node = self._build_db_node(
            db_id,
            "ギャラリーDB",
            schema={"名前": {"type": "title"}, "区分": {"type": "select"}},
            rows_props=[{"区分": {"type": "select", "value": "A"}}],
        )
        root_page = PageNode(
            id="aaaa1111-1111-1111-1111-111111111111",
            title="Hub",
            node_type="page",
            blocks=[
                {
                    "id": "ldb-1",
                    "type": "child_database",
                    "is_linked_database": True,
                    "database_id": db_id,
                    "data_sources": [{"id": db_id, "name": "ギャラリーDB"}],
                    "views": [
                        {"name": "Gallery", "type": "gallery", "configuration": {}},
                        {"name": "Board", "type": "board", "configuration": {"group_by": {"property_name": "区分"}}},
                    ],
                }
            ],
            children=[db_node],
        )

        results = MigrationVerifier(mock_client).verify_tree(
            root_page,
            {root_page.id: "5001", db_node.id: "5002"},
            expected_parent_id="100",
        )
        db_res = next(r for r in results if r["page_id"] == "5002")
        self.assertEqual(db_res["status"], "WARN")
        self.assertIn("Board view layout not found in Confluence database.", db_res["warnings"])
        self.assertIn("Gallery view layout not found in Confluence database.", db_res["warnings"])


if __name__ == "__main__":
    unittest.main()
