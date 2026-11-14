"""HTMLエクスポート未収録の外部同期ブロック検知および --dry-run / 移行後検証 WARNING カウントのテスト (TCK_00009)。"""

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
from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode
from src.html_extractor import HtmlZipExtractor
from src.telemetry import SanitizedTelemetry
from src.verifier import MigrationVerifier
try:
    from storage_xml_helper import assert_valid_storage_xml
except ImportError:
    from tests.storage_xml_helper import assert_valid_storage_xml


def _build_html_doc(title: str, body_inner: str, page_id: str = "11111111-1111-1111-1111-111111111111") -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
  <article id="{page_id}" class="page">
    <header><h1 class="page-title">{title}</h1></header>
    <div class="page-body">
      {body_inner}
    </div>
  </article>
</body>
</html>"""


class TestExternalSyncedBlockDetection(unittest.TestCase):
    """HtmlZipExtractor による外部同期ブロック検知と通常同期ブロック非誤検知の単体テスト。"""

    def test_detects_empty_and_external_ref_synced_blocks(self):
        root_html = _build_html_doc(
            "External Sync Root",
            """
            <p>通常段落</p>
            <div dir="auto" id="ext-empty-1"></div>
            <div dir="auto"><div dir="auto" id="ext-nested-2"></div></div>
            <div class="synced-block" data-synced-from="orig-ext-999" id="ext-attr-3"></div>
            <div class="synced-block" data-synced-from="https://www.notion.so/other/Page-aaaa1111bbbb2222cccc3333dddd4444" id="ext-link-4">
              <a href="https://www.notion.so/other/Page-aaaa1111bbbb2222cccc3333dddd4444">External Synced Ref</a>
            </div>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "ext_sync.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("External Sync Root 11111111111111111111111111111111.html", root_html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        self.assertEqual(len(tree.external_synced_blocks), 4)
        synced_blocks = [b for b in tree.blocks if b.get("type") == "synced_block"]
        self.assertEqual(len(synced_blocks), 4)
        self.assertTrue(all(b.get("_is_external_synced_block") for b in synced_blocks))
        self.assertEqual(synced_blocks[0]["id"], "ext-empty-1")
        self.assertEqual(synced_blocks[2]["synced_block"]["synced_from"]["block_id"], "orig-ext-999")
        self.assertIn("notion.so", synced_blocks[3]["synced_block"]["synced_from"]["block_id"])

        # Storage Format 変換が well-formed XML を出力すること
        conv = NotionToConfluenceConverter()
        storage_xml = conv.blocks_to_storage_format(tree.blocks)
        assert_valid_storage_xml(self, storage_xml)
        self.assertIn("外部同期ブロック", storage_xml)
        self.assertIn("orig-ext-999", storage_xml)

    def test_detects_nested_external_synced_blocks_in_column_and_toggle(self):
        html = _build_html_doc(
            "Nested External Sync",
            """
            <div class="column-list">
              <div class="column">
                <div dir="auto" id="col-ext-sync"></div>
              </div>
              <div class="column"></div>
            </div>
            <details>
              <summary>トグル見出し</summary>
              <div class="indented">
                <div class="synced-block" data-synced-from="orig-in-toggle"></div>
              </div>
            </details>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "nested_ext.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Nested External Sync 22222222222222222222222222222222.html", html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        # 空の column div 自体は誤検知せず、column 内の <div dir="auto"> と toggle 内の synced-block の2件のみ検知
        self.assertEqual(len(tree.external_synced_blocks), 2)
        refs = [item.get("synced_from") for item in tree.external_synced_blocks]
        self.assertIn("orig-in-toggle", refs)

    def test_internal_synced_blocks_do_not_produce_false_positives(self):
        """本文がHTMLエクスポートに含まれている通常の内部同期ブロックは警告対象にならないこと。"""
        html = _build_html_doc(
            "Internal Sync Only",
            """
            <div dir="auto">
              <div dir="auto">
                <aside class="block-color-blue_background callout" id="callout-ok">
                  <p>同期ブロック１（本文あり）</p>
                </aside>
              </div>
            </div>
            <div dir="auto">
              <p>通常の同期段落テキスト</p>
            </div>
            <div class="synced-block" data-synced-from="orig-ok">
              <p>展開済みの同期ブロック本文</p>
            </div>
            """,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "internal_sync.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Internal Sync Only 33333333333333333333333333333333.html", html)

            tree = HtmlZipExtractor(zpath).extract_tree()

        self.assertEqual(tree.external_synced_blocks, [])
        self.assertEqual(len(tree.blocks), 3)
        self.assertEqual(tree.blocks[0]["type"], "callout")
        self.assertEqual(tree.blocks[1]["type"], "paragraph")
        self.assertEqual(tree.blocks[2]["type"], "paragraph")


class TestTelemetryAndVerifierExternalSyncedBlocks(unittest.TestCase):
    """SanitizedTelemetry と MigrationVerifier での警告記録・WARN判定の単体テスト。"""

    def test_telemetry_counts_external_synced_blocks_without_leaking_titles(self):
        root = PageNode(
            id="aaaa1111bbbb2222cccc3333dddd4444",
            title="Secret Roadmap Page",
            node_type="page",
            blocks=[
                {
                    "id": "sync-1",
                    "type": "synced_block",
                    "synced_block": {"synced_from": {"type": "block_id", "block_id": "orig-111"}},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                    "_external_synced_warning": "外部同期ブロックの本文がHTMLエクスポートに含まれていません",
                },
                {
                    "id": "sync-2",
                    "type": "synced_block",
                    "synced_block": {"synced_from": None},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                    "_external_synced_warning": "外部同期ブロックの本文がHTMLエクスポートに含まれていません",
                },
            ],
            external_synced_blocks=[
                {"block_id": "sync-1", "synced_from": "orig-111", "message": "missing"},
                {"block_id": "sync-2", "synced_from": None, "message": "missing"},
            ],
        )
        telemetry = SanitizedTelemetry()
        telemetry.record_tree(root)
        report = telemetry.generate_report()

        self.assertEqual(telemetry.total_warnings_count, 2)
        self.assertEqual(report["warnings_count"], 2)
        self.assertEqual(report["diagnostics"]["external_synced_blocks_count"], 2)
        report_json = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Secret Roadmap Page", report_json)

    def test_verifier_marks_external_synced_block_as_warn_and_internal_as_pass(self):
        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []
        verifier = MigrationVerifier(mock_client)

        # 1. 外部同期ブロックを含むページ -> WARN
        ext_node = PageNode(
            id="p-ext",
            title="Ext Sync Page",
            node_type="page",
            blocks=[
                {"id": "p1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "通常本文"}]}},
                {
                    "id": "s1",
                    "type": "synced_block",
                    "synced_block": {"synced_from": {"type": "block_id", "block_id": "orig-ext-id"}},
                    "children_blocks": [],
                    "_is_external_synced_block": True,
                },
            ],
            external_synced_blocks=[
                {"block_id": "s1", "synced_from": "orig-ext-id", "message": "missing"}
            ],
        )
        mock_client.get_page_by_id.return_value = {
            "id": "1001",
            "title": "Ext Sync Page",
            "ancestors": [{"id": "999"}],
            "body": {
                "storage": {"value": "<p>通常本文</p><p>🔄 <em>同期ブロック（同期元: orig-ext-id）</em></p>"},
                "view": {"value": "<p>通常本文</p>"},
            },
        }
        res_warn = verifier.verify_page_node(ext_node, "1001", expected_parent_id="999")
        self.assertEqual(res_warn["status"], "WARN")
        self.assertTrue(any("外部同期ブロック" in w for w in res_warn["warnings"]))
        self.assertEqual(res_warn["issues"], [])

        # 2. 内部同期ブロック（展開済み）のみのページ -> PASS
        int_node = PageNode(
            id="p-int",
            title="Internal Sync Page",
            node_type="page",
            blocks=[
                {
                    "id": "s-ok",
                    "type": "synced_block",
                    "synced_block": {"synced_from": None},
                    "children_blocks": [
                        {"id": "p2", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "展開済み本文"}]}}
                    ],
                }
            ],
        )
        mock_client.get_page_by_id.return_value = {
            "id": "1002",
            "title": "Internal Sync Page",
            "ancestors": [{"id": "999"}],
            "body": {
                "storage": {"value": "<div><p>展開済み本文</p></div>"},
                "view": {"value": "<p>展開済み本文</p>"},
            },
        }
        res_pass = verifier.verify_page_node(int_node, "1002", expected_parent_id="999")
        self.assertEqual(res_pass["status"], "PASS")
        self.assertEqual(res_pass["warnings"], [])


class TestExternalSyncedBlockCliIntegration(unittest.TestCase):
    """HTML ZIP を用いた --dry-run および移行後検証 (Step 3) の結合テスト。"""

    CONFLUENCE_ENV = {
        "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "bot@example.com",
        "CONFLUENCE_API_TOKEN": "dummy-token",
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

    def _run_main(self, argv, env=None):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, env or {}, clear=True), \
             patch.object(sys, "argv", ["migrate.py"] + argv), \
             contextlib.redirect_stdout(stdout_buf), \
             contextlib.redirect_stderr(stderr_buf):
            migrate.main()
        return stdout_buf.getvalue(), stderr_buf.getvalue()

    def _create_mixed_sync_zip(self, zip_path: str):
        root_id = "11111111111111111111111111111111"
        child_ext_id = "22222222222222222222222222222222"
        child_ok_id = "33333333333333333333333333333333"

        root_html = _build_html_doc(
            "Root Hub",
            """
            <p>ルート本文</p>
            <div dir="auto" id="root-ext-sync"></div>
            """,
            page_id=root_id,
        )
        child_ext_html = _build_html_doc(
            "External Ref Child",
            """
            <p>子ページ本文</p>
            <div class="synced-block" data-synced-from="orig-outside-777" id="child-ext-sync"></div>
            """,
            page_id=child_ext_id,
        )
        child_ok_html = _build_html_doc(
            "Internal Sync Child",
            """
            <div dir="auto">
              <aside class="block-color-blue_background callout" id="ok-callout">
                <p>内部同期ブロック本文</p>
              </aside>
            </div>
            """,
            page_id=child_ok_id,
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"Root Hub {root_id}.html", root_html.encode("utf-8"))
            zf.writestr(f"Root Hub/External Ref Child {child_ext_id}.html", child_ext_html.encode("utf-8"))
            zf.writestr(f"Root Hub/Internal Sync Child {child_ok_id}.html", child_ok_html.encode("utf-8"))

    def test_dry_run_counts_external_synced_warnings_and_lists_affected_pages(self):
        zpath = os.path.join(self.tmpdir, "mixed_sync.zip")
        self._create_mixed_sync_zip(zpath)
        report_dir = os.path.join(self.tmpdir, "reports")
        os.makedirs(report_dir, exist_ok=True)
        cp_path = os.path.join(self.tmpdir, "cp_dry.json")

        with patch("migrate.ConfluenceImporter") as mock_imp_cls:
            out, _ = self._run_main([
                "--zip", zpath,
                "--dry-run",
                "--checkpoint", cp_path,
                "--generate-debug-report",
                "--debug-report-dir", report_dir,
            ])

        mock_imp_cls.assert_not_called()
        self.assertIn("ドライラン結果サマリー: WARNING 2 件", out)
        self.assertIn("外部同期ブロックを 2 件検出しました（対象ページ: 2 件）", out)
        self.assertIn("[HTML] Root Hub", out)
        self.assertIn("[HTML] External Ref Child", out)
        self.assertNotIn("- [page] [HTML] Internal Sync Child", out)

        with open(os.path.join(report_dir, "debug_report.json"), "r", encoding="utf-8") as f:
            report_data = json.load(f)
        self.assertEqual(report_data["warnings_count"], 2)
        self.assertEqual(report_data["diagnostics"]["external_synced_blocks_count"], 2)

    def test_post_migration_verification_counts_external_synced_warnings(self):
        zpath = os.path.join(self.tmpdir, "mixed_sync.zip")
        self._create_mixed_sync_zip(zpath)
        cp_path = os.path.join(self.tmpdir, "cp_full.json")

        root_id = "11111111-1111-1111-1111-111111111111"
        child_ext_id = "22222222-2222-2222-2222-222222222222"
        child_ok_id = "33333333-3333-3333-3333-333333333333"

        mock_client = MagicMock()
        mock_client.get_page_attachments.return_value = []

        def fake_get_page_by_id(page_id, expand=None):
            pages = {
                "5001": {
                    "id": "5001",
                    "title": "[HTML] Root Hub",
                    "ancestors": [{"id": "999"}],
                    "body": {"storage": {"value": "<p>ルート本文</p>"}, "view": {"value": "<p>ルート本文</p>"}},
                },
                "5002": {
                    "id": "5002",
                    "title": "[HTML] External Ref Child",
                    "ancestors": [{"id": "5001"}],
                    "body": {
                        "storage": {"value": "<p>子ページ本文</p><p>🔄 <em>同期ブロック（同期元: orig-outside-777）</em></p>"},
                        "view": {"value": "<p>子ページ本文</p>"},
                    },
                },
                "5003": {
                    "id": "5003",
                    "title": "[HTML] Internal Sync Child",
                    "ancestors": [{"id": "5001"}],
                    "body": {
                        "storage": {"value": '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>内部同期ブロック本文</p></ac:rich-text-body></ac:structured-macro>'},
                        "view": {"value": "<p>内部同期ブロック本文</p>"},
                    },
                },
            }
            return pages.get(page_id)

        mock_client.get_page_by_id.side_effect = fake_get_page_by_id

        mock_importer = MagicMock()
        mock_importer.client = mock_client
        mock_importer.import_tree.return_value = {
            root_id: {"title": "[HTML] Root Hub", "node_type": "page", "confluence_id": "5001", "url": "https://example.invalid/5001"},
            child_ext_id: {"title": "[HTML] External Ref Child", "node_type": "page", "confluence_id": "5002", "url": "https://example.invalid/5002"},
            child_ok_id: {"title": "[HTML] Internal Sync Child", "node_type": "page", "confluence_id": "5003", "url": "https://example.invalid/5003"},
        }

        with patch("migrate.ConfluenceImporter", return_value=mock_importer):
            out, _ = self._run_main(
                [
                    "--zip", zpath,
                    "--no-dashboard",
                    "--confluence-parent-id", "999",
                    "--checkpoint", cp_path,
                ],
                env=self.CONFLUENCE_ENV,
            )

        self.assertIn("検証サマリー: 合計 3 ページ | 正常: 1 | 警告: 2 | 失敗: 0", out)
        self.assertIn("⚠️ [WARN] [HTML] Root Hub (ID: 5001)", out)
        self.assertIn("⚠️ [WARN] [HTML] External Ref Child (ID: 5002)", out)
        self.assertIn("✅ [PASS] [HTML] Internal Sync Child (ID: 5003)", out)
        self.assertIn("HTMLエクスポート未収録・未展開の外部同期ブロックを検出しました", out)
        self.assertIn("orig-outside-777", out)


if __name__ == "__main__":
    unittest.main()
