import io
import json
import logging
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import src.logger_util as logger_util
from src.cancellation import CancellationManager
from src.checkpoint import CheckpointWriteError, MigrationCheckpoint
from src.extractor import PageNode
from src.importer import ConfluenceImporter


def _make_err_with_winerror(winerr: int, msg: str) -> OSError:
    err = OSError(msg)
    err.winerror = winerr
    return err


class TestCheckpointResilience(unittest.TestCase):
    """TCK_00014: チェックポイント保存時の WinError 5/32 耐性強化と書込不能時 ERROR 即時停止の検証"""

    def setUp(self):
        logger_util.setup_notice_log_level()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_transient_winerror5_and_32_retry_recovers_without_error_log(self):
        """一時的な PermissionError / WinError 5, 32 発生時に指数バックオフでリトライ復旧し ERROR ログを出さないこと"""
        cp_path = os.path.join(self._tmpdir.name, "cp_retry.json")
        cp = MigrationCheckpoint(cp_path, max_retries=4, retry_base_delay=0.05)

        real_replace = os.replace
        attempts = {"count": 0}

        def flaky_replace(src, dst):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError(13, "[WinError 5] アクセスが拒否されました。")
            if attempts["count"] == 2:
                raise _make_err_with_winerror(32, "[WinError 32] プロセスはファイルにアクセスできません。")
            if attempts["count"] == 3:
                raise _make_err_with_winerror(5, "[WinError 5] Access is denied")
            return real_replace(src, dst)

        with self.assertLogs("src.checkpoint", level="WARNING") as cm:
            with patch("src.checkpoint.os.replace", side_effect=flaky_replace), \
                 patch("src.checkpoint.time.sleep", return_value=None) as mock_sleep:
                cp.record_pass1("n1", "c1", "Page 1", "https://example.com/1", "page")

        self.assertEqual(attempts["count"], 4)
        self.assertEqual(mock_sleep.call_count, 3)
        self.assertFalse(cp.write_failed)

        # リトライ中の復旧可能事象は WARNING のみで ERROR ログは出力されないこと
        self.assertTrue(any(rec.levelno == logging.WARNING for rec in cm.records))
        self.assertFalse(any(rec.levelno >= logging.ERROR for rec in cm.records))

        # 保存された JSON が正しく読み込め、一時ファイルが残留していないこと
        with open(cp_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["nodes"]["n1"]["confluence_id"], "c1")
        leftovers = [fn for fn in os.listdir(self._tmpdir.name) if fn.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_creates_parent_dir_skips_chmod_on_windows_and_uses_compact_json(self):
        """親ディレクトリ不在時の自動作成・Windows環境(os.name=='nt')でのchmodスキップ・コンパクトJSON書出を検証"""
        cp_path = os.path.join(self._tmpdir.name, "nested", "subdir", "cp.json")
        cp = MigrationCheckpoint(cp_path, max_retries=1, retry_base_delay=0.01)

        with patch("src.checkpoint.os.name", "nt"), \
             patch("src.checkpoint.os.chmod") as mock_chmod:
            cp.record_pass1("n1", "c1", "Compact Title", "https://example.com/1", "page")

        mock_chmod.assert_not_called()
        self.assertTrue(os.path.exists(cp_path))
        with open(cp_path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("\n  ", raw)
        data = json.loads(raw)
        self.assertEqual(data["nodes"]["n1"]["title"], "Compact Title")

    def test_permanent_write_failure_logs_error_and_raises_checkpoint_write_error(self):
        """全リトライ超過の恒久書込失敗時は ERROR ログを出力し CheckpointWriteError を送出すること"""
        cp_path = os.path.join(self._tmpdir.name, "cp_fail.json")
        cp = MigrationCheckpoint(cp_path, max_retries=2, retry_base_delay=0.01)

        with self.assertLogs("src.checkpoint", level="ERROR") as cm:
            with patch(
                "src.checkpoint.os.replace",
                side_effect=PermissionError(13, "[WinError 5] アクセスが拒否されました。"),
            ), patch("src.checkpoint.time.sleep", return_value=None) as mock_sleep:
                with self.assertRaises(CheckpointWriteError):
                    cp.record_pass1("n1", "c1", "Page 1", "https://example.com/1", "page")

        self.assertEqual(mock_sleep.call_count, 2)
        self.assertTrue(cp.write_failed)
        self.assertTrue(any(rec.levelno == logging.ERROR for rec in cm.records))
        leftovers = [fn for fn in os.listdir(self._tmpdir.name) if fn.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_importer_pass1_workers_and_pass2_do_not_swallow_checkpoint_write_error(self):
        """ConfluenceImporter の Pass 1 並行ワーカーおよび Pass 2 で CheckpointWriteError が握りつぶされず Fail-Fast 停止すること"""
        # 1. Pass 1 並行ワーカーでの書込不能 -> 即時停止 + CancelManager error halt
        cp_p1 = MigrationCheckpoint(
            os.path.join(self._tmpdir.name, "cp_imp_p1.json"),
            max_retries=1,
            retry_base_delay=0.0,
        )
        cm_p1 = CancellationManager()
        importer_p1 = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="u",
            token="t",
            space_key="TEST",
            checkpoint=cp_p1,
            cancel_manager=cm_p1,
            page_workers=2,
        )
        importer_p1._create_or_update_page = MagicMock(side_effect=[
            ("c_root", "https://example.com/root"),
            ("c_child1", "https://example.com/c1"),
            ("c_child2", "https://example.com/c2"),
        ])
        importer_p1.client.get_page_by_id = MagicMock(return_value={"id": "x", "version": {"number": 1}})
        importer_p1.client.get_page_by_title = MagicMock(return_value=None)
        importer_p1.client.get_database_by_title = MagicMock(return_value=None)
        importer_p1.client.update_page = MagicMock(return_value={"id": "x", "_links": {"webui": "/p"}})

        root = PageNode(
            id="r1",
            title="Root",
            node_type="page",
            children=[
                PageNode(id="c1", title="Child 1", node_type="page"),
                PageNode(id="c2", title="Child 2", node_type="page"),
            ],
        )

        # ルートの Pass 1 保存は成功させ、子ノード処理時に恒久書込失敗を発生させる
        real_save = cp_p1.save
        call_counter = {"n": 0}

        def fail_on_second_save(*args, **kwargs):
            call_counter["n"] += 1
            if call_counter["n"] >= 2:
                with patch("src.checkpoint.os.replace", side_effect=PermissionError("[WinError 5] denied")):
                    return real_save(*args, **kwargs)
            return real_save(*args, **kwargs)

        cp_p1.save = fail_on_second_save
        with self.assertRaises((CheckpointWriteError, Exception)):
            importer_p1.import_tree(root, parent_id=None, overwrite=False)
        self.assertTrue(cm_p1.is_error_halt)

        # 2. Pass 2 (_resolve_and_update_single_node) でも WARNING として握りつぶさず伝播すること
        cp_p2 = MigrationCheckpoint(
            os.path.join(self._tmpdir.name, "cp_imp_p2.json"),
            max_retries=0,
            retry_base_delay=0.0,
        )
        cm_p2 = CancellationManager()
        importer_p2 = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="u",
            token="t",
            space_key="TEST",
            checkpoint=cp_p2,
            cancel_manager=cm_p2,
            page_workers=1,
        )
        importer_p2.client.get_page_by_id = MagicMock(return_value={"id": "c_p2", "version": {"number": 1}})
        importer_p2.client.update_page = MagicMock(return_value={"id": "c_p2"})
        p2_node = PageNode(
            id="n_p2",
            title="Pass2 Node",
            node_type="page",
            blocks=[{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "hello"}]}}],
        )
        importer_p2.migration_map["n_p2"] = {
            "confluence_id": "c_p2",
            "title": "Pass2 Node",
            "url": "https://example.com/c_p2",
            "node_type": "page",
        }
        cp_p2.record_pass2 = MagicMock(side_effect=CheckpointWriteError("permanent disk write error"))
        with self.assertRaises(CheckpointWriteError):
            importer_p2._resolve_and_update_single_node(p2_node)
        self.assertTrue(cm_p2.is_error_halt)

    def test_migrate_cli_exits_cleanly_on_checkpoint_write_error_without_secondary_panic(self):
        """migrate.py でチェックポイント恒久書込障害が発生した場合、二次パニックせず ERROR ログ＋終了コード1で停止すること"""
        import migrate

        zip_path = os.path.join(self._tmpdir.name, "sample.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "Root 11111111111111111111111111111111.html",
                "<html><head><title>Root</title></head><body><article><div class='page-body'><p>hello</p></div></article></body></html>",
            )
        cp_path = os.path.join(self._tmpdir.name, "cli_cp.json")

        env_vars = {
            "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
            "CONFLUENCE_USERNAME": "u@example.com",
            "CONFLUENCE_API_TOKEN": "tok",
            "CONFLUENCE_SPACE_KEY": "TEST",
        }

        # Step 2 実行中にチェックポイント保存が恒久失敗するケースを再現
        def fake_import_tree(self_imp, root_node, parent_id=None, overwrite=True):
            # 1回目の checkpoint.save() (Step 1) は成功させ、Step 2 の record_pass1 相当で恒久失敗させる
            self_imp.checkpoint.write_failed = True
            err = CheckpointWriteError("Failed to write checkpoint atomically: [WinError 5]")
            if self_imp.cancel_manager:
                self_imp.cancel_manager.trigger_error_halt(str(err))
            raise err

        with patch.dict(os.environ, env_vars, clear=False), \
             patch("sys.argv", ["migrate.py", "--zip", zip_path, "--no-dashboard", "--no-verify", "--checkpoint", cp_path]), \
             patch("migrate.setup_logger"), \
             patch("migrate.ConfluenceImporter.import_tree", fake_import_tree), \
             patch("migrate.MigrationCheckpoint.mark_interrupted") as mock_mark_int, \
             patch("migrate.MigrationCheckpoint.flush") as mock_flush, \
             patch("sys.stdout", new_callable=io.StringIO):
            with self.assertLogs(level="ERROR") as cm:
                with self.assertRaises(SystemExit) as exit_ctx:
                    migrate.main()

        self.assertEqual(exit_ctx.exception.code, 1)
        self.assertTrue(any(rec.levelno == logging.ERROR for rec in cm.records))
        self.assertTrue(any("WinError 5" in rec.getMessage() for rec in cm.records))
        # write_failed=True 済みの場合、cleanup で mark_interrupted / flush の二次失敗ループを起こさないこと
        mock_mark_int.assert_not_called()
        mock_flush.assert_not_called()


if __name__ == "__main__":
    unittest.main()
