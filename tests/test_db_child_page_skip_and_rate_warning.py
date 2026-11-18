"""TCK_00023: 本文なしDBの子ページ作成スキップ(text列化)と低子ページ存在率WARNINGのテスト。"""

import io
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import migrate
from src.checkpoint import MigrationCheckpoint
from src.converter import NotionToConfluenceConverter
from src.extractor import (
    PageNode,
    analyze_database_child_page_stats,
    collect_low_child_rate_databases,
    database_row_has_substantive_body,
)
from src.importer import ConfluenceImporter
from src.telemetry import SanitizedTelemetry
from src.verifier import MigrationVerifier
from tests.storage_xml_helper import assert_valid_storage_xml


def _make_row(row_id: str, title: str, has_body: bool = False, has_nested_child: bool = False) -> PageNode:
    blocks = (
        [{"id": f"{row_id}_p", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "plain_text": "詳細本文", "text": {"content": "詳細本文"}}]}}]
        if has_body
        else [{"id": f"{row_id}_empty", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "plain_text": "   ", "text": {"content": "   "}}]}}]
    )
    children = (
        [PageNode(id=f"{row_id}_sub", title=f"{title} Sub", node_type="page", blocks=[])]
        if has_nested_child
        else []
    )
    return PageNode(
        id=row_id,
        title=title,
        node_type="database_row",
        properties={
            "名前": {"type": "title", "value": title},
            "ステータス": {"type": "select", "value": "進行中"},
        },
        blocks=blocks,
        children=children,
    )


class TestDbChildPageSkipAndRateWarning(unittest.TestCase):
    def test_substantive_body_and_stats_calculation(self):
        rows = [
            _make_row(f"r{i}", f"Item {i}", has_body=(i == 1), has_nested_child=(i == 2))
            for i in range(1, 11)
        ]
        # 完全に空の無題レコードは無視されること
        rows.append(
            PageNode(
                id="r_empty",
                title="Untitled",
                node_type="database_row",
                properties={},
                blocks=[],
            )
        )
        db = PageNode(
            id="db-1",
            title="マスタDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}, "ステータス": {"type": "select"}}},
            children=rows,
        )

        self.assertTrue(database_row_has_substantive_body(rows[0]))
        self.assertTrue(database_row_has_substantive_body(rows[1]))
        self.assertFalse(database_row_has_substantive_body(rows[2]))

        stats = analyze_database_child_page_stats(db)
        self.assertEqual(stats["total_rows"], 10)
        self.assertEqual(stats["body_rows"], 2)
        self.assertAlmostEqual(stats["body_ratio_pct"], 20.0)
        self.assertFalse(stats["skip_child_pages"])

        # 20.0% < 20.0% は False（境界値は警告対象外）
        self.assertEqual(collect_low_child_rate_databases(db, threshold_pct=20.0), [])
        # 25.0% 未満にすると検出されること
        low = collect_low_child_rate_databases(db, threshold_pct=25.0)
        self.assertEqual(len(low), 1)
        self.assertIn("2/10件", low[0]["message"])

    def test_zero_body_db_skips_child_pages_and_uses_text_title_and_fallback_links(self):
        """本文あり行が0件のDBでは子ページを作成せず、タイトル列をtext型にし、行参照リンクを親DBへフォールバックすること"""
        row1 = _make_row("row-001", "Master Row A", has_body=False)
        row2 = _make_row("row-002", "Master Row B", has_body=False)
        db_node = PageNode(
            id="db-zero",
            title="本文なしマスタDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}, "ステータス": {"type": "select"}}},
            children=[row1, row2],
        )
        ref_page = PageNode(
            id="page-ref",
            title="参照元ページ",
            node_type="page",
            blocks=[
                {
                    "id": "p_rel",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "plain_text": "参照行: ", "text": {"content": "参照行: "}},
                            {"type": "mention", "mention": {"type": "page", "page": {"id": "row-001"}}, "plain_text": "@Master Row A"},
                        ]
                    },
                }
            ],
            properties={
                "関連レコード": {"type": "relation", "relations": [{"id": "row-002", "text": "Master Row B"}]},
            },
        )
        root = PageNode(
            id="root-1",
            title="Root",
            node_type="page",
            blocks=[],
            children=[db_node, ref_page],
        )

        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None
        mock_client.get_space_id.return_value = "space-1"
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]

        created_pages = []

        def _create_page(space_key, title, body, parent_id=None, **kwargs):
            pid = f"p_{len(created_pages) + 100}"
            created_pages.append({"id": pid, "title": title, "body": body})
            return {"id": pid, "_links": {"webui": f"/spaces/{space_key}/pages/{pid}"}}

        mock_client.create_page.side_effect = _create_page
        created_dbs = []

        def _create_db(space_id, title, parent_id=None, csv_body="", **kwargs):
            did = "db_900"
            created_dbs.append({"id": did, "title": title, "csv_body": csv_body})
            return {"id": did, "_links": {"webui": f"/spaces/DOCS/database/{did}"}}

        mock_client.create_database.side_effect = _create_db
        updated_bodies = {}
        mock_client.get_page_by_id.side_effect = lambda pid, expand=None: {
            "id": pid,
            "title": next((p["title"] for p in created_pages if p["id"] == pid), "Page"),
            "version": {"number": 2 if pid in updated_bodies else 1},
            "ancestors": [{"id": "parent-0" if pid == "p_100" else "p_100"}],
            "body": {"storage": {"value": updated_bodies.get(pid, next((p["body"] for p in created_pages if p["id"] == pid), ""))}},
        }
        mock_client.get_database_by_id.return_value = {
            "id": "db_900",
            "title": "本文なしマスタDB",
            "_links": {"webui": "/spaces/DOCS/database/db_900"},
        }
        mock_client.get_database_entries.return_value = [{"id": "e1"}, {"id": "e2"}]
        mock_client.get_database_fields.return_value = [
            {"name": "名前", "type": "text"},
            {"name": "ステータス", "type": "tag"},
        ]

        mock_client.update_page.side_effect = lambda page_id, space_key, title, body, current_version, **kw: updated_bodies.update({page_id: body}) or {"id": page_id}
        mock_client.auth = ("user", "tok")
        mock_client.get_page_parent_id.side_effect = lambda pid: "parent-0" if pid == "p_100" else "p_100"

        importer = ConfluenceImporter("https://example.atlassian.net/wiki", "user", "tok", space_key="DOCS")
        importer.client = mock_client
        mapping = importer.import_tree(root, parent_id="parent-0", overwrite=False)

        # DB 行の子ページは作成されていないこと（Root と 参照元ページ のみ）
        created_titles = [p["title"] for p in created_pages]
        self.assertNotIn("Master Row A", created_titles)
        self.assertNotIn("Master Row B", created_titles)
        self.assertEqual(created_titles, ["Root", "参照元ページ"])

        # CSV のタイトル列が confluence-page-link ではなく text 型になっていること
        self.assertEqual(len(created_dbs), 1)
        csv_body = created_dbs[0]["csv_body"]
        self.assertIn("名前,text,", csv_body)
        self.assertNotIn("confluence-page-link", csv_body)
        self.assertIn("Master Row A,", csv_body)
        self.assertIn("Master Row B,", csv_body)

        # 参照元ページの Pass 2 本文で、未作成DB行リンクが親DB URLへフォールバックしていること
        ref_conf_id = mapping["page-ref"]["confluence_id"]
        ref_final_body = updated_bodies[ref_conf_id]
        assert_valid_storage_xml(self, ref_final_body, label="参照元ページ Pass 2")
        self.assertIn("https://example.atlassian.net/wiki/spaces/DOCS/database/db_900", ref_final_body)
        self.assertNotIn('<ri:page ri:content-title="Master Row A"', ref_final_body)

        # MigrationVerifier でも FAIL / WARNING にならず PASS になること
        fake_db_res = MagicMock(ok=True)
        root_conf_id = mapping["root-1"]["confluence_id"]
        fake_db_res.json.return_value = {"id": "db_900", "title": "本文なしマスタDB", "parentId": root_conf_id}
        verifier = MigrationVerifier(mock_client)
        page_id_map = {nid: item["confluence_id"] for nid, item in mapping.items()}
        with patch("src.verifier.requests.get", return_value=fake_db_res):
            results = verifier.verify_tree(root, page_id_map, "parent-0")
        db_res = next(r for r in results if r["title"] == "本文なしマスタDB")
        self.assertEqual(db_res["status"], "PASS", f"Unexpected DB result: {db_res}")
        self.assertFalse(any(r["status"] == "FAIL" for r in results), f"Unexpected FAIL: {results}")

    def test_low_child_rate_warning_in_verifier_telemetry_and_cli_option(self):
        """子ページ存在率が閾値未満 (0%超〜デフォルト20%未満) で WARNING が出力され、CLI オプションで変更できること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "low_rate.zip")
            db_id = "dddd0000000000000000000000000001"
            root_id = "aaaa0000000000000000000000000001"

            csv_lines = ["名前,メモ"]
            entries = {
                f"Root {root_id}.html": (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Root</title></head>"
                    f"<body><header><h1 class='page-title'>Root</h1></header>"
                    f"<div class='page-body'><figure class='link-to-page'><a href='SparseDB {db_id}.html'>SparseDB</a></figure></div></body></html>"
                ),
                f"Root/SparseDB {db_id}.html": (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>SparseDB</title></head>"
                    "<body><header><h1 class='page-title'>SparseDB</h1></header><div class='page-body'></div></body></html>"
                ),
            }
            for i in range(1, 11):
                rid = f"cccc00000000000000000000000000{i:02d}"
                rtitle = f"Row-{i:02d}"
                csv_lines.append(f"{rtitle},memo-{i}")
                body_content = "<p>実本文あり</p>" if i == 1 else ""
                entries[f"Root/SparseDB/{rtitle} {rid}.html"] = (
                    f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{rtitle}</title></head>"
                    f"<body><header><h1 class='page-title'>{rtitle}</h1></header>"
                    f"<div class='page-body'>{body_content}</div></body></html>"
                )
            entries[f"Root/SparseDB {db_id}.csv"] = ("\n".join(csv_lines) + "\n").encode("utf-8-sig")

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for k, v in entries.items():
                    zf.writestr(k, v.encode("utf-8") if isinstance(v, str) else v)

            cp_default = os.path.join(tmpdir, "cp_default.json")
            orig_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                # 1. デフォルト閾値 (20.0%): 1/10 = 10.0% < 20.0% -> WARNING 表示
                buf_default = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--checkpoint", cp_default]), \
                     redirect_stdout(buf_default):
                    migrate.main()
                out_default = buf_default.getvalue()
                self.assertIn("子ページ存在率が閾値 (20.0%) 未満", out_default)
                self.assertIn("1/10件, 10.0% < 20.0%", out_default)

                # 2. --db-child-rate-threshold 10.0: 10.0% < 10.0% は False -> 警告なし
                cp_10 = os.path.join(tmpdir, "cp_10.json")
                buf_10 = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--db-child-rate-threshold", "10", "--checkpoint", cp_10]), \
                     redirect_stdout(buf_10):
                    migrate.main()
                out_10 = buf_10.getvalue()
                self.assertNotIn("子ページ存在率が閾値", out_10)

                # 3. --db-child-rate-threshold 0: 無効化
                cp_0 = os.path.join(tmpdir, "cp_0.json")
                buf_0 = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--db-child-rate-threshold", "0", "--checkpoint", cp_0]), \
                     redirect_stdout(buf_0):
                    migrate.main()
                self.assertNotIn("子ページ存在率が閾値", buf_0.getvalue())
            finally:
                os.chdir(orig_cwd)

        # Telemetry のみでの境界値確認
        db = PageNode(
            id="db-t",
            title="Sparse",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[_make_row(f"rt{i}", f"R{i}", has_body=(i == 1)) for i in range(1, 11)],
        )
        tel = SanitizedTelemetry()
        tel.record_tree(db, db_child_rate_threshold=20.0)
        self.assertEqual(tel.low_child_rate_databases_count, 1)
        self.assertEqual(tel.skipped_bodyless_dbs_count, 0)

    def test_skipped_db_rows_excluded_from_dashboard_and_pass2_node_counts(self):
        """TCK_00030: 子ページを作成しないDB行が dashboard.start(total_nodes) および Pass 2 の対象ノード数に算入されないこと。"""
        db_skipped = PageNode(
            id="db-skip",
            title="マスタDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[
                _make_row("r1", "Row 1", has_body=False),
                _make_row("r2", "Row 2", has_body=False),
                _make_row("r3", "Row 3", has_body=False),
            ],
        )
        root = PageNode(id="root-count", title="Root", node_type="page", children=[db_skipped])

        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_space_id.return_value = "space-1"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None
        mock_client.create_page.return_value = {"id": "p-root", "_links": {"webui": "/spaces/D/pages/p-root"}}
        mock_client.create_database.return_value = {"id": "d-skip", "_links": {"webui": "/spaces/D/database/d-skip"}}
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = []
        mock_client.get_page_by_id.return_value = {"id": "p-root", "version": {"number": 1}}

        mock_dashboard = MagicMock()
        importer = ConfluenceImporter("https://example.atlassian.net/wiki", "user", "tok", space_key="D", dashboard=mock_dashboard)
        importer.client = mock_client

        mapping = importer.import_tree(root, parent_id="parent-0", overwrite=False)

        # Root(1) + マスタDB(1) = 2 ノードのみが算入され、スキップされた3行は含まれないこと
        mock_dashboard.start.assert_called_once_with(2)
        self.assertEqual(len(mapping), 2)
        pass2_skipped_titles = {c.args[0] for c in mock_dashboard.record_pass2_skip.call_args_list}
        self.assertEqual(pass2_skipped_titles, {"Root", "マスタDB"})

    def test_low_rate_db_text_key_option_creates_only_body_rows_and_appends_link_to_text_key(self):
        """TCK_00034: low_rate_db_text_key有効かつ存在率が閾値以下の場合、キーカラムがtext型になり本文あり行のみ子ページ作成・リンク文字列追加されること。"""
        rows = [_make_row("r_body", "本文あり行", has_body=True)] + [
            _make_row(f"r_nob_{i}", f"本文なし行{i}", has_body=False) for i in range(1, 10)
        ]  # 1/10 = 10.0% <= 20.0%
        db_low = PageNode(
            id="db-low-text",
            title="低存在率DB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}, "メモ": {"type": "rich_text"}}},
            children=rows,
        )
        root = PageNode(id="root-low", title="Root", node_type="page", children=[db_low])

        created_pages = []
        created_dbs = []
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_space_id.return_value = "space-1"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None

        def fake_create_page(space_key, title, body, parent_id=None):
            pid = f"p_{len(created_pages) + 1}"
            created_pages.append({"id": pid, "title": title})
            return {"id": pid, "_links": {"webui": f"/spaces/{space_key}/pages/{pid}"}}

        def fake_create_db(space_id, title, parent_id, csv_body):
            created_dbs.append({"title": title, "csv_body": csv_body})
            return {"id": "db_low_1", "_links": {"webui": "/spaces/D/database/db_low_1"}}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.create_database.side_effect = fake_create_db
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = []
        mock_client.get_page_by_id.return_value = {
            "id": "p_2",
            "title": "本文あり行",
            "ancestors": [{"id": "db_low_1"}],
            "body": {"storage": {"value": "<p>Body text for 本文あり行</p>"}, "view": {"value": "<p>Body text for 本文あり行</p>"}},
            "version": {"number": 1},
        }
        mock_client.auth = ("user", "tok")

        mock_dashboard = MagicMock()
        importer = ConfluenceImporter(
            "https://example.atlassian.net/wiki",
            "user",
            "tok",
            space_key="D",
            dashboard=mock_dashboard,
            db_child_rate_threshold=20.0,
            low_rate_db_text_key=True,
        )
        importer.client = mock_client

        mapping = importer.import_tree(root, parent_id="parent-0", overwrite=False)

        # 1. Root(1) + 低存在率DB(1) + 本文あり行(1) = 3 ノードのみ作成・算入
        mock_dashboard.start.assert_called_once_with(3)
        created_titles = [p["title"] for p in created_pages]
        self.assertEqual(created_titles, ["Root", "本文あり行"])
        self.assertEqual(len(mapping), 3)

        # 2. DB CSVのキーカラムが text 型、右隣に非表示のリンク型カラム(詳細ページ)が配置され、本文あり行のみURLが入ること
        csv_body = created_dbs[0]["csv_body"]
        self.assertIn("名前,text,", csv_body)
        self.assertIn("詳細ページ,confluence-page-link,", csv_body)
        self.assertIn('すべてのデータ,table,"","","詳細ページ"', csv_body)
        self.assertIn('本文あり行,https://example.atlassian.net/wiki/spaces/D/pages/p_2,""', csv_body)
        self.assertIn('本文なし行1,"",""', csv_body)

        # 3. MigrationVerifier でもスキップ行で FAIL せず PASS になること
        fake_db_res = MagicMock(ok=True)
        fake_db_res.json.return_value = {"id": "db_low_1", "title": "低存在率DB", "parentId": "p_1"}
        mock_client.get_page_by_id.side_effect = lambda pid, expand=None: (
            {"id": "p_1", "title": "Root", "ancestors": [{"id": "parent-0"}], "body": {"storage": {"value": "<p>r</p>"}, "view": {"value": "<p>r</p>"}}}
            if pid == "p_1"
            else {"id": "p_2", "title": "本文あり行", "ancestors": [{"id": "db_low_1"}], "body": {"storage": {"value": "<p>Body text for 本文あり行</p>"}, "view": {"value": "<p>Body text for 本文あり行</p>"}}}
        )
        verifier = MigrationVerifier(mock_client, db_child_rate_threshold=20.0)
        page_id_map = {nid: item["confluence_id"] for nid, item in mapping.items()}
        with patch("src.verifier.requests.get", return_value=fake_db_res):
            results = verifier.verify_tree(root, page_id_map, "parent-0")
        self.assertFalse(any(r["status"] == "FAIL" for r in results), f"Unexpected FAIL: {results}")


if __name__ == "__main__":
    unittest.main()
