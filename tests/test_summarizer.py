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
        self.assertEqual(stats["total"], 13)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["databases"], 1)
        self.assertEqual(stats["database_rows"], 10)
        self.assertEqual(stats["max_depth"], 4)

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

    def test_verification_summary_switches_on_threshold(self):
        # 100件の検証結果を作成（98件PASS, 1件WARN, 1件FAIL）
        results = []
        for i in range(98):
            results.append({
                "title": f"Page {i}",
                "page_id": f"id_{i}",
                "status": "PASS",
                "checks_passed": ["title_match"],
                "warnings": [],
                "issues": [],
            })
        results.append({
            "title": "Warning Page",
            "page_id": "id_warn",
            "status": "WARN",
            "checks_passed": [],
            "warnings": ["Missing image attachment"],
            "issues": [],
        })
        results.append({
            "title": "Failed Page",
            "page_id": "id_fail",
            "status": "FAIL",
            "checks_passed": [],
            "warnings": [],
            "issues": ["Content body mismatch"],
        })

        # 1. サマライズ表示 (問題のある2件のみ表示され、98件のPASSは抑制される)
        f = io.StringIO()
        with redirect_stdout(f):
            print_verification_results(results, verbose=False, show_all=False)
        out = f.getvalue()

        self.assertIn("検証サマリー: 合計 100 ページ | 正常: 98 | 警告: 1 | 失敗: 1", out)
        self.assertIn("確認・対応が必要なページ (2 件):", out)
        self.assertIn("Warning Page", out)
        self.assertIn("Failed Page", out)
        self.assertNotIn("Page 50", out)  # 正常ページは出力されない

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

