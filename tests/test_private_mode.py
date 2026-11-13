"""
Unit tests for Confluence private mode restriction functionality.
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
from src.checkpoint import MigrationCheckpoint
from src.extractor import PageNode
from src.importer import ConfluenceClient, ConfluenceImporter


def _create_test_zip(zip_path: str, title: str = "Private Root", uid: str = "11111111111111111111111111111111"):
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
  <header><h1 class="page-title">{title}</h1></header>
  <div class="page-body"><p>Hello from {title}</p></div>
</body>
</html>"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{title} {uid}.html", html.encode("utf-8"))


class TestPrivateMode(unittest.TestCase):

    def test_client_set_page_private(self):
        """ConfluenceClient.set_page_private が正しく PUT /rest/api/content/{id}/restriction を呼ぶこと"""
        client = ConfluenceClient("https://example.atlassian.net/wiki", "user@example.com", "token123")
        with patch("requests.put") as mock_put:
            mock_put.return_value.ok = True
            mock_put.return_value.status_code = 200
            success = client.set_page_private("12345", account_id="acc-999")
            self.assertTrue(success)
            mock_put.assert_called_once()
            args, kwargs = mock_put.call_args
            self.assertIn("/rest/api/content/12345/restriction", args[0])
            payload = kwargs.get("json", [])
            self.assertEqual(len(payload), 2)
            ops = {p["operation"]: p["restrictions"]["user"][0]["accountId"] for p in payload}
            self.assertEqual(ops.get("read"), "acc-999")
            self.assertEqual(ops.get("update"), "acc-999")

    def test_client_set_page_private_failure_logs_warning_and_returns_false(self):
        """API失敗・例外・accountId取得不可時に警告ログを出して False を返すこと"""
        client = ConfluenceClient("https://example.atlassian.net/wiki", "user@example.com", "token123")

        # 1) HTTPエラー (403 Forbidden 等)
        with patch("requests.put") as mock_put, \
             self.assertLogs("src.importer", level="WARNING") as cm:
            mock_put.return_value.ok = False
            mock_put.return_value.status_code = 403
            mock_put.return_value.text = "Forbidden"
            self.assertFalse(client.set_page_private("12345", account_id="acc-999"))
        self.assertTrue(any("403" in m for m in cm.output))

        # 2) 通信例外
        with patch("requests.put", side_effect=RuntimeError("network timeout")), \
             self.assertLogs("src.importer", level="WARNING") as cm:
            self.assertFalse(client.set_page_private("12345", account_id="acc-999"))
        self.assertTrue(any("network timeout" in m for m in cm.output))

        # 3) accountId 未取得
        with patch.object(client, "get_current_user", return_value=None), \
             self.assertLogs("src.importer", level="WARNING") as cm:
            self.assertFalse(client.set_page_private("12345"))
        self.assertTrue(any("accountId" in m for m in cm.output))

    def test_importer_applies_private_mode_to_root(self):
        """private_mode 有効時、ルートノードにのみ set_page_private が呼ばれること"""
        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value=None), \
             patch.object(ConfluenceClient, "create_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "update_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 1}}), \
             patch.object(ConfluenceClient, "set_page_private", return_value=True) as mock_set_private, \
             patch.object(ConfluenceClient, "set_page_emoji"):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                private_mode=True,
            )

            root = PageNode(
                id="root-1",
                title="Root Private Page",
                node_type="page",
                blocks=[],
                children=[
                    PageNode(id="child-1", title="Child Page", node_type="page", blocks=[], children=[]),
                ],
            )

            with patch.object(importer, "_import_children_concurrently"):
                importer.import_tree(root)

            # ルートページに対して set_page_private が呼ばれたことを検証
            mock_set_private.assert_called_once_with("page-root", account_id="acc-current")

    def test_importer_locks_root_immediately_after_dummy_create_before_attachments_and_update(self):
        """新規ルートページ作成時、dummy_body 作成直後・添付アップロードや本文 update より前に set_page_private が呼ばれること"""
        call_order = []

        def fake_create_page(**kwargs):
            call_order.append(("create_page", kwargs.get("body")))
            return {"id": "page-root", "_links": {"webui": "/root"}}

        def fake_set_private(page_id, account_id=None):
            call_order.append(("set_page_private", page_id, account_id))
            return True

        def fake_process_cover(node, page_id):
            call_order.append(("process_cover", page_id))

        def fake_process_attachments(blocks, page_id):
            call_order.append(("process_attachments", page_id))

        def fake_update_page(**kwargs):
            call_order.append(("update_page", kwargs.get("page_id")))
            return {"id": "page-root", "_links": {"webui": "/root"}}

        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value=None), \
             patch.object(ConfluenceClient, "create_page", side_effect=fake_create_page), \
             patch.object(ConfluenceClient, "set_page_private", side_effect=fake_set_private), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 1}}), \
             patch.object(ConfluenceClient, "update_page", side_effect=fake_update_page), \
             patch.object(ConfluenceClient, "set_page_emoji"):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                private_mode=True,
            )

            root = PageNode(
                id="root-1",
                title="Root Private Page",
                node_type="page",
                blocks=[{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "hello"}]}}],
                children=[
                    PageNode(id="child-1", title="Child Page", node_type="page", blocks=[], children=[]),
                ],
            )

            with patch.object(importer, "_process_cover", side_effect=fake_process_cover), \
                 patch.object(importer, "_process_attachments", side_effect=fake_process_attachments), \
                 patch.object(importer, "_import_children_concurrently"):
                importer.import_tree(root)

        names = [c[0] for c in call_order]
        # Pass 1: create_page -> set_page_private -> process_cover -> process_attachments -> update_page
        self.assertGreaterEqual(len(names), 5)
        self.assertEqual(
            names[:5],
            ["create_page", "set_page_private", "process_cover", "process_attachments", "update_page"],
        )
        self.assertEqual(call_order[0][1], "<p>読み込み中...</p>")
        self.assertEqual(call_order[1], ("set_page_private", "page-root", "acc-current"))

    def test_importer_locks_existing_root_before_overwrite_update(self):
        """既存ルートページ上書き時も、添付処理や本文 update より前に set_page_private が呼ばれること"""
        call_order = []

        def fake_set_private(page_id, account_id=None):
            call_order.append(("set_page_private", page_id, account_id))
            return True

        def fake_process_cover(node, page_id):
            call_order.append(("process_cover", page_id))

        def fake_update_page(**kwargs):
            call_order.append(("update_page", kwargs.get("page_id")))
            return {"id": "page-existing", "_links": {"webui": "/existing"}}

        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value={"id": "page-existing"}), \
             patch.object(ConfluenceClient, "set_page_private", side_effect=fake_set_private), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 2}}), \
             patch.object(ConfluenceClient, "update_page", side_effect=fake_update_page), \
             patch.object(ConfluenceClient, "set_page_emoji"):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                private_mode=True,
            )

            root = PageNode(id="root-existing", title="Existing Root", node_type="page", blocks=[], children=[])
            with patch.object(importer, "_process_cover", side_effect=fake_process_cover), \
                 patch.object(importer, "_import_children_concurrently"):
                importer.import_tree(root, overwrite=True)

        names = [c[0] for c in call_order]
        self.assertGreaterEqual(len(names), 3)
        self.assertEqual(names[:3], ["set_page_private", "process_cover", "update_page"])

    def test_importer_locks_database_root_immediately_after_creation(self):
        """ルートノードがデータベースの場合、DB作成直後・フィールド設定やビュー設定より前に set_page_private が呼ばれること"""
        call_order = []

        def fake_create_database(**kwargs):
            call_order.append(("create_database", kwargs.get("title")))
            return {"id": "db-root-1", "_links": {"webui": "/database/db-root-1"}}

        def fake_set_private(page_id, account_id=None):
            call_order.append(("set_page_private", page_id, account_id))
            return True

        def fake_update_field_options(db_id, **kwargs):
            call_order.append(("update_database_field_options", db_id))

        def fake_get_views(db_id):
            call_order.append(("get_database_views", db_id))
            return []

        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value=None), \
             patch.object(ConfluenceClient, "get_database_by_title", return_value=None), \
             patch.object(ConfluenceClient, "get_space_id", return_value="space-1"), \
             patch.object(ConfluenceClient, "create_page", return_value={"id": "row-1", "_links": {"webui": "/row-1"}}), \
             patch.object(ConfluenceClient, "update_page", return_value={"id": "row-1", "_links": {"webui": "/row-1"}}), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 1}}), \
             patch.object(ConfluenceClient, "create_database", side_effect=fake_create_database), \
             patch.object(ConfluenceClient, "set_page_private", side_effect=fake_set_private), \
             patch.object(ConfluenceClient, "update_database_field_options", side_effect=fake_update_field_options), \
             patch.object(ConfluenceClient, "get_database_views", side_effect=fake_get_views), \
             patch.object(ConfluenceClient, "create_database_view", return_value={}), \
             patch.object(ConfluenceClient, "get_database_entries", return_value=[]), \
             patch.object(ConfluenceClient, "add_labels"):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                private_mode=True,
            )
            importer.converter.get_select_field_configurations = MagicMock(return_value=({"Status": ["Todo"]}, {}))
            importer.converter.generate_database_csv = MagicMock(return_value="<metadata></metadata><data></data>")

            row_child = PageNode(id="row-node-1", title="Row 1", node_type="database_row", blocks=[], children=[])
            db_root = PageNode(
                id="db-node-1",
                title="Root Private DB",
                node_type="database",
                blocks=[],
                children=[row_child],
            )

            with patch.object(importer, "resolve_pending_database_row_views"), \
                 patch.object(importer, "_resolve_and_update_page_links"):
                importer.import_tree(db_root)

        self.assertEqual(
            call_order[:4],
            [
                ("create_database", "Root Private DB"),
                ("set_page_private", "db-root-1", "acc-current"),
                ("update_database_field_options", "db-root-1"),
                ("get_database_views", "db-root-1"),
            ],
        )
        # DB子レコードではなくDBルート1回のみ set_page_private が呼ばれること
        private_calls = [c for c in call_order if c[0] == "set_page_private"]
        self.assertEqual(len(private_calls), 1)

    def test_importer_records_warning_and_continues_when_set_page_private_fails(self):
        """set_page_private 失敗時、移行は継続しつつ logger / telemetry / dashboard に警告が記録されること"""
        mock_telemetry = MagicMock()
        mock_dashboard = MagicMock()

        with patch.object(ConfluenceClient, "get_current_user", return_value={"accountId": "acc-current"}), \
             patch.object(ConfluenceClient, "get_page_by_title", return_value=None), \
             patch.object(ConfluenceClient, "create_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "update_page", return_value={"id": "page-root", "_links": {"webui": "/root"}}), \
             patch.object(ConfluenceClient, "get_page_by_id", return_value={"version": {"number": 1}}), \
             patch.object(ConfluenceClient, "set_page_private", return_value=False):

            importer = ConfluenceImporter(
                url="https://example.atlassian.net",
                username="test",
                token="test",
                space_key="TEST",
                telemetry=mock_telemetry,
                dashboard=mock_dashboard,
                private_mode=True,
            )

            root = PageNode(id="root-warn", title="Warn Root", node_type="page", blocks=[], children=[])
            with self.assertLogs("src.importer", level="WARNING") as cm:
                mapping = importer.import_tree(root)

        self.assertIn("root-warn", mapping)
        self.assertEqual(mapping["root-warn"]["confluence_id"], "page-root")
        self.assertTrue(any("Failed to apply private restriction" in m for m in cm.output))
        mock_telemetry.record_warning.assert_called()
        mock_dashboard.record_warning.assert_called()


class TestPrivateModeCliAndResume(unittest.TestCase):
    """migrate.py の --visibility / --private 配線および --resume 復元のテスト。"""

    CONFLUENCE_ENV = {
        "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "bot@example.com",
        "CONFLUENCE_API_TOKEN": "dummy-secret-token",
        "CONFLUENCE_SPACE_KEY": "DOCS",
    }

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

    def _run_main(self, argv):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, self.CONFLUENCE_ENV, clear=True), \
             patch.object(sys, "argv", ["migrate.py"] + argv), \
             contextlib.redirect_stdout(stdout_buf), \
             contextlib.redirect_stderr(stderr_buf):
            migrate.main()
        return stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_visibility_flag_wiring_and_checkpoint_persistence(self):
        """--visibility private / space の配線と checkpoint.source_info['private_mode'] 保存を検証"""
        zpath = os.path.join(self.tmpdir, "vis.zip")
        _create_test_zip(zpath, "VisRoot")
        cp_private = os.path.join(self.tmpdir, "cp_private.json")
        cp_space = os.path.join(self.tmpdir, "cp_space.json")

        mock_importer = MagicMock()
        mock_importer.import_tree.return_value = {
            "11111111111111111111111111111111": {
                "title": "[HTML] VisRoot",
                "node_type": "page",
                "confluence_id": "2001",
                "url": "https://example.atlassian.net/wiki/pages/2001",
            }
        }

        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier"):
            self._run_main([
                "--zip", zpath,
                "--visibility", "private",
                "--no-verify",
                "--no-dashboard",
                "--checkpoint", cp_private,
            ])

        self.assertTrue(mock_imp_cls.call_args.kwargs["private_mode"])
        with open(cp_private, "r", encoding="utf-8") as f:
            cp_data = json.load(f)
        self.assertTrue(cp_data["source_info"]["private_mode"])

        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier"):
            self._run_main([
                "--zip", zpath,
                "--visibility", "space",
                "--no-verify",
                "--no-dashboard",
                "--checkpoint", cp_space,
            ])

        self.assertFalse(mock_imp_cls.call_args.kwargs["private_mode"])
        with open(cp_space, "r", encoding="utf-8") as f:
            cp_space_data = json.load(f)
        self.assertFalse(cp_space_data["source_info"]["private_mode"])

    def test_resume_restores_private_mode_from_checkpoint(self):
        """--resume 時、checkpoint.source_info['private_mode'] が True なら自動復元され、--visibility space 指定時は上書きできること"""
        zpath = os.path.join(self.tmpdir, "resume_priv.zip")
        _create_test_zip(zpath, "ResumedPrivate")
        cp_path = os.path.join(self.tmpdir, "cp_resume.json")

        cp = MigrationCheckpoint(checkpoint_path=cp_path)
        cp.status = "INTERRUPTED"
        cp.source_info = {
            "zip_paths": [zpath],
            "private_mode": True,
        }
        cp.save(force=True)

        mock_importer = MagicMock()
        mock_importer.import_tree.return_value = {
            "11111111111111111111111111111111": {
                "title": "[HTML] ResumedPrivate",
                "node_type": "page",
                "confluence_id": "3001",
                "url": "https://example.atlassian.net/wiki/pages/3001",
            }
        }

        # 1) --private / --visibility 指定なしの --resume で private_mode=True が復元される
        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier"):
            self._run_main([
                "--resume",
                "--no-verify",
                "--no-dashboard",
                "--checkpoint", cp_path,
            ])
        self.assertTrue(mock_imp_cls.call_args.kwargs["private_mode"])

        # 2) --visibility space を明示指定した --resume では private_mode=False となる
        cp.source_info["private_mode"] = True
        cp.save(force=True)
        with patch("migrate.ConfluenceImporter", return_value=mock_importer) as mock_imp_cls, \
             patch("migrate.MigrationVerifier"):
            self._run_main([
                "--resume",
                "--visibility", "space",
                "--no-verify",
                "--no-dashboard",
                "--checkpoint", cp_path,
            ])
        self.assertFalse(mock_imp_cls.call_args.kwargs["private_mode"])


if __name__ == "__main__":
    unittest.main()
