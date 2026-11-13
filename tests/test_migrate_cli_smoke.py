"""migrate.py 主要 CLI フローの結合スモークテスト (TCK_00004)。

--zip / --zip-dir / --dry-run / --resume / --no-verify / --no-overwrite /
--private / --generate-debug-report / --export-worksheet / --export-user-mapping /
異常系終了・中断ハンドリング・大規模サマリー出力関数を検証する。
"""

import contextlib
import io
import json
import logging
import os
import signal
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import migrate
from src.cancellation import MigrationCancelledException
from src.checkpoint import MigrationCheckpoint
from src.extractor import PageNode


def _create_minimal_zip(zip_path: str, page_title: str = "Sample Root", uid: str = "11111111111111111111111111111111"):
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{page_title}</title></head>
<body>
  <header><h1 class="page-title">{page_title}</h1></header>
  <div class="page-body"><p>Hello from {page_title}</p></div>
</body>
</html>"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{page_title} {uid}.html", html.encode("utf-8"))


class MigrateCliTestBase(unittest.TestCase):
    """一時ディレクトリ・環境変数・ロガー・シグナルハンドラをテスト毎に完全隔離する基底クラス。"""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_obj.name
        os.chdir(self.tmpdir)

        root_logger = logging.getLogger()
        self._orig_handlers = list(root_logger.handlers)
        self._orig_level = root_logger.level
        self._orig_sigint = signal.getsignal(signal.SIGINT)
        self._orig_sigterm = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        signal.signal(signal.SIGINT, self._orig_sigint)
        signal.signal(signal.SIGTERM, self._orig_sigterm)

        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            if h not in self._orig_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        root_logger.handlers = self._orig_handlers
        root_logger.setLevel(self._orig_level)

        os.chdir(self._orig_cwd)
        self._tmpdir_obj.cleanup()

    def _run_main(self, argv, env=None):
        env_patch = env if env is not None else {}
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, env_patch, clear=True), \
             patch.object(sys, "argv", ["migrate.py"] + argv), \
             contextlib.redirect_stdout(stdout_buf), \
             contextlib.redirect_stderr(stderr_buf):
            migrate.main()
        return stdout_buf.getvalue(), stderr_buf.getvalue()


class TestMigrateCliSmoke(MigrateCliTestBase):
    CONFLUENCE_ENV = {
        "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "bot@example.com",
        "CONFLUENCE_API_TOKEN": "dummy-secret-token",
        "CONFLUENCE_SPACE_KEY": "DOCS",
    }

    def test_zip_dry_run_with_debug_report_skips_importer(self):
        zpath = os.path.join(self.tmpdir, "export.zip")
        _create_minimal_zip(zpath, "DryRun Page")
        report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(report_dir, exist_ok=True)
        cp_path = os.path.join(self.tmpdir, "cp.json")

        with patch("migrate.ConfluenceImporter") as mock_importer_cls:
            out, _ = self._run_main([
                "--zip", zpath,
                "--dry-run",
                "--checkpoint", cp_path,
                "--generate-debug-report",
                "--debug-report-dir", report_dir,
            ])

        mock_importer_cls.assert_not_called()
        self.assertIn("DRY RUN モード", out)
        self.assertIn("[HTML] DryRun Page", out)
        self.assertTrue(os.path.exists(cp_path))
        self.assertTrue(os.path.exists(os.path.join(report_dir, "debug_report.json")))
        self.assertTrue(os.path.exists(os.path.join(report_dir, "debug_report.md")))

    def test_zip_dir_full_flow_with_mocked_importer_and_verifier(self):
        zip_dir = os.path.join(self.tmpdir, "zips")
        os.makedirs(zip_dir)
        _create_minimal_zip(os.path.join(zip_dir, "a.zip"), "Alpha", "11111111111111111111111111111111")
        _create_minimal_zip(os.path.join(zip_dir, "b.zip"), "Beta", "22222222222222222222222222222222")
        cp_path = os.path.join(self.tmpdir, "cp.json")

        mock_importer = MagicMock()
        mock_importer.client = MagicMock()
        mock_importer.import_tree.return_value = {
            "multi_block_root_page": {
                "title": "[HTML] 【統合移行】マルチブロック移行ルート",
                "node_type": "page",
                "confluence_id": "1001",
                "url": "https://example.atlassian.net/wiki/spaces/DOCS/pages/1001",
            }
        }
        mock_verifier = MagicMock()
        mock_verifier.verify_tree.return_value = [
            {
                "status": "PASS",
                "title": "[HTML] 【統合移行】マルチブロック移行ルート",
                "page_id": "1001",
                "checks_passed": ["ok"],
                "warnings": [],
                "issues": [],
            }
        ]

        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier", return_value=mock_verifier) as mock_ver_cls:
            out, _ = self._run_main(
                [
                    "--zip-dir", zip_dir,
                    "--no-dashboard",
                    "--confluence-parent-id", "999",
                    "--checkpoint", cp_path,
                ],
                env=self.CONFLUENCE_ENV,
            )

        self.assertIn("移行が正常に完了しました", out)
        # import_tree 呼び出し引数と importer オプションの検証
        imp_kwargs = mock_imp_cls.call_args.kwargs
        self.assertEqual(imp_kwargs["space_key"], "DOCS")
        self.assertFalse(imp_kwargs["private_mode"])
        self.assertFalse(imp_kwargs["prune_blocks_on_pass2"])

        call_kwargs = mock_importer.import_tree.call_args.kwargs
        self.assertEqual(call_kwargs["parent_id"], "999")
        self.assertTrue(call_kwargs["overwrite"])
        self.assertEqual(call_kwargs["root_node"].id, "multi_block_root_page")

        mock_ver_cls.assert_called_once_with(mock_importer.client)
        mock_verifier.verify_tree.assert_called_once()

        # migration_result.json と checkpoint の完了状態を確認
        result_path = os.path.join(self.tmpdir, "migration_result.json")
        self.assertTrue(os.path.exists(result_path))
        with open(result_path, "r", encoding="utf-8") as f:
            res_data = json.load(f)
        self.assertEqual(res_data["space_key"], "DOCS")
        self.assertEqual(res_data["pages_count"], 1)

        with open(cp_path, "r", encoding="utf-8") as f:
            cp_data = json.load(f)
        self.assertEqual(cp_data["status"], "COMPLETED")

    def test_no_verify_no_overwrite_and_private_flags(self):
        zpath = os.path.join(self.tmpdir, "single.zip")
        _create_minimal_zip(zpath, "PrivatePage")
        cp_path = os.path.join(self.tmpdir, "cp.json")

        mock_importer = MagicMock()
        mock_importer.import_tree.return_value = {
            "11111111111111111111111111111111": {
                "title": "Custom: PrivatePage",
                "node_type": "page",
                "confluence_id": "2001",
                "url": "https://example.atlassian.net/wiki/pages/2001",
            }
        }

        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier") as mock_ver_cls:
            self._run_main(
                [
                    "--zip", zpath,
                    "--title-prefix", "Custom: ",
                    "--no-verify",
                    "--no-overwrite",
                    "--private",
                    "--no-dashboard",
                    "--checkpoint", cp_path,
                ],
                env=self.CONFLUENCE_ENV,
            )

        imp_kwargs = mock_imp_cls.call_args.kwargs
        self.assertTrue(imp_kwargs["private_mode"])
        self.assertTrue(imp_kwargs["prune_blocks_on_pass2"])
        self.assertFalse(mock_importer.import_tree.call_args.kwargs["overwrite"])
        mock_ver_cls.assert_not_called()

    def test_resume_restores_source_info_and_missing_checkpoint_exits(self):
        # 1) 存在しないチェックポイントでの --resume は終了コード 1
        with self.assertRaises(SystemExit) as cm:
            self._run_main(["--resume", "--checkpoint", "missing_cp.json"])
        self.assertEqual(cm.exception.code, 1)

        # 2) チェックポイントの source_info (--zip なし) から自動復元して dry-run 完了
        zpath = os.path.join(self.tmpdir, "saved.zip")
        _create_minimal_zip(zpath, "Resumed Page")
        cp_path = os.path.join(self.tmpdir, "saved_cp.json")

        cp = MigrationCheckpoint(checkpoint_path=cp_path)
        cp.status = "interrupted"
        cp.source_info = {
            "zip_paths": [zpath],
            "title_prefix": "[RESTORED] ",
            "confluence_parent_id": "777",
            "page_workers": 3,
        }
        cp.save()

        with patch("migrate.ConfluenceImporter") as mock_imp_cls:
            out, _ = self._run_main(["--resume", "--checkpoint", cp_path, "--dry-run"])

        mock_imp_cls.assert_not_called()
        self.assertIn("[RESTORED] Resumed Page", out)
        self.assertIn("チェックポイントから移行を再開します", out)

    def test_missing_env_or_token_exits_with_error(self):
        # --zip も NOTION_TOKEN もなし
        with self.assertRaises(SystemExit) as cm1:
            self._run_main(["--dry-run"], env={})
        self.assertEqual(cm1.exception.code, 1)

        # --enrich-via-api で NOTION_TOKEN なし
        zpath = os.path.join(self.tmpdir, "e.zip")
        _create_minimal_zip(zpath, "Enrich")
        with self.assertRaises(SystemExit) as cm2:
            self._run_main(["--zip", zpath, "--enrich-via-api", "--dry-run"], env={})
        self.assertEqual(cm2.exception.code, 1)

        # 非 dry-run で CONFLUENCE_* 不足
        with self.assertRaises(SystemExit) as cm3:
            self._run_main(["--zip", zpath], env={"CONFLUENCE_URL": "https://x"})
        self.assertEqual(cm3.exception.code, 1)

    def test_export_worksheet_and_user_mapping_exit_before_import(self):
        zpath = os.path.join(self.tmpdir, "ws.zip")
        _create_minimal_zip(zpath, "ExportOnly")
        ws_out = os.path.join(self.tmpdir, "out_ws.csv")
        um_out = os.path.join(self.tmpdir, "out_users.csv")

        with patch("migrate.WorksheetManager") as mock_ws_cls, \
             patch("migrate.ConfluenceImporter") as mock_imp_cls:
            self._run_main(
                ["--zip", zpath, "--export-worksheet", ws_out],
                env=self.CONFLUENCE_ENV,
            )
        mock_ws_cls.return_value.export_worksheet.assert_called_once()
        mock_imp_cls.assert_not_called()

        with patch("migrate.UserMappingManager.export_template", return_value=2) as mock_exp, \
             patch("migrate.ConfluenceImporter") as mock_imp_cls:
            self._run_main(
                ["--zip", zpath, "--export-user-mapping", um_out],
                env=self.CONFLUENCE_ENV,
            )
        mock_exp.assert_called_once()
        mock_imp_cls.assert_not_called()

    def test_cancellation_exception_error_halt_vs_graceful_interrupt(self):
        zpath = os.path.join(self.tmpdir, "cancel.zip")
        _create_minimal_zip(zpath, "CancelPage")
        cp_path = os.path.join(self.tmpdir, "cp_cancel.json")

        # 1) Fail-Fast (is_error_halt=True) -> exit(1) + debug report
        mock_imp_err = MagicMock()
        mock_imp_err.import_tree.side_effect = MigrationCancelledException("fatal")
        fake_cm_err = MagicMock()
        fake_cm_err.is_error_halt = True
        fake_cm_err.halt_reason = "409 conflict"

        with patch("migrate.ConfluenceImporter", return_value=mock_imp_err), \
             patch("migrate.CancellationManager", return_value=fake_cm_err):
            with self.assertRaises(SystemExit) as cm_err:
                self._run_main(
                    [
                        "--zip", zpath,
                        "--no-dashboard",
                        "--checkpoint", cp_path,
                        "--generate-debug-report",
                    ],
                    env=self.CONFLUENCE_ENV,
                )
        self.assertEqual(cm_err.exception.code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "debug_report.json")))

        # 2) Ctrl+C による通常中断 (is_error_halt=False) -> exit(0) + interrupted保存
        fake_cm_user = MagicMock()
        fake_cm_user.is_error_halt = False

        with patch("migrate.ConfluenceImporter", return_value=mock_imp_err), \
             patch("migrate.CancellationManager", return_value=fake_cm_user):
            with self.assertRaises(SystemExit) as cm_user:
                self._run_main(
                    ["--zip", zpath, "--no-dashboard", "--checkpoint", cp_path],
                    env=self.CONFLUENCE_ENV,
                )
        self.assertEqual(cm_user.exception.code, 0)
        with open(cp_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "INTERRUPTED")

    def test_api_mode_enrich_worksheet_and_repair_inpage_links(self):
        """Notion APIモード(--notion-page-id)と--enrich-via-api/--import-worksheet/--repair-inpage-links連携"""
        root_node = PageNode(id="root-api", title="API Root", node_type="page")
        mock_extractor = MagicMock()
        mock_extractor.extract_tree.return_value = root_node

        mock_enricher = MagicMock()
        mock_enricher.enrich_tree.return_value = {"databases_enriched": 1, "properties_mapped": 3}

        mock_ws_mgr = MagicMock()
        mock_ws_mgr.import_worksheet.return_value = []
        mock_ws_mgr.apply_worksheet_to_tree.return_value = {
            "databases_updated": 1,
            "columns_updated": 2,
            "views_updated": 1,
        }

        mock_importer = MagicMock()
        mock_importer.client = MagicMock()
        mock_importer.converter = MagicMock()
        mock_importer.import_tree.return_value = {
            "root-api": {
                "title": "API Root",
                "node_type": "page",
                "confluence_id": "3001",
                "url": "https://example.atlassian.net/wiki/pages/3001",
            }
        }

        env = dict(self.CONFLUENCE_ENV, NOTION_TOKEN="ntn_dummy")
        cp_path = os.path.join(self.tmpdir, "cp_api.json")

        with patch("migrate.NotionExtractor", return_value=mock_extractor) as mock_ext_cls, \
             patch("migrate.NotionApiEnricher", return_value=mock_enricher), \
             patch("migrate.WorksheetManager", return_value=mock_ws_mgr), \
             patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("src.inpage_link_repairer.InPageLinkRepairer") as mock_repair_cls, \
             patch("migrate.MigrationVerifier") as mock_ver_cls:
            mock_ver_cls.return_value.verify_tree.return_value = []
            self._run_main(
                [
                    "--notion-page-id", "notion-root-123",
                    "--enrich-via-api",
                    "--import-worksheet", "sheet.csv",
                    "--repair-inpage-links",
                    "--notion-token", "ntn_repair",
                    "--no-dashboard",
                    "--checkpoint", cp_path,
                ],
                env=env,
            )

        mock_ext_cls.assert_called_once_with(token="ntn_dummy")
        mock_extractor.extract_tree.assert_called_once_with(root_page_id="notion-root-123")
        mock_enricher.enrich_tree.assert_called_once_with(root_node)
        mock_ws_mgr.import_worksheet.assert_called_once_with("sheet.csv")
        # repair_inpage_links 指定時は prune_blocks_on_pass2=False
        self.assertFalse(mock_imp_cls.call_args.kwargs["prune_blocks_on_pass2"])
        mock_repair_cls.assert_called_once_with(
            confluence_client=mock_importer.client,
            notion_token="ntn_repair",
        )
        mock_repair_cls.return_value.repair_pages.assert_called_once_with(
            root_node=root_node,
            converter=mock_importer.converter,
        )

    def test_large_tree_and_verification_summary_helpers(self):
        children = []
        for i in range(12):
            db = PageNode(
                id=f"db-{i}",
                title=f"DB {i}",
                node_type="database",
                children=[PageNode(id=f"row-{i}", title=f"Row {i}", node_type="database_row")],
            )
            p = PageNode(id=f"p-{i}", title=f"Page {i}", node_type="page", children=[db])
            children.append(p)
        root = PageNode(id="root", title="Large Root", node_type="page", children=children)

        stats = migrate.collect_tree_stats(root)
        self.assertEqual(stats["total"], 37)
        self.assertEqual(stats["pages"], 13)
        self.assertEqual(stats["databases"], 12)
        self.assertEqual(stats["database_rows"], 12)
        self.assertEqual(stats["max_depth"], 4)

        verify_results = [
            {
                "status": "WARN" if i == 0 else "PASS",
                "title": f"Item {i}",
                "page_id": str(i),
                "checks_passed": ["c1"],
                "warnings": ["w1"] if i == 0 else [],
                "issues": [],
            }
            for i in range(30)
        ]
        mapping = {
            f"n{i}": {
                "title": f"Item {i}",
                "node_type": "database" if i == 0 else ("database_row" if i == 1 else "page"),
                "url": f"https://example.invalid/{i}",
            }
            for i in range(30)
        }

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            migrate.print_tree_summary(root, verbose=False, show_all=False)
            migrate.print_verification_results(verify_results, verbose=False, show_all=False)
            migrate.print_result_summary(mapping, "migration_result.json", show_all=False, verbose=False)
        output = buf.getvalue()
        self.assertIn("合計: 37 ノード", output)
        self.assertIn("他 4 件の主要ページ・セクション", output)
        self.assertIn("確認・対応が必要なページ (1 件)", output)
        self.assertIn("移行成果サマリー (合計: 30 件)", output)


if __name__ == "__main__":
    unittest.main()
