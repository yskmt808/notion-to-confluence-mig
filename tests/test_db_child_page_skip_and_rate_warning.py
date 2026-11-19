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
                # 1. デフォルト (--individual-db-child-threshold 0): 1ページ集約方式 (警告なし・正常完了)
                buf_default = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--checkpoint", cp_default]), \
                     redirect_stdout(buf_default):
                    migrate.main()
                out_default = buf_default.getvalue()
                self.assertNotIn("子ページ存在率が閾値", out_default)
                self.assertIn("DRY RUN モードのため", out_default)

                # 2. --individual-db-child-threshold 1: 閾値以下なので個別子ページ方式が選択可能
                cp_1 = os.path.join(tmpdir, "cp_1.json")
                buf_1 = io.StringIO()
                with patch("sys.argv", ["migrate.py", "--zip", zip_path, "--dry-run", "--no-dashboard", "--individual-db-child-threshold", "1", "--checkpoint", cp_1]), \
                     redirect_stdout(buf_1):
                    migrate.main()
                out_1 = buf_1.getvalue()
                self.assertIn("DRY RUN モードのため", out_1)
            finally:
                os.chdir(orig_cwd)

        # Telemetry & collect_tree_stats の確認 (デフォルト1ページ集約=2, 個別子ページ=11)
        db = PageNode(
            id="db-t",
            title="Sparse",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[_make_row(f"rt{i}", f"R{i}", has_body=(i == 1)) for i in range(1, 11)],
        )
        self.assertEqual(migrate.collect_tree_stats(db)["total"], 2)
        self.assertEqual(migrate.collect_tree_stats(db, individual_db_child_threshold=1)["total"], 11)
        tel = SanitizedTelemetry()
        tel.record_tree(db)
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

    def test_default_aggregated_1page_mode_creates_single_detail_page_with_anchor_and_entryid_links(self):
        """TCK_00036: デフォルトで1ページ集約方式となり、textキー+非表示hyperlink列(#見出しアンカー)+?entryId=戻りリンク(静的プロパティ表なし)が出力されること。"""
        rows = [_make_row("r_body", "本文あり行", has_body=True)] + [
            _make_row(f"r_nob_{i}", f"本文なし行{i}", has_body=False) for i in range(1, 10)
        ]
        db_agg = PageNode(
            id="db-agg-1",
            title="集約対象DB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}, "メモ": {"type": "rich_text"}}},
            children=rows,
        )
        root = PageNode(id="root-agg", title="Root", node_type="page", children=[db_agg])

        created_pages = []
        updated_pages = []
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

        def fake_update_page(page_id, space_key, title, body, current_version, parent_id=None):
            updated_pages.append({"id": page_id, "title": title, "body": body})
            return {"id": page_id}

        def fake_create_db(space_id, title, parent_id, csv_body):
            created_dbs.append({"title": title, "csv_body": csv_body})
            return {"id": "db_agg_conf_1", "_links": {"webui": "/spaces/D/database/db_agg_conf_1"}}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.update_page.side_effect = fake_update_page
        mock_client.create_database.side_effect = fake_create_db
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = [
            {"id": "entry-agg-1", "values": {"f_title": {"type": "text", "plain": "📄 本文あり行"}}}
        ]
        mock_client.get_page_by_id.return_value = {
            "id": "p_2",
            "title": "集約対象DB - 詳細ページ一覧",
            "ancestors": [{"id": "p_1"}],
            "body": {"storage": {"value": "<p>agg</p>"}, "view": {"value": "<p>agg</p>"}},
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
        )
        importer.client = mock_client

        mapping = importer.import_tree(root, parent_id="parent-0", overwrite=False)

        # 1. Root(1) + 集約詳細ページ(1) + 集約対象DB(1) = 3 ノード作成
        mock_dashboard.start.assert_called_once_with(3)
        created_titles = [p["title"] for p in created_pages]
        self.assertEqual(created_titles, ["Root", "集約対象DB - 詳細ページ一覧"])

        # 2. DB CSVのキーカラムが text 型、右隣に非表示の hyperlink カラム(詳細ページ)が配置され、#アンカー付きURLが入ること
        csv_body = created_dbs[0]["csv_body"]
        self.assertIn("名前,text,", csv_body)
        self.assertIn("詳細ページ,hyperlink,", csv_body)
        self.assertIn('すべてのデータ,table,"","","詳細ページ"', csv_body)
        self.assertIn("📄 本文あり行,https://example.atlassian.net/wiki/spaces/D/pages/p_2#", csv_body)
        self.assertIn('本文なし行1,"",""', csv_body)

        # 3. Pass 2で更新された集約ページに?entryId=戻りリンクがあり、静的プロパティ折りたたみ表がないこと
        agg_updates = [u for u in updated_pages if u["id"] == "p_2"]
        self.assertEqual(len(agg_updates), 1)
        agg_body = agg_updates[0]["body"]
        self.assertIn("<h2>本文あり行</h2>", agg_body)
        self.assertIn("db_agg_conf_1?entryId=entry-agg-1", agg_body)
        self.assertIn("DBの該当レコードを直接開く", agg_body)
        self.assertNotIn("データベースプロパティ", agg_body)
        self.assertNotIn("レコードプロパティ・行編集", agg_body)

        # 4. MigrationVerifier でも PASS になること
        fake_db_res = MagicMock(ok=True)
        fake_db_res.json.return_value = {"id": "db_agg_conf_1", "title": "集約対象DB", "parentId": "p_1"}
        mock_client.get_page_by_id.side_effect = lambda pid, expand=None: (
            {"id": "p_1", "title": "Root", "ancestors": [{"id": "parent-0"}], "body": {"storage": {"value": "<p>r</p>"}, "view": {"value": "<p>r</p>"}}}
            if pid == "p_1"
            else {"id": "p_2", "title": "集約対象DB - 詳細ページ一覧", "ancestors": [{"id": "p_1"}], "body": {"storage": {"value": agg_body}, "view": {"value": agg_body}}}
        )
        verifier = MigrationVerifier(mock_client)
        page_id_map = {nid: item["confluence_id"] for nid, item in mapping.items()}
        with patch("src.verifier.requests.get", return_value=fake_db_res):
            results = verifier.verify_tree(root, page_id_map, "parent-0")
        self.assertFalse(any(r["status"] == "FAIL" for r in results), f"Unexpected FAIL: {results}")

    def test_aggregated_detail_page_uploads_and_disambiguates_row_images(self):
        """1ページ集約方式で複数行に同名画像(Untitled.png)があっても重複回避して集約ページへアップロード＆埋め込みされること。"""
        row1 = PageNode(
            id="r-img-1",
            title="行1",
            node_type="database_row",
            properties={"名前": {"type": "title", "value": "行1"}},
            blocks=[{
                "id": "b-img-1",
                "type": "image",
                "image": {"type": "file", "file": {"url": "img1/Untitled.png"}, "caption": []},
                "_attachment_filename": "Untitled.png",
                "_attachment_bytes": b"PNGDATA_1",
            }],
        )
        row2 = PageNode(
            id="r-img-2",
            title="行2",
            node_type="database_row",
            properties={"名前": {"type": "title", "value": "行2"}},
            blocks=[{
                "id": "b-img-2",
                "type": "image",
                "image": {"type": "file", "file": {"url": "img2/Untitled.png"}, "caption": []},
                "_attachment_filename": "Untitled.png",
                "_attachment_bytes": b"PNGDATA_2",
            }],
        )
        db = PageNode(
            id="db-img",
            title="画像ありDB",
            node_type="database",
            properties={"schema": {"名前": {"type": "title"}}},
            children=[row1, row2],
        )
        root = PageNode(id="root-img", title="Root", node_type="page", children=[db])

        created_pages = []
        updated_pages = []
        uploaded = []
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.get_space_id.return_value = "space-1"
        mock_client.get_page_by_title.return_value = None
        mock_client.get_database_by_title.return_value = None

        def fake_create_page(space_key, title, body, parent_id=None):
            pid = f"p_{len(created_pages) + 1}"
            created_pages.append({"id": pid, "title": title})
            return {"id": pid, "_links": {"webui": f"/spaces/{space_key}/pages/{pid}"}}

        def fake_update_page(page_id, space_key, title, body, current_version, parent_id=None):
            updated_pages.append({"id": page_id, "body": body})
            return {"id": page_id}

        def fake_upload_attachment(page_id, filename, file_bytes, mime_type=None):
            uploaded.append({"page_id": page_id, "filename": filename, "bytes": file_bytes})
            return {"id": f"att_{len(uploaded)}"}

        mock_client.create_page.side_effect = fake_create_page
        mock_client.update_page.side_effect = fake_update_page
        mock_client.upload_attachment.side_effect = fake_upload_attachment
        mock_client.create_database.return_value = {"id": "db_img_conf", "_links": {"webui": "/spaces/D/database/db_img_conf"}}
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = []
        mock_client.get_page_by_id.return_value = {"id": "p_2", "version": {"number": 1}}

        importer = ConfluenceImporter("https://example.atlassian.net/wiki", "user", "tok", space_key="D")
        importer.client = mock_client
        importer.import_tree(root, parent_id="parent-0", overwrite=False)

        self.assertEqual(len(uploaded), 2)
        self.assertEqual([u["page_id"] for u in uploaded], ["p_2", "p_2"])
        self.assertEqual([u["filename"] for u in uploaded], ["Untitled.png", "r2_Untitled.png"])
        self.assertEqual([u["bytes"] for u in uploaded], [b"PNGDATA_1", b"PNGDATA_2"])

        agg_body = [u["body"] for u in updated_pages if u["id"] == "p_2"][-1]
        self.assertIn('ri:filename="Untitled.png"', agg_body)
        self.assertIn('ri:filename="r2_Untitled.png"', agg_body)

    def test_generate_database_csv_deduplicates_normalized_field_names(self):
        """項番 などキーカラムと同名（空白差含む）のプロパティがあっても <fields> に重複出力されないこと。"""
        from src.converter import NotionToConfluenceConverter

        row = PageNode(
            id="r-dup-1",
            title="1",
            node_type="database_row",
            properties={
                "項番": {"type": "title", "value": "1"},
                "項番 ": {"type": "rich_text", "value": "1"},
                "メモ": {"type": "rich_text", "value": "ok"},
            },
        )
        db = PageNode(
            id="db-dup",
            title="重複カラムテストDB",
            node_type="database",
            properties={
                "title_column": "項番",
                "schema": {
                    "項番": {"type": "title"},
                    "項番 ": {"type": "rich_text"},
                    "メモ": {"type": "rich_text"},
                },
            },
            children=[row],
        )
        csv_body = NotionToConfluenceConverter().generate_database_csv(
            db,
            page_link_map={"r-dup-1": "https://example/p#1"},
            use_text_title=True,
            add_hidden_link_col=True,
        )
        fields_section = csv_body.split("</fields>")[0]
        self.assertEqual(fields_section.count("項番,"), 1)
        self.assertIn("メモ,text,", fields_section)

    def test_generate_database_csv_sanitizes_invisible_chars_and_falls_back_non_http_url_to_text(self):
        """カラム名の不可視制御文字を除去し、共有フォルダパス等を含むURLカラムをtext型へフォールバックすること。"""
        from src.converter import NotionToConfluenceConverter

        row1 = PageNode(
            id="r-san-1",
            title="1",
            node_type="database_row",
            properties={
                "\ufeff項番\u200b": {"type": "title", "value": "1"},
                "格納先\u200b": {"type": "url", "value": r"\\fileserver\share\project"},
                "参考URL": {"type": "url", "value": "https://example.com/docs"},
            },
        )
        db = PageNode(
            id="db-san",
            title="サニタイズDB",
            node_type="database",
            properties={
                "title_column": "\ufeff項番\u200b",
                "schema": {
                    "\ufeff項番\u200b": {"type": "title"},
                    "格納先\u200b": {"type": "url"},
                    "参考URL": {"type": "url"},
                },
            },
            children=[row1],
        )
        csv_body = NotionToConfluenceConverter().generate_database_csv(
            db,
            page_link_map={"r-san-1": "https://example/p#1"},
            use_text_title=True,
            add_hidden_link_col=True,
        )
        fields_section = csv_body.split("</fields>")[0]
        self.assertNotIn("\ufeff", csv_body)
        self.assertNotIn("\u200b", csv_body)
        self.assertIn("項番,text,", fields_section)
        self.assertIn("格納先,text,", fields_section)
        self.assertIn("参考URL,hyperlink,", fields_section)
        self.assertIn(r"\\fileserver\share\project", csv_body)


if __name__ == "__main__":
    unittest.main()
