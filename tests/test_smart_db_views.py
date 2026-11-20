"""
tests/test_smart_db_views.py
オンデマンド参照（カテゴリ別ページ等）＋スマート厳選によるデータベースビュー自動生成削減の検証テスト。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.extractor import PageNode
from src.importer import ConfluenceImporter
from src.converter import NotionToConfluenceConverter


class TestSmartDbViews(unittest.TestCase):
    def _create_importer(self) -> ConfluenceImporter:
        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="test_user",
            token="test_token",
            space_key="TEST",
            page_workers=1,
        )
        importer.client = MagicMock()
        importer.client.base_url = "https://example.atlassian.net/wiki"
        importer.client.auth = ("test_user", "test_token")
        importer.client.get_page_by_title.return_value = None
        importer.client.get_database_views.return_value = [
            {"id": "v-all", "name": "すべてのデータ", "type": "table"}
        ]
        importer.client.get_database_entries.return_value = []
        return importer

    @patch("src.importer.requests")
    def test_unreferenced_db_skips_gallery_and_high_cardinality_columns(self, mock_requests):
        """未参照のDBで一般列（担当者名など）やギャラリービューが自動生成されず、代表ステータス列(2〜7値)のみ厳選されること"""
        mock_get_resp = MagicMock()
        mock_get_resp.ok = True
        mock_get_resp.json.return_value = {"version": {"number": 1}}
        mock_requests.get.return_value = mock_get_resp

        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-1", "_links": {"webui": "/spaces/TEST/database/db-1"}}

        # 10個の異なる「メモ」値を持つが、ステータスは3種類のDB
        rows = []
        statuses = ["未着手", "進行中", "完了"]
        for i in range(10):
            rows.append(
                PageNode(
                    id=f"row-{i}",
                    title=f"Item {i}",
                    node_type="database_row",
                    properties={
                        "メモ": {"type": "select", "value": f"Note_{i}"},
                        "ステータス": {"type": "select", "value": statuses[i % 3]},
                    },
                )
            )
        db_node = PageNode(id="db-node-1", title="Tasks DB", node_type="database", children=rows)
        root = PageNode(id="root-1", title="Root", node_type="page", children=[db_node])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        # ギャラリーは未参照のため作成されないこと
        importer.client.create_database_view.assert_not_called()

        # setup_grouped_database_views は「ステータス」の3値のみ呼ばれ、「メモ」の10値は呼ばれないこと
        importer.client.setup_grouped_database_views.assert_called_once_with(
            "db-1", "ステータス", ["完了", "未着手", "進行中"]
        )

        # ボードビューは「ステータス別ボード」の1回のみ PUT されること（「メモ別ボード」は作られない）
        put_calls = mock_requests.put.call_args_list
        self.assertEqual(len(put_calls), 1)
        put_csv = put_calls[0].kwargs["json"]["body"]["value"]
        self.assertIn("add_view,ステータス別ボード,board,ステータス", put_csv)

    @patch("src.importer.requests")
    def test_unreferenced_db_without_status_creates_no_extra_views(self, mock_requests):
        """ステータス列がなく他ページからも参照されていないDBでは、追加のボード・ギャラリー・フィルタービューが一切生成されないこと"""
        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-2", "_links": {"webui": "/spaces/TEST/database/db-2"}}

        rows = [
            PageNode(
                id=f"row-{i}",
                title=f"Item {i}",
                node_type="database_row",
                properties={"タグ": {"type": "select", "value": f"Tag_{i}"}},
            )
            for i in range(6)
        ]
        db_node = PageNode(id="db-node-2", title="Plain DB", node_type="database", children=rows)
        root = PageNode(id="root-2", title="Root", node_type="page", children=[db_node])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        importer.client.create_database_view.assert_not_called()
        importer.client.setup_grouped_database_views.assert_not_called()
        mock_requests.put.assert_not_called()

    @patch("src.importer.requests")
    def test_referenced_category_pages_create_only_referenced_views_without_limit(self, mock_requests):
        """別ページ（カテゴリ別ページ等）から参照されているカテゴリ値だけが件数制限なしでピンポイント作成され、埋め込み時に自動解決されること"""
        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-cat", "_links": {"webui": "/spaces/TEST/database/db-cat"}}

        # カテゴリ列に10種類のユニーク値があるDB
        categories = ["開発", "デザイン", "営業", "人事", "総務", "法務", "経理", "CS", "広報", "情シス"]
        rows = [
            PageNode(
                id=f"row-{i}",
                title=f"Doc {i}",
                node_type="database_row",
                properties={"カテゴリ": {"type": "select", "value": cat}},
            )
            for i, cat in enumerate(categories)
        ]
        db_node = PageNode(id="db-node-cat", title="Docs DB", node_type="database", children=rows)

        # 別ページ1: 「開発カテゴリページ」から DB を参照
        ref_page_dev = PageNode(
            id="page-dev",
            title="開発カテゴリページ",
            node_type="page",
            blocks=[{"id": "b1", "type": "link_to_page", "link_to_page": {"database_id": "db-node-cat"}}],
        )
        # 別ページ2: 「デザイン一覧」からギャラリー付きで参照、見出し「営業」でリンク
        ref_page_design = PageNode(
            id="page-design",
            title="デザイン一覧",
            node_type="page",
            blocks=[
                {
                    "id": "b2",
                    "type": "child_database",
                    "is_linked_database": True,
                    "database_id": "db-node-cat",
                    "views": [{"name": "ギャラリー表示", "type": "gallery"}],
                }
            ],
        )

        root = PageNode(id="root-cat", title="Root", node_type="page", children=[db_node, ref_page_dev, ref_page_design])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        # ギャラリービューがオンデマンドで作成されること
        importer.client.create_database_view.assert_called_once_with("db-cat", view_name="ギャラリービュー", layout="gallery")

        # カテゴリ列は10種類あるが、他ページから参照されている「デザイン」「開発」の2つのみ作成されること
        importer.client.setup_grouped_database_views.assert_called_once_with(
            "db-cat", "カテゴリ", ["デザイン", "開発"]
        )

        # Converter がページタイトル「開発カテゴリページ」の文脈からカテゴリビューの savedViewId を正しく解決できること
        conv = NotionToConfluenceConverter()
        conv.migration_map = {
            "db-node-cat": {
                "node_type": "database",
                "url": "https://example.atlassian.net/wiki/spaces/TEST/database/db-cat",
                "views": {
                    "すべてのデータ": "v-all",
                    "カテゴリ: 開発": "v-cat-dev",
                    "カテゴリ: デザイン": "v-cat-design",
                },
                "views_info": [],
            }
        }
        html_out = conv.generate_page_content(ref_page_dev)
        self.assertIn("savedViewId=v-cat-dev", html_out)

    @patch("src.importer.requests")
    def test_multi_select_field_uses_contains_operator_for_grouped_views(self, mock_requests):
        """マルチセレクト列のフィルタービュー作成では operator='contains'（「含む」）が使用されること"""
        importer = self._create_importer()
        importer.client.create_page.side_effect = lambda **kw: {"id": f"p-{kw['title']}", "_links": {"webui": f"/pages/p-{kw['title']}"}}
        importer.client.create_database.return_value = {"id": "db-ms", "_links": {"webui": "/spaces/TEST/database/db-ms"}}

        rows = [
            PageNode(
                id="row-1",
                title="Doc 1",
                node_type="database_row",
                properties={"タグ": {"type": "multi_select", "value": ["Python", "API"]}},
            ),
            PageNode(
                id="row-2",
                title="Doc 2",
                node_type="database_row",
                properties={"タグ": {"type": "multi_select", "value": ["API"]}},
            ),
        ]
        db_node = PageNode(id="db-node-ms", title="Multi DB", node_type="database", children=rows)
        ref_page = PageNode(
            id="page-ms",
            title="Pythonタグ一覧",
            node_type="page",
            blocks=[{"id": "b1", "type": "link_to_page", "link_to_page": {"database_id": "db-node-ms"}}],
        )
        root = PageNode(id="root-ms", title="Root", node_type="page", children=[db_node, ref_page])

        importer._db_view_requirements = importer._analyze_database_view_requirements(root)
        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        importer.client.setup_grouped_database_views.assert_called_once_with(
            "db-ms", "タグ", ["Python"], operator="contains"
        )

    @patch("src.importer.requests")
    def test_api_views_config_creates_views_with_quick_filters(self, mock_requests):
        """--enrich-via-api で取得した views_config（quick_filters 含む）が create_database_view で反映されること"""
        importer = self._create_importer()
        importer.client.create_database.return_value = {"id": "db-api", "_links": {"webui": "/spaces/TEST/database/db-api"}}
        importer.client.create_database_view.return_value = {"id": "v-new", "name": "AAAビュー"}

        db_node = PageNode(
            id="db-node-api",
            title="同じ名前",
            node_type="database",
            properties={
                "schema": {
                    "名前": {"id": "title", "type": "title"},
                    "項番": {"id": "CN>o", "type": "number"},
                    "選択": {"id": "|jHk", "type": "select"},
                    "タグ": {"id": "tag1", "type": "multi_select"},
                },
                "views_config": [
                    {
                        "id": "v-default",
                        "name": "Untitled",
                        "type": "table",
                        "filter": None,
                        "quick_filters": None,
                        "sorts": None,
                        "configuration": {"type": "table", "properties": []},
                    },
                    {
                        "id": "v-nomatch",
                        "name": "マッチしないView",
                        "type": "table",
                        "filter": None,
                        "quick_filters": {"CN>o": {"number": {"equals": 1000}}},
                        "sorts": None,
                        "configuration": {"type": "table", "properties": []},
                    },
                    {
                        "id": "v-aaa",
                        "name": "AAAビュー",
                        "type": "table",
                        "filter": None,
                        "quick_filters": {
                            "|jHk": {"select": {"equals": "AAA"}},
                            "tag1": {"multi_select": {"equals": "Python"}},
                        },
                        "sorts": None,
                        "configuration": {"type": "table", "properties": []},
                    },
                ],
            },
            children=[
                PageNode(
                    id="r1",
                    title="Row 1",
                    node_type="database_row",
                    properties={
                        "項番": {"type": "number", "value": "1"},
                        "選択": {"type": "select", "value": "AAA"},
                        "タグ": {"type": "multi_select", "value": ["Python"]},
                    },
                )
            ],
        )

        importer._import_node_recursive(db_node, parent_id="root-conf", overwrite=False)

        calls = importer.client.create_database_view.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["view_name"], "マッチしないView")
        self.assertEqual(calls[0].kwargs["filters"], [{"field": "項番", "operator": "=", "value": "1000"}])
        self.assertEqual(calls[1].kwargs["view_name"], "AAAビュー")
        self.assertEqual(
            calls[1].kwargs["filters"],
            [
                {"field": "選択", "operator": "=", "value": "AAA"},
                {"field": "タグ", "operator": "contains", "value": "Python"},
            ],
        )

    @patch("src.api_enricher.requests")
    def test_api_enricher_preserves_view_column_order_and_quick_filters(self, mock_req):
        """NotionApiEnricher がビューの properties 順に schema を整列し、quick_filters も保持すること"""
        from src.api_enricher import NotionApiEnricher

        def fake_get(url, headers=None, timeout=15):
            r = MagicMock()
            r.status_code = 200
            r.ok = True
            if "/v1/databases/" in url:
                r.json.return_value = {
                    "properties": {
                        "作成日時": {"id": "@VEm", "type": "created_time"},
                        "項番": {"id": "CN>o", "type": "number"},
                        "名前": {"id": "title", "type": "title"},
                    }
                }
            elif "/v1/views?" in url:
                r.json.return_value = {"results": [{"id": "v1"}]}
            elif "/v1/views/v1" in url:
                r.json.return_value = {
                    "id": "v1",
                    "name": "マッチしないView",
                    "type": "table",
                    "filter": None,
                    "quick_filters": {"CN>o": {"number": {"equals": 1000}}},
                    "sorts": None,
                    "configuration": {
                        "properties": [
                            {"property_id": "CN>o", "property_name": "項番"},
                            {"property_id": "title", "property_name": "名前"},
                            {"property_id": "@VEm", "property_name": "作成日時"},
                        ]
                    },
                }
            return r

        mock_req.get.side_effect = fake_get
        mock_req.post.return_value = MagicMock(status_code=200, ok=True, json=lambda: {"results": [], "has_more": False})

        enricher = NotionApiEnricher("secret_test")
        db_node = PageNode(id="4157be77-284d-8021-b09e-f9e9a1a7475d", title="同じ名前", node_type="database")
        summary = {"databases_enriched": 0, "properties_mapped": 0, "views_enriched": 0, "errors": []}
        enricher._enrich_database(db_node, summary)

        self.assertEqual(list(db_node.properties["schema"].keys()), ["項番", "名前", "作成日時"])
        self.assertEqual(db_node.properties["views_config"][0]["quick_filters"], {"CN>o": {"number": {"equals": 1000}}})

    def test_api_views_list_type_falls_back_to_table_and_records_warning_on_failure(self):
        """Notion の 'list' ビューは 'table' へフォールバックし、作成失敗時は telemetry に警告が記録されること"""
        from src.telemetry import SanitizedTelemetry

        importer = self._create_importer()
        tel = SanitizedTelemetry()
        importer.telemetry = tel

        db_node = PageNode(
            id="db-list",
            title="リストDB",
            node_type="database",
            properties={"schema": {"名前": {"id": "title", "type": "title"}}},
        )

        # 1. list -> table フォールバック
        importer.client.create_database_view.return_value = {"id": "v-list", "name": "リスト表示"}
        importer._apply_api_views_config(
            confluence_page_id="conf-db-list",
            node=db_node,
            views_config=[{"id": "v1", "name": "リスト表示", "type": "list", "configuration": {}}],
            existing_views=[],
            single_props=[],
            multi_props=[],
        )
        importer.client.create_database_view.assert_called_once_with(
            "conf-db-list",
            view_name="リスト表示",
            layout="table",
            filters=None,
            sorts=None,
        )
        self.assertEqual(tel.total_warnings_count, 0)

        # 2. 作成失敗時は telemetry に警告を記録
        importer.client.create_database_view.reset_mock()
        importer.client.create_database_view.return_value = None
        importer._apply_api_views_config(
            confluence_page_id="conf-db-list",
            node=db_node,
            views_config=[{"id": "v2", "name": "失敗ビュー", "type": "list", "configuration": {}}],
            existing_views=[],
            single_props=[],
            multi_props=[],
        )
        self.assertEqual(tel.total_warnings_count, 1)
        self.assertIn("失敗ビュー", tel.warnings[0])

    def test_api_views_does_not_duplicate_when_created_pending_and_extracts_all_4_filters(self):
        """反映遅れでcreate_database_viewがNoneでも再確認で見つかれば重複作成せず、4条件フィルタをすべて抽出すること"""
        importer = self._create_importer()
        db_node = PageNode(
            id="db-4f",
            title="4フィルタDB",
            node_type="database",
            properties={
                "schema": {
                    "名前": {"id": "title", "type": "title"},
                    "状態": {"id": "p_st", "type": "select"},
                    "タグ": {"id": "p_tg", "type": "multi_select"},
                    "期日": {"id": "p_dt", "type": "date"},
                    "数値": {"id": "p_num", "type": "number"},
                }
            },
        )
        view_cfg = {
            "id": "v-4f",
            "name": "複合4条件",
            "type": "table",
            "filter": {
                "and": [
                    {"property": "p_st", "select": {"equals": "進行中"}},
                    {"property": "p_tg", "multi_select": {"contains": "重要"}},
                    {"property": "p_dt", "date": {"on_or_before": "2026-12-31"}},
                ]
            },
            "quick_filters": {"p_num": {"number": {"greater_than": 5}}},
            "configuration": {},
        }
        importer.client.create_database_view.return_value = None
        importer.client.get_database_views.return_value = [{"id": "v-created", "name": "複合4条件", "type": "table"}]

        importer._apply_api_views_config(
            confluence_page_id="conf-db-4f",
            node=db_node,
            views_config=[view_cfg],
            existing_views=[],
            single_props=["状態"],
            multi_props=["タグ"],
        )

        # 1回のみ呼ばれ、フォールバックの2回目は呼ばれないこと
        self.assertEqual(importer.client.create_database_view.call_count, 1)
        sent_filters = importer.client.create_database_view.call_args.kwargs["filters"]
        self.assertEqual(
            sent_filters,
            [
                {"field": "状態", "operator": "=", "value": "進行中"},
                {"field": "タグ", "operator": "contains", "value": "重要"},
                {"field": "期日", "operator": "<=", "value": "2026-12-31"},
                {"field": "数値", "operator": ">", "value": "5"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
