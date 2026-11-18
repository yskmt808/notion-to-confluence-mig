"""
大規模移行時の標準出力サマライズ機能のテストスイート。
- ページツリーの集計（collect_tree_stats）
- ツリーサマリー表示の自動切替（print_tree_summary）
- 検証結果サマリー表示の自動切替（print_verification_results）
- 移行完了サマリー表示の自動切替（print_result_summary）
"""

import os
import sys
import io
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import PageNode
from migrate import (
    collect_tree_stats,
    print_tree_summary,
    print_verification_results,
    print_result_summary,
)


class TestOutputSummarizer(unittest.TestCase):
    def test_collect_tree_stats(self):
        root = PageNode(id="root", title="ルート", node_type="page")
        sub1 = PageNode(id="sub1", title="セクション1", node_type="page")
        db1 = PageNode(id="db1", title="タスクDB", node_type="database")
        for i in range(10):
            row = PageNode(id=f"row_{i}", title=f"タスク {i}", node_type="database_row")
            db1.children.append(row)
        sub1.children.append(db1)
        root.children.append(sub1)

        stats = collect_tree_stats(root)
        # 本文なしDB行は作成されないため total=3 (pages=2 + databases=1)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["databases"], 1)
        self.assertEqual(stats["database_rows"], 10)
        self.assertEqual(stats["max_depth"], 4)

        # 本文あり行を1件追加するとデフォルト（1ページ集約方式）では DB + 集約ページ1枚 (2 + 1 + 1 = 4)
        # individual_db_child_threshold=1 指定時は11行全て作成対象 (2 + 1 + 11 = 14)
        db1.children.append(
            PageNode(
                id="row_body",
                title="本文ありタスク",
                node_type="database_row",
                blocks=[{"id": "b1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "詳細"}]}}],
            )
        )
        self.assertEqual(collect_tree_stats(root)["total"], 4)
        self.assertEqual(
            collect_tree_stats(root, individual_db_child_threshold=1)["total"],
            14,
        )

    def test_tree_summary_switches_on_threshold(self):
        root = PageNode(id="root", title="大規模ルート", node_type="page")
        for i in range(50):
            p = PageNode(id=f"p_{i}", title=f"ページ {i}", node_type="page")
            root.children.append(p)

        # 1. デフォルト (サマライズ表示)
        f_summary = io.StringIO()
        with redirect_stdout(f_summary):
            print_tree_summary(root, verbose=False, show_all=False)
        out_summary = f_summary.getvalue()

        self.assertIn("抽出されたNotionツリー概要 (合計: 51 ノード)", out_summary)
        self.assertIn("通常ページ", out_summary)
        self.assertIn("--show-all-pages", out_summary)
        # 全50件の個別タイトルが直接ずらりと出ないこと（プレビュー上限で省略されること）
        self.assertNotIn("ページ 49", out_summary)

        # 2. --show-all-pages 指定時 (全件展開表示)
        f_all = io.StringIO()
        with redirect_stdout(f_all):
            print_tree_summary(root, verbose=False, show_all=True)
        out_all = f_all.getvalue()

        self.assertIn("抽出されたNotionツリー構造:", out_all)
        self.assertIn("ページ 49", out_all)

    def test_verification_summary_default_suppresses_pass_and_shows_abnormal(self):
        # 小規模（3件）でもデフォルトで PASS 個別行は非表示となり、WARN/FAIL のみ表示されること
        results = [
            {
                "title": "Pass Page 1",
                "page_id": "p1",
                "status": "PASS",
                "checks_passed": ["title_match"],
                "warnings": [],
                "issues": [],
            },
            {
                "title": "Warning Page",
                "page_id": "p2",
                "status": "WARN",
                "checks_passed": [],
                "warnings": ["Missing image attachment"],
                "issues": [],
            },
            {
                "title": "Failed Page",
                "page_id": "p3",
                "status": "FAIL",
                "checks_passed": [],
                "warnings": [],
                "issues": ["Content body mismatch"],
            },
        ]

        f = io.StringIO()
        with redirect_stdout(f):
            print_verification_results(results, verbose=False, show_all=False)
        out = f.getvalue()

        self.assertIn("検証サマリー: 合計 3 ページ | 正常: 1 | 警告: 1 | 失敗: 1", out)
        self.assertIn("確認・対応が必要なページ (2 件):", out)
        self.assertIn("Warning Page", out)
        self.assertIn("Failed Page", out)
        self.assertNotIn("Pass Page 1", out)
        self.assertIn("💡 全ページの個別検証項目を表示するには `--verbose` または `--show-all-pages` を指定してください。", out)

    def test_verification_summary_all_pass_message(self):
        # 全件PASSの場合、合格メッセージが表示され、個別行は表示されないこと
        results = [
            {
                "title": f"Pass Page {i}",
                "page_id": f"p_{i}",
                "status": "PASS",
                "checks_passed": ["title_match"],
                "warnings": [],
                "issues": [],
            }
            for i in range(5)
        ]

        f = io.StringIO()
        with redirect_stdout(f):
            print_verification_results(results, verbose=False, show_all=False)
        out = f.getvalue()

        self.assertIn("検証サマリー: 合計 5 ページ | 正常: 5 | 警告: 0 | 失敗: 0", out)
        self.assertIn("✅ 全ページが整合性検証に完全合格しました（警告・エラーなし）。", out)
        self.assertNotIn("Pass Page 0", out)
        self.assertIn("💡 全ページの個別検証項目を表示するには `--verbose` または `--show-all-pages` を指定してください。", out)

    def test_verification_summary_verbose_and_show_all(self):
        # verbose=True または show_all=True で PASS を含む全件が表示されること
        results = [
            {
                "title": "Pass Page 1",
                "page_id": "p1",
                "status": "PASS",
                "checks_passed": ["title_match"],
                "warnings": [],
                "issues": [],
            },
            {
                "title": "Warning Page",
                "page_id": "p2",
                "status": "WARN",
                "checks_passed": [],
                "warnings": ["Missing image attachment"],
                "issues": [],
            },
        ]

        # 1. show_all=True
        f_show_all = io.StringIO()
        with redirect_stdout(f_show_all):
            print_verification_results(results, verbose=False, show_all=True)
        out_show_all = f_show_all.getvalue()

        self.assertIn("✅ [PASS] Pass Page 1 (ID: p1)", out_show_all)
        self.assertIn("⚠️ [WARN] Warning Page (ID: p2)", out_show_all)
        self.assertNotIn("✓ title_match", out_show_all)  # verboseでないためcheck詳細は出ない

        # 2. verbose=True
        f_verbose = io.StringIO()
        with redirect_stdout(f_verbose):
            print_verification_results(results, verbose=True, show_all=False)
        out_verbose = f_verbose.getvalue()

        self.assertIn("✅ [PASS] Pass Page 1 (ID: p1)", out_verbose)
        self.assertIn("✓ title_match", out_verbose)
        self.assertIn("⚠️ [WARN] Warning Page (ID: p2)", out_verbose)

    def test_result_summary_switches_on_threshold(self):
        # 40件の移行結果マッピング
        mapping = {}
        for i in range(40):
            mapping[f"node_{i}"] = {
                "confluence_id": f"conf_{i}",
                "title": f"Doc {i}",
                "url": f"https://wiki/pages/{i}",
                "node_type": "page",
            }

        # 1. サマライズ表示
        f = io.StringIO()
        with redirect_stdout(f):
            print_result_summary(mapping, "migration_result.json", show_all=False, verbose=False)
        out = f.getvalue()

        self.assertIn("移行成果サマリー (合計: 40 件):", out)
        self.assertIn("通常ページ      : 40 件", out)
        self.assertIn("migration_result.json", out)
        self.assertNotIn("Doc 39", out)  # 末尾のページは省略され主要URLのみ出る

        # 2. 全件表示
        f_all = io.StringIO()
        with redirect_stdout(f_all):
            print_result_summary(mapping, "migration_result.json", show_all=True, verbose=False)
        out_all = f_all.getvalue()
        self.assertIn("Doc 39", out_all)


if __name__ == "__main__":
    unittest.main()

