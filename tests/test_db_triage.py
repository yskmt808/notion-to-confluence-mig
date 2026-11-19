"""DB移行方式トリアージ (--db-triage / --import-db-triage) の単体・結合テスト (TCK_00037)。"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import migrate
from src.db_triage import (
    collect_db_triage_rows,
    export_db_triage_csv,
    format_db_triage_table,
    load_db_triage_overrides,
)
from src.extractor import PageNode, count_migratable_nodes
from src.importer import ConfluenceImporter


def _row(rid: str, title: str, has_body: bool = False, block_count: int = 1) -> PageNode:
    blocks = (
        [{"id": f"b-{rid}-{i}", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": f"text {i}"}]}} for i in range(block_count)]
        if has_body
        else []
    )
    return PageNode(
        id=rid,
        title=title,
        node_type="database_row",
        properties={"名前": {"type": "title", "value": title}},
        blocks=blocks,
    )


class TestDbTriage(unittest.TestCase):
    def test_collect_export_load_and_invalid_mode(self):
        db_small = PageNode(
            id="aaaa0000-0000-0000-0000-000000000001",
            title="議事録DB",
            node_type="database",
            children=[_row("r1", "定例1", has_body=True, block_count=3)],
        )
        db_large = PageNode(
            id="aaaa0000-0000-0000-0000-000000000002",
            title="巨大ログDB",
            node_type="database",
            children=[_row(f"rl{i}", f"Log-{i}", has_body=(i < 10), block_count=2) for i in range(300)],
        )
        root = PageNode(
            id="root-1",
            title="Root",
            node_type="page",
            blocks=[
                {"type": "child_database", "is_linked_database": True, "database_id": "aaaa0000-0000-0000-0000-000000000001"},
                {"type": "link_to_page", "link_to_page": {"database_id": "aaaa0000-0000-0000-0000-000000000001"}},
            ],
            children=[db_small, db_large],
        )

        rows = collect_db_triage_rows(root, individual_db_child_threshold=5)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("parent_path", rows[0])
        self.assertEqual(rows[0]["migration_mode"], "individual")
        self.assertEqual(rows[0]["embedded_view_count"], 2)
        self.assertIn("小規模DB", rows[0]["recommendation"])
        self.assertEqual(rows[1]["migration_mode"], "aggregated")
        self.assertEqual(rows[1]["total_rows"], 300)
        self.assertEqual(rows[1]["total_body_blocks"], 20)
        self.assertEqual(rows[1]["embedded_view_count"], 0)
        self.assertIn("大規模DB注意", rows[1]["recommendation"])

        table_str = format_db_triage_table(rows)
        self.assertIn("議事録DB", table_str)
        self.assertIn("埋め込み: 2件", table_str)
        self.assertIn("巨大ログDB", table_str)

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "triage.csv")
            export_db_triage_csv(rows, csv_path)
            loaded = load_db_triage_overrides(csv_path)
            self.assertEqual(loaded["aaaa0000000000000000000000000001"], "individual")
            self.assertEqual(loaded["aaaa0000000000000000000000000002"], "aggregated")

            bad_csv = os.path.join(tmpdir, "bad.csv")
            with open(bad_csv, "w", encoding="utf-8") as f:
                f.write("db_id,migration_mode\naaaa0000000000000000000000000001,invalid_mode\n")
            with self.assertRaises(ValueError):
                load_db_triage_overrides(bad_csv)

    def test_count_and_importer_mode_overrides_skip_and_static_table(self):
        db_skip = PageNode(
            id="db-skip-id",
            title="除外DB",
            node_type="database",
            children=[_row("rs1", "Row S1", has_body=True)],
        )
        db_static = PageNode(
            id="db-static-id",
            title="静的テーブルDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[_row("rst1", "Row ST1", has_body=True)],
        )
        root = PageNode(id="root-1", title="Root", node_type="page", children=[db_skip, db_static])

        overrides = {"db-skip-id": "skip", "db-static-id": "static_table"}
        # Root(1) + 除外DB(0) + 静的テーブルDB本体(1) + 集約ページ(1) = 3
        self.assertEqual(count_migratable_nodes(root, db_mode_overrides=overrides), 3)

        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None
        created_pages = []

        def fake_create_page(space_key, title, body, parent_id=None):
            pid = f"p_{len(created_pages) + 1}"
            created_pages.append({"id": pid, "title": title, "parent_id": parent_id, "body": body})
            return {"id": pid, "_links": {"webui": f"/spaces/{space_key}/pages/{pid}"}}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.get_page_by_id.return_value = {"id": "p_1", "version": {"number": 1}}

        mock_dashboard = MagicMock()
        importer = ConfluenceImporter(
            "https://example.atlassian.net/wiki",
            "user",
            "tok",
            space_key="D",
            dashboard=mock_dashboard,
            db_mode_overrides=overrides,
        )
        importer.client = mock_client

        mapping = importer.import_tree(root, parent_id="parent-0", overwrite=False)

        mock_dashboard.start.assert_called_once_with(3)
        mock_client.create_database.assert_not_called()
        created_titles = [p["title"] for p in created_pages]
        self.assertEqual(created_titles, ["Root", "静的テーブルDB", "静的テーブルDB - 詳細ページ一覧"])
        self.assertNotIn("db-skip-id", mapping)
        self.assertIn("db-static-id", mapping)

    def test_cli_dry_run_db_triage_option_b_and_import_db_triage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "sample.zip")
            db_id = "bbbb0000000000000000000000000001"
            rid = "cccc0000000000000000000000000001"
            entries = {
                "Root aaaa0000000000000000000000000001.html": (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Root</title></head>"
                    "<body><header><h1 class='page-title'>Root</h1></header>"
                    f"<div class='page-body'><a href='MyDB%20{db_id}.csv'>MyDB</a></div></body></html>"
                ),
                f"Root/MyDB {db_id}.csv": ("\ufeff名前,メモ\nRow1,memo\n").encode("utf-8"),
                f"Root/MyDB/Row1 {rid}.html": (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Row1</title></head>"
                    "<body><header><h1 class='page-title'>Row1</h1></header>"
                    "<div class='page-body'><p>詳細本文</p></div></body></html>"
                ),
            }
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for k, v in entries.items():
                    zf.writestr(k, v.encode("utf-8") if isinstance(v, str) else v)

            orig_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                # 1. 通常の --dry-run ではトリアージ表・CSVを出力しない (案B)
                buf_plain = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard"]), \
                     contextlib.redirect_stdout(buf_plain):
                    migrate.main()
                self.assertNotIn("[DB Triage]", buf_plain.getvalue())
                self.assertFalse(os.path.exists(os.path.join(tmpdir, "reports", "db_triage.csv")))

                # 2. --dry-run --db-triage 指定時はテーブル表示＆CSV出力
                triage_csv = os.path.join(tmpdir, "custom_triage.csv")
                buf_triage = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--db-triage", triage_csv]), \
                     contextlib.redirect_stdout(buf_triage):
                    migrate.main()
                self.assertIn("[DB Triage]", buf_triage.getvalue())
                self.assertTrue(os.path.exists(triage_csv))

                # 3. 出力したCSVを skip に編集して --import-db-triage で読み込めること
                with open(triage_csv, "r", encoding="utf-8-sig") as f:
                    content = f.read().replace(",aggregated,", ",skip,")
                with open(triage_csv, "w", encoding="utf-8-sig") as f:
                    f.write(content)

                buf_imp = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--import-db-triage", triage_csv]), \
                     contextlib.redirect_stdout(buf_imp):
                    migrate.main()
                self.assertIn("DBトリアージ上書き定義を読み込みました", buf_imp.getvalue())
            finally:
                os.chdir(orig_cwd)

    def test_static_table_embed_prefers_uuid_over_same_name_database(self):
        """static_table (node_type=page) として移行されたDBの埋め込みが同名別DBに奪われないこと"""
        from src.converter import NotionToConfluenceConverter
        from src.extractor import PageNode

        conv = NotionToConfluenceConverter()
        conv.migration_map = {
            "4157be77-284d-8021-b09e-f9e9a1a7475d": {
                "node_type": "database",
                "title": "同じ名前",
                "url": "https://example.atlassian.net/wiki/spaces/S/database/7110696",
                "views": {},
            },
            "4167be77-284d-8019-9cbe-e3bfa907e63e": {
                "node_type": "page",
                "title": "同じ名前 (2)",
                "url": "https://example.atlassian.net/wiki/spaces/S/pages/7045206",
            },
        }
        page = PageNode(
            id="parent-1",
            title="親ページ",
            node_type="page",
            blocks=[
                {
                    "id": "4167be77-284d-8019-9cbe-e3bfa907e63e",
                    "type": "child_database",
                    "is_linked_database": True,
                    "database_id": "4167be77-284d-8019-9cbe-e3bfa907e63e",
                    "data_sources": [{"name": "同じ名前", "id": "4167be77-284d-8019-9cbe-e3bfa907e63e"}],
                    "views": [{"name": "同じ名前", "type": "table"}],
                }
            ],
        )
        html_out = conv.generate_page_content(page)
        self.assertIn("https://example.atlassian.net/wiki/spaces/S/pages/7045206", html_out)
        self.assertNotIn("7110696", html_out)

    def test_static_table_overview_has_no_detailssummary_and_links_only_existing_details(self):
        """static_tableページにdetailssummaryや壊れた子ページリンクが出ず、詳細がある行のみリンク化されること"""
        from src.converter import NotionToConfluenceConverter
        from src.extractor import PageNode

        conv = NotionToConfluenceConverter()
        conv.migration_map = {
            "row-with-body": {
                "confluence_id": "999",
                "title": "raw1",
                "url": "https://example.atlassian.net/wiki/spaces/S/pages/999#raw1",
                "node_type": "database_row",
            }
        }
        db_node = PageNode(
            id="db-static",
            title="同じ名前 (2)",
            node_type="database",
            properties={"title_column": "名前", "schema": {"名前": {"type": "title"}, "テキスト": {"type": "rich_text"}}},
            children=[
                PageNode(
                    id="row-with-body",
                    title="raw1",
                    node_type="database_row",
                    properties={"名前": {"type": "title", "value": "raw1"}, "テキスト": {"type": "rich_text", "value": "aaa"}},
                ),
                PageNode(
                    id="row-no-body",
                    title="raw2",
                    node_type="database_row",
                    properties={"名前": {"type": "title", "value": "raw2"}, "テキスト": {"type": "rich_text", "value": "bbb"}},
                ),
            ],
        )
        html_out = conv._generate_database_overview_content(db_node)
        self.assertNotIn("detailssummary", html_out)
        self.assertNotIn("動的連動テーブル", html_out)
        self.assertNotIn("ri:content-title", html_out)
        self.assertIn('href="https://example.atlassian.net/wiki/spaces/S/pages/999#raw1"', html_out)
        self.assertIn("<td>raw2</td>", html_out)

    def test_html_extractor_resolves_short_uuid_db_row_directory(self):
        """同名DB衝突時にNotionが生成する `<DB名> <先頭4桁>-<末尾4桁>/` 配下の行HTMLを正しく読み込むこと"""
        import zipfile
        from src.html_extractor import HtmlZipExtractor
        from src.importer import database_row_has_substantive_body

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "short_uuid_db.zip")
            db1_id = "4157be77284d8021b09ef9e9a1a7475d"
            db2_id = "4167be77284d80199cbee3bfa907e63e"
            row_id = "41d7be77284d8079a310e1733e48a15a"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(
                    "親ページ 4137be77284d801d9fcfc13c0989f376.html",
                    '<html><body><article><header><h1 class="page-title">親ページ</h1></header><div class="page-body"></div></article></body></html>',
                )
                zf.writestr(
                    f"親ページ/同じ名前 {db1_id}.csv",
                    "名前\nItemA\n",
                )
                zf.writestr(
                    f"親ページ/同じ名前 {db2_id}.csv",
                    "名前\nraw6\n",
                )
                zf.writestr(
                    f"親ページ/同じ名前 4167-e63e/raw6 {row_id}.html",
                    f'<html><head><title>raw6</title></head><body><article id="{row_id}" class="page"><header><h1 class="page-title">raw6</h1></header><div class="page-body"><p>詳細テスト</p></div></article></body></html>',
                )

            tree = HtmlZipExtractor(zip_path).extract_tree()
            db2 = next(c for c in tree.children if c.node_type == "database" and c.id.replace("-", "") == db2_id)
            self.assertEqual(len(db2.children), 1)
            self.assertEqual(len(db2.children[0].blocks), 1)
            self.assertTrue(database_row_has_substantive_body(db2.children[0]))


if __name__ == "__main__":
    unittest.main()
