"""plan_tree.py / src/tree_planner.py の単体・結合テスト。"""

import csv
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import plan_tree
from src.tree_planner import (
    ApiTreeScanner,
    build_summary_markdown,
    export_pages_csv,
    flatten_plan_trees,
    scan_zips_to_plan_trees,
)


class TestTreePlanner(unittest.TestCase):
    @patch("src.tree_planner.time.sleep")
    @patch("src.tree_planner.requests.request")
    def test_api_scanner_pagination_429_retry_block_parent_and_cache_resume(self, mock_req, mock_sleep):
        root_id = "11111111-1111-1111-1111-111111111111"
        child_page_id = "22222222-2222-2222-2222-222222222222"
        db_id = "33333333-3333-3333-3333-333333333333"
        row_id = "44444444-4444-4444-4444-444444444444"
        wrapper_block_id = "55555555-5555-5555-5555-555555555555"

        r_429 = MagicMock(status_code=429, ok=False, headers={"Retry-After": "1"}, text="rate limited")
        r_page1 = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "results": [
                    {
                        "object": "page",
                        "id": root_id,
                        "parent": {"type": "workspace", "workspace": True},
                        "url": "https://www.notion.so/root",
                        "last_edited_time": "2026-11-20T10:00:00.000Z",
                        "archived": False,
                        "properties": {"title": {"type": "title", "title": [{"plain_text": "全社ポータル"}]}},
                    },
                    {
                        "object": "page",
                        "id": child_page_id,
                        "parent": {"type": "block_id", "block_id": wrapper_block_id},
                        "url": "https://www.notion.so/child",
                        "last_edited_time": "2026-11-21T10:00:00.000Z",
                        "archived": False,
                        "properties": {"title": {"type": "title", "title": [{"plain_text": "開発ガイド"}]}},
                    },
                ],
                "has_more": True,
                "next_cursor": "cur-2",
            },
        )
        r_page2 = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "results": [
                    {
                        "object": "database",
                        "id": db_id,
                        "parent": {"type": "page_id", "page_id": child_page_id},
                        "url": "https://www.notion.so/db",
                        "last_edited_time": "2026-11-22T10:00:00.000Z",
                        "archived": False,
                        "title": [{"plain_text": "タスクDB"}],
                    },
                    {
                        "object": "page",
                        "id": row_id,
                        "parent": {"type": "database_id", "database_id": db_id},
                        "url": "https://www.notion.so/row1",
                        "last_edited_time": "2026-11-23T10:00:00.000Z",
                        "archived": False,
                        "properties": {"名前": {"type": "title", "title": [{"plain_text": "タスク1"}]}},
                    },
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )
        r_block_parent = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {"id": wrapper_block_id, "parent": {"type": "page_id", "page_id": root_id}},
        )
        mock_req.side_effect = [r_429, r_page1, r_page2, r_block_parent]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "cache.json")
            scanner = ApiTreeScanner(token="ntn_test", rate_limit=100.0, cache_file=cache_path, resume=False)
            roots = scanner.scan(root_page_id=root_id)

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].title, "全社ポータル")
            self.assertEqual(roots[0].children[0].title, "開発ガイド")
            self.assertEqual(roots[0].children[0].children[0].db_row_count, 1)

            rows = flatten_plan_trees(roots, include_db_rows=True)
            self.assertEqual([r["title"] for r in rows], ["全社ポータル", "開発ガイド", "タスクDB", "タスク1"])
            self.assertEqual([r["depth"] for r in rows], [0, 1, 2, 3])
            self.assertEqual(rows[0]["subtree_node_count"], 3)

            rows_no_db = flatten_plan_trees(roots, include_db_rows=False)
            self.assertEqual([r["title"] for r in rows_no_db], ["全社ポータル", "開発ガイド", "タスクDB"])
            self.assertEqual(rows_no_db[2]["db_row_count"], 1)

            csv_path = os.path.join(tmpdir, "pages.csv")
            export_pages_csv(rows_no_db, csv_path)
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual(len(csv_rows), 3)
            self.assertEqual(csv_rows[2]["db_row_count"], "1")

            md = build_summary_markdown(roots, rows_no_db, source_label="Notion API", api_calls=scanner.api_calls)
            self.assertIn("**ルートページ**: 全社ポータル (`page` / ID: `11111111-1111-1111-1111-111111111111`)", md)
            self.assertIn("## 0. ルートページ情報", md)
            self.assertIn("| 出力ノード総数 (CSV行数) | 3 |", md)
            self.assertIn("| DB行レコード (`database_row` 実数) | 1 |", md)
            self.assertIn("| タスクDB | 1 |", md)

            # キャッシュからの再開時は /v1/search も block_id親解決 も呼ばないこと (block_parentsもキャッシュ済み)
            mock_req.reset_mock()
            mock_req.side_effect = []
            scanner_resumed = ApiTreeScanner(token="ntn_test", rate_limit=100.0, cache_file=cache_path, resume=True)
            roots_resumed = scanner_resumed.scan(root_page_id=root_id)
            self.assertEqual(len(roots_resumed), 1)
            self.assertEqual(scanner_resumed.api_calls, 0)

    def test_zip_mode_cli_outputs_pages_csv_and_summary_md(self):
        root_id = "aaaa1111111111111111111111111111"
        sub_id = "bbbb2222222222222222222222222222"
        db_id = "cccc3333333333333333333333333333"

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "sample.zip")
            out_dir = os.path.join(tmpdir, "plan_out")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(
                    f"Top {root_id}.html",
                    f'<html><head><title>Top</title></head><body><article id="{root_id}"><header><h1 class="page-title">Top</h1></header><div class="page-body"></div></article></body></html>',
                )
                zf.writestr(
                    f"Top/Child {sub_id}.html",
                    f'<html><head><title>Child</title></head><body><article id="{sub_id}"><header><h1 class="page-title">Child</h1></header><div class="page-body"></div></article></body></html>',
                )
                zf.writestr(f"Top/Items {db_id}.csv", "名前,メモ\nItem1,a\nItem2,b\n")

            exit_code = plan_tree.main(["--zip", zip_path, "--output-dir", out_dir, "--exclude-db-rows"])
            self.assertEqual(exit_code, 0)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "pages.csv")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "summary.md")))

            with open(os.path.join(out_dir, "pages.csv"), "r", encoding="utf-8-sig") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual([r["title"] for r in csv_rows], ["Top", "Child", "Items"])
            self.assertEqual(csv_rows[2]["db_row_count"], "2")

            sub_dir = os.path.join(tmpdir, "plan_sub")
            exit_code_sub = plan_tree.main([
                "--zip", zip_path, "--root-page-id", sub_id, "--output-dir", sub_dir,
            ])
            self.assertEqual(exit_code_sub, 0)
            with open(os.path.join(sub_dir, "pages.csv"), "r", encoding="utf-8-sig") as f:
                sub_rows = list(csv.DictReader(f))
            self.assertEqual([r["title"] for r in sub_rows], ["Child"])
            self.assertEqual(sub_rows[0]["parent_id"], "")

    @patch("src.tree_planner.time.sleep")
    @patch("src.tree_planner.requests.request")
    def test_subtree_only_mode_traverses_blocks_and_skips_db_rows(self, mock_req, mock_sleep):
        root_id = "aaaa0000-0000-0000-0000-000000000001"
        child_page_id = "bbbb0000-0000-0000-0000-000000000002"
        toggle_id = "cccc0000-0000-0000-0000-000000000003"
        nested_db_id = "dddd0000-0000-0000-0000-000000000004"

        r_root = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "object": "page",
                "id": root_id,
                "url": "https://www.notion.so/root",
                "last_edited_time": "2026-11-24T00:00:00.000Z",
                "archived": False,
                "properties": {"title": {"type": "title", "title": [{"plain_text": "対象ルート"}]}},
            },
        )
        r_root_children = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "results": [
                    {
                        "id": child_page_id,
                        "type": "child_page",
                        "child_page": {"title": "配下ページ"},
                        "has_children": True,
                        "last_edited_time": "2026-11-24T01:00:00.000Z",
                        "archived": False,
                    },
                    {
                        "id": toggle_id,
                        "type": "toggle",
                        "toggle": {"rich_text": []},
                        "has_children": True,
                    },
                    {
                        "id": "eeee0000-0000-0000-0000-000000000005",
                        "type": "table",
                        "table": {},
                        "has_children": True,
                    },
                ],
                "has_more": False,
            },
        )
        r_child_children = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {"results": [], "has_more": False},
        )
        r_toggle_children = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "results": [
                    {
                        "id": nested_db_id,
                        "type": "child_database",
                        "child_database": {"title": "配下DB"},
                        "has_children": False,
                        "last_edited_time": "2026-11-24T02:00:00.000Z",
                        "archived": False,
                    }
                ],
                "has_more": False,
            },
        )
        mock_req.side_effect = [r_root, r_root_children, r_child_children, r_toggle_children]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "subtree_cache.json")
            out_dir = os.path.join(tmpdir, "out")
            exit_code = plan_tree.main([
                "--api",
                "--notion-token",
                "ntn_test",
                "--root-page-id",
                root_id,
                "--subtree-only",
                "--rate-limit",
                "100",
                "--cache-file",
                cache_path,
                "--output-dir",
                out_dir,
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(mock_req.call_count, 4)

            with open(os.path.join(out_dir, "pages.csv"), "r", encoding="utf-8-sig") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual([r["title"] for r in csv_rows], ["対象ルート", "配下ページ", "配下DB"])
            self.assertEqual([r["node_type"] for r in csv_rows], ["page", "page", "database"])

            # --resume 時はキャッシュからブロックを読み込み、ルート確認の1回のみで済むこと
            mock_req.reset_mock()
            mock_req.side_effect = [r_root]
            scanner_resume = ApiTreeScanner(token="ntn_test", rate_limit=100.0, cache_file=cache_path, resume=True)
            roots_resumed = scanner_resume.scan_subtree(root_id)
            self.assertEqual(len(roots_resumed), 1)
            self.assertEqual(scanner_resume.api_calls, 1)

    @patch("src.tree_planner.time.sleep")
    def test_atomic_write_json_retries_on_winerror5_permission_error(self, mock_sleep):
        scanner = ApiTreeScanner(token="ntn_test")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "cache.json")
            orig_replace = os.replace
            attempts = [0]

            def flaky_replace(src, dst):
                attempts[0] += 1
                if attempts[0] <= 2:
                    raise PermissionError(13, "WinError 5 access denied")
                return orig_replace(src, dst)

            with patch("src.tree_planner.os.replace", side_effect=flaky_replace):
                scanner._atomic_write_json(target, {"ok": True})

            self.assertEqual(attempts[0], 3)
            self.assertTrue(os.path.exists(target))

    @patch("src.tree_planner.time.sleep")
    @patch("src.tree_planner.requests.request")
    def test_from_cache_cli_extracts_subtree_offline_with_block_parents(self, mock_req, mock_sleep):
        root_id = "11111111-1111-1111-1111-111111111111"
        sub_root_id = "22222222-2222-2222-2222-222222222222"
        leaf_id = "33333333-3333-3333-3333-333333333333"
        blk_id = "44444444-4444-4444-4444-444444444444"

        r_search = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {
                "results": [
                    {
                        "object": "page",
                        "id": root_id,
                        "parent": {"type": "workspace", "workspace": True},
                        "properties": {"title": {"type": "title", "title": [{"plain_text": "全体ルート"}]}},
                    },
                    {
                        "object": "page",
                        "id": sub_root_id,
                        "parent": {"type": "page_id", "page_id": root_id},
                        "properties": {"title": {"type": "title", "title": [{"plain_text": "部署A"}]}},
                    },
                    {
                        "object": "page",
                        "id": leaf_id,
                        "parent": {"type": "block_id", "block_id": blk_id},
                        "properties": {"title": {"type": "title", "title": [{"plain_text": "手順書"}]}},
                    },
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )
        r_blk = MagicMock(
            status_code=200,
            ok=True,
            json=lambda: {"id": blk_id, "parent": {"type": "page_id", "page_id": sub_root_id}},
        )
        mock_req.side_effect = [r_search, r_blk]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "api_search_cache.json")
            out_sub = os.path.join(tmpdir, "sub_out")
            scanner = ApiTreeScanner(token="ntn_test", rate_limit=100.0, cache_file=cache_path)
            scanner.scan()

            # --from-cache は requests.request を一切呼ばずにサブツリーを抽出できること
            mock_req.reset_mock()
            exit_code = plan_tree.main([
                "--from-cache",
                cache_path,
                "--root-page-id",
                sub_root_id,
                "--output-dir",
                out_sub,
            ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(mock_req.call_count, 0)

            with open(os.path.join(out_sub, "pages.csv"), "r", encoding="utf-8-sig") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual([r["title"] for r in csv_rows], ["部署A", "手順書"])

    def test_zip_stats_only_calculates_scale_without_html_parsing(self):
        """--stats-only 指定時はHTMLフルパースを行わず、ZIP規模サマリー・DB一覧CSV・フォルダ一覧CSVを高速出力すること"""
        root_id = "aaaa1111111111111111111111111111"
        db_id = "bbbb2222222222222222222222222222"
        row_id = "cccc3333333333333333333333333333"

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "scale.zip")
            out_dir = os.path.join(tmpdir, "stats_out")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"DeptA/Top {root_id}.html", "<html><body>top</body></html>")
                zf.writestr(f"DeptA/Tasks {db_id}.csv", "名前,メモ\nT1,a\nT2,b\nT3,c\n".encode("utf-16"))
                zf.writestr(f"DeptA/Tasks/T1 {row_id}.html", "<html><body>detail</body></html>")
                zf.writestr("DeptA/image.png", b"\x89PNG\r\n\x1a\n")

            with patch("src.tree_planner.HtmlZipExtractor.extract_tree", side_effect=AssertionError("should not full-parse")):
                exit_code = plan_tree.main(["--zip", zip_path, "--stats-only", "--output-dir", out_dir])

            self.assertEqual(exit_code, 0)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "zip_stats.md")))
            with open(os.path.join(out_dir, "zip_databases.csv"), "r", encoding="utf-8-sig") as f:
                db_rows = list(csv.DictReader(f))
            self.assertEqual(len(db_rows), 1)
            self.assertEqual(db_rows[0]["db_title"], "Tasks")
            self.assertEqual(db_rows[0]["row_count"], "3")
            self.assertEqual(db_rows[0]["row_html_count"], "1")

            with open(os.path.join(out_dir, "zip_folders.csv"), "r", encoding="utf-8-sig") as f:
                folder_rows = list(csv.DictReader(f))
            self.assertEqual(folder_rows[0]["folder"], "DeptA")
            self.assertEqual(folder_rows[0]["html_count"], "2")
            self.assertEqual(folder_rows[0]["csv_count"], "1")
            self.assertEqual(folder_rows[0]["csv_rows"], "3")

    def test_fast_tree_and_exclude_file_skip_full_parse_and_prune_subtrees(self):
        """--fast-tree がフルパースなしでツリーを構築し、--exclude-file 指定IDのサブツリーを除外すること"""
        root_id = "aaaa1111111111111111111111111111"
        keep_id = "bbbb2222222222222222222222222222"
        db_id = "cccc3333333333333333333333333333"
        row_id = "dddd4444444444444444444444444444"
        excl_id = "eeee5555555555555555555555555555"
        excl_child_id = "ffff6666666666666666666666666666"

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "fast.zip")
            excl_file = os.path.join(tmpdir, "exclude.txt")
            out_dir = os.path.join(tmpdir, "fast_out")
            with open(excl_file, "w", encoding="utf-8") as ef:
                ef.write(f"# 不要なアーカイブ\nhttps://www.notion.so/{excl_id}\n")

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"Root {root_id}.html", "<html><head><title>全体ルート</title></head><body></body></html>")
                zf.writestr(f"Root/部署A {keep_id}.html", "<html><head><title>部署A</title></head><body></body></html>")
                zf.writestr(f"Root/部署A/課題DB {db_id}.csv", "名前,メモ\nK1,a\nK2,b\n")
                zf.writestr(f"Root/部署A/課題DB/K1 {row_id}.html", "<html><head><title>K1</title></head><body></body></html>")
                zf.writestr(f"Root/アーカイブ {excl_id}.html", "<html><head><title>アーカイブ</title></head><body></body></html>")
                zf.writestr(f"Root/アーカイブ/古いページ {excl_child_id}.html", "<html><head><title>古いページ</title></head><body></body></html>")

            with patch("src.tree_planner.HtmlZipExtractor.extract_tree", side_effect=AssertionError("should not full-parse")):
                exit_code = plan_tree.main([
                    "--zip", zip_path,
                    "--fast-tree",
                    "--exclude-db-rows",
                    "--exclude-file", excl_file,
                    "--output-dir", out_dir,
                ])

            self.assertEqual(exit_code, 0)
            with open(os.path.join(out_dir, "pages.csv"), "r", encoding="utf-8-sig") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual([r["title"] for r in csv_rows], ["全体ルート", "部署A", "課題DB"])
            self.assertEqual(csv_rows[2]["db_row_count"], "2")


if __name__ == "__main__":
    unittest.main()
