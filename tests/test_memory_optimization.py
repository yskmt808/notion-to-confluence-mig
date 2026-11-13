"""
tests/test_memory_optimization.py
4,000ページ規模の移行における省メモリ化（施策1: 添付ファイル遅延読み込み＆即時解放、施策2: BeautifulSoup多重パース廃止＆明示的破棄）の検証テスト。
"""

import os
import unittest
import zipfile
import tempfile
from unittest.mock import MagicMock

from src.html_extractor import HtmlZipExtractor, AttachmentBlockDict
from src.importer import ConfluenceImporter
from src.extractor import PageNode
from src.telemetry import SanitizedTelemetry
from src.checkpoint import MigrationCheckpoint
from src.verifier import MigrationVerifier


class TestMemoryOptimization(unittest.TestCase):
    def test_lazy_attachment_dict_behavior(self):
        """AttachmentBlockDict が平時はバイト列を保持せず、要求時にのみ遅延読み込みし、popで即時解放されること"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        sample_bytes = b"IMAGE_BINARY_DATA_12345"
        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("test_image.png", sample_bytes)

            block = AttachmentBlockDict({
                "id": "block-1",
                "type": "image",
                "_attachment_filename": "test_image.png",
                "_attachment_zip_path": tmp_zip,
                "_attachment_zip_entry": "test_image.png",
            })

            # 平時は内部辞書に raw bytes は入っていない
            self.assertNotIn("_attachment_bytes", block.keys())
            # しかし __contains__ では存在判定される
            self.assertIn("_attachment_bytes", block)

            # アクセスした瞬間に遅延読み込みされる
            self.assertEqual(block.get("_attachment_bytes"), sample_bytes)
            self.assertEqual(block["_attachment_bytes"], sample_bytes)

            # pop でキャッシュと参照情報が即時クリアされる
            val = block.pop("_attachment_bytes")
            self.assertEqual(val, sample_bytes)
            self.assertNotIn("_attachment_bytes", block)
            self.assertIsNone(block.get("_attachment_bytes"))
            self.assertNotIn("_attachment_zip_path", block)
            self.assertNotIn("_attachment_zip_entry", block)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    def test_html_zip_extractor_lazy_loads_attachments_without_bytes_in_ram(self):
        """HtmlZipExtractor.extract_tree() 実行直後はブロック辞書キーに生バイトが入っておらず、遅延読込可能であること"""
        html_content = """<!DOCTYPE html>
<html>
<head><title>Lazy Load Page</title></head>
<body>
<div class="page-body">
  <figure class="image"><img src="diagram.png"/></figure>
  <figure class="file"><a href="doc.pdf">PDF資料</a></figure>
</div>
</body>
</html>
"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        img_data = b"DIAGRAM_PNG_DATA"
        pdf_data = b"%PDF-1.4 TEST_PDF_DATA"

        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("lazy_page 4137be77284d804189c8d080073331b4.html", html_content.encode("utf-8"))
                zf.writestr("diagram.png", img_data)
                zf.writestr("doc.pdf", pdf_data)

            extractor = HtmlZipExtractor(zip_path=tmp_zip)
            root = extractor.extract_tree()

            self.assertEqual(len(root.blocks), 2)
            img_b = root.blocks[0]
            pdf_b = root.blocks[1]

            # 辞書キー直下には生バイト列が直接保持されていない（メタデータのみ）
            self.assertIsInstance(img_b, AttachmentBlockDict)
            self.assertIsInstance(pdf_b, AttachmentBlockDict)
            self.assertNotIn("_attachment_bytes", img_b.keys())
            self.assertNotIn("_attachment_bytes", pdf_b.keys())

            # get("_attachment_bytes") で要求された瞬間に初めてバイト列が返される
            self.assertEqual(img_b.get("_attachment_bytes"), img_data)
            self.assertEqual(pdf_b.get("_attachment_bytes"), pdf_data)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    def test_importer_processes_and_immediately_purges_attachment_memory(self):
        """ConfluenceImporter._process_attachments がアップロード完了直後にブロックからバイト情報と参照を完全除去すること"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        img_data = b"PHOTO_BYTES_TO_UPLOAD"
        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("photo.jpg", img_data)

            block = AttachmentBlockDict({
                "id": "b-photo",
                "type": "image",
                "_attachment_filename": "photo.jpg",
                "_attachment_zip_path": tmp_zip,
                "_attachment_zip_entry": "photo.jpg",
            })

            importer = ConfluenceImporter(
                url="https://example.atlassian.net/wiki",
                username="test_user",
                token="test_token",
                space_key="TEST",
            )
            # client.upload_attachment をモック化
            importer.client = MagicMock()

            # 添付ファイル処理実行
            uploaded = set()
            importer._process_attachments([block], page_id="12345", uploaded_filenames=uploaded)

            # client.upload_attachment が正しいバイト列で呼ばれたこと
            importer.client.upload_attachment.assert_called_once_with(
                page_id="12345",
                filename="photo.jpg",
                file_bytes=img_data,
                mime_type="image/jpeg",
            )
            self.assertIn("photo.jpg", uploaded)
            self.assertTrue(block.get("_attachment_uploaded"))

            # 処理完了後、ブロックから生データおよびZIP参照情報が綺麗に消去されていること（メモリ解放完了）
            self.assertNotIn("_attachment_bytes", block)
            self.assertNotIn("_attachment_zip_path", block)
            self.assertNotIn("_attachment_zip_entry", block)
            self.assertIsNone(block.get("_attachment_bytes"))
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    def test_database_fast_regex_title_extraction_without_dom_overhead(self):
        """データベース行のHTMLからBeautifulSoup DOMを使わずに正規表現で正確にタイトルが抽出されること"""
        root_html = """<!DOCTYPE html>
<html>
<head><title>Root Page</title></head>
<body><div class="page-body"></div></body>
</html>
"""
        row1_html = """<!DOCTYPE html>
<html>
<head><title>Task 001 - Urgent Fix</title></head>
<body><div class="page-body"><p>Body 1</p></div></body>
</html>
"""
        row2_html = """<!DOCTYPE html>
<head><title>  Task 002 - Feature Dev (extra info.html) </title></head>
<body><div class="page-body"><p>Body 2</p></div></body>
</html>
"""
        csv_content = """名前,ステータス
Task 001 - Urgent Fix,進行中
Task 002 - Feature Dev,未着手
"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("Root 4137be77284d804189c8d080073331b4.html", root_html)
                zf.writestr("Root/Tasks 11111111111111111111111111111111.csv", csv_content.encode("utf-8-sig"))
                zf.writestr("Root/Tasks/Task 001 - Urgent Fix 22222222222222222222222222222222.html", row1_html)
                zf.writestr("Root/Tasks/Task 002 - Feature Dev 33333333333333333333333333333333.html", row2_html)

            extractor = HtmlZipExtractor(zip_path=tmp_zip)
            root = extractor.extract_tree()
            self.assertIsNotNone(root)
            # ルート配下にデータベースが検出されていること
            self.assertEqual(len(root.children), 1)
            db_node = root.children[0]
            self.assertEqual(db_node.node_type, "database")
            self.assertEqual(len(db_node.children), 2)

            titles = [c.title for c in db_node.children]
            self.assertIn("Task 001 - Urgent Fix", titles)
            self.assertIn("Task 002 - Feature Dev", titles)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    def test_zip_indexing_avoids_repeated_namelist_scans(self):
        """施策3: extract_tree 実行時に _dir_direct_children と _file_set が構築され、高速な階層探索が行われること"""
        root_html = "<!DOCTYPE html><html><head><title>Root</title></head><body></body></html>"
        child1_html = "<!DOCTYPE html><html><head><title>Child 1</title></head><body></body></html>"
        child2_html = "<!DOCTYPE html><html><head><title>Child 2</title></head><body></body></html>"
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("Root 4137be77284d804189c8d080073331b4.html", root_html)
                zf.writestr("Root 4137be77284d804189c8d080073331b4/Child1 11111111111111111111111111111111.html", child1_html)
                zf.writestr("Root 4137be77284d804189c8d080073331b4/Child2 22222222222222222222222222222222.html", child2_html)

            extractor = HtmlZipExtractor(zip_path=tmp_zip)
            root = extractor.extract_tree()
            self.assertIsNotNone(root)
            self.assertEqual(len(root.children), 2)

            # インデックス辞書が正常に保持されていること
            self.assertTrue(hasattr(extractor, "_dir_direct_children"))
            self.assertTrue(hasattr(extractor, "_file_set"))
            self.assertIn("Root 4137be77284d804189c8d080073331b4.html", extractor._file_set)
            self.assertEqual(len(extractor._dir_direct_children["Root 4137be77284d804189c8d080073331b4/"]), 2)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    def test_importer_and_verifier_memory_pruning(self):
        """施策4: コメントインポート後、Pass 2更新後、および整合性検証後にノードメモリが順次解放されること"""
        # 1. importer: コメントインポート後の解放
        importer = ConfluenceImporter(
            url="https://example.atlassian.net/wiki",
            username="test_user",
            token="test_token",
            space_key="TEST",
            prune_blocks_on_pass2=True,
        )
        importer.client = MagicMock()
        importer.client.create_page.return_value = {"id": "conf-page-1"}
        importer.client.create_comment.return_value = {"id": "comment-1"}

        node = PageNode(
            id="page-1",
            title="Pruning Test Page",
            node_type="page",
            blocks=[{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello"}]}}],
            comments=[{"rich_text": [{"plain_text": "A comment"}], "created_by": {"name": "Alice"}}],
        )

        importer._import_node_recursive(node, parent_id=None, overwrite=False)
        # コメントが移行された後、node.comments が空リストに解放されていること
        self.assertEqual(node.comments, [])
        importer.client.create_comment.assert_called_once()

        # 2. importer: Pass 2 完了後の blocks / properties 解放 (prune_blocks_on_pass2=True)
        importer.client.get_page_by_id.return_value = {"id": "conf-page-1", "version": {"number": 1}}
        importer._resolve_and_update_single_node(node)
        self.assertEqual(node.blocks, [])
        self.assertEqual(node.properties, {})

        # 3. verifier: verify_page_node 完了後の解放
        node_for_verify = PageNode(
            id="page-v",
            title="Verify Prune Page",
            node_type="page",
            blocks=[{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Verify Text"}]}}],
            comments=[{"rich_text": [{"plain_text": "Comment"}]}],
        )
        node_for_verify.properties = {"Category": "Engineering"}
        verifier = MigrationVerifier(importer.client)
        importer.client.get_page_by_id.return_value = {
            "id": "conf-page-v",
            "title": "Verify Prune Page",
            "body": {
                "storage": {"value": "<p>Verify Text</p>"},
                "view": {"value": "<p>Verify Text</p>"},
            },
            "ancestors": [],
        }

        res = verifier.verify_page_node(node_for_verify, confluence_page_id="conf-page-v")
        self.assertEqual(res["status"], "PASS")
        # 検証完了後にノードの重い属性が解放されていること
        self.assertEqual(node_for_verify.blocks, [])
        self.assertEqual(node_for_verify.properties, {})
        self.assertEqual(node_for_verify.comments, [])

    def test_telemetry_memory_bounding_and_aggregations(self):
        """施策5: テレメトリの http_requests と warnings が上限1000件でバッファリングされ、集計が正確であること"""
        telemetry = SanitizedTelemetry()

        # 1,500件のHTTP通信を記録
        for i in range(1, 1501):
            telemetry.record_http_request(
                method="GET",
                endpoint_pattern=f"/api/v2/pages/{i}",
                status_code=200,
                duration_ms=float(i % 100 + 10),
            )

        # 保持リストは最大1,000件に収まっていること（OOM防止）
        self.assertEqual(len(telemetry.http_requests), 1000)
        # しかし総リクエスト数カウンタは1,500を正確に保持していること
        self.assertEqual(telemetry.total_http_requests, 1500)

        # 1,200件の警告を記録
        for i in range(1200):
            telemetry.record_warning(f"Warning {i}")

        self.assertEqual(len(telemetry.warnings), 1000)
        self.assertEqual(telemetry.total_warnings_count, 1200)

        # レポート生成時の集計値確認
        report = telemetry.generate_report()
        http_summary = report["diagnostics"]["http_summary"]
        self.assertEqual(http_summary["total_requests"], 1500)
        self.assertGreater(http_summary["latency_ms"]["min"], 0)
        self.assertGreater(http_summary["latency_ms"]["max"], 0)
        self.assertEqual(report["warnings_count"], 1200)

    def test_checkpoint_throttled_saving_and_flush(self):
        """施策5: チェックポイントの間引き書き込み（throttled save）と flush/強制保存が機能すること"""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            tmp_cp = tf.name

        try:
            # 10秒間隔で書き込みを間引く設定
            cp = MigrationCheckpoint(checkpoint_path=tmp_cp, save_interval_sec=10.0, max_pending_changes=50)

            # 1回目は必ず保存される（初期 last_saved_time = 0.0 のため）
            cp.record_pass1("node-1", "conf-1", "Title 1", "http://conf/1", "page")
            mtime_initial = os.path.getmtime(tmp_cp)

            # 2回目〜5回目は10秒以内かつ50件未満のため、ファイルI/Oが間引かれる
            import time
            time.sleep(0.05)
            for i in range(2, 6):
                cp.record_pass1(f"node-{i}", f"conf-{i}", f"Title {i}", f"http://conf/{i}", "page")

            # ファイル更新時刻が変わっていないこと（ディスク書き込みがスキップされたこと）
            mtime_after_batch = os.path.getmtime(tmp_cp)
            self.assertEqual(mtime_initial, mtime_after_batch)
            self.assertEqual(cp._pending_changes, 4)

            # flush() を呼び出すと即座に強制保存されること
            time.sleep(0.05)
            cp.flush()
            mtime_after_flush = os.path.getmtime(tmp_cp)
            self.assertGreater(mtime_after_flush, mtime_initial)
            self.assertEqual(cp._pending_changes, 0)

            # 読み込み直して全5ノードが確実に書き込まれていること
            cp_reader = MigrationCheckpoint(checkpoint_path=tmp_cp)
            loaded = cp_reader.load()
            self.assertTrue(loaded)
            self.assertEqual(len(cp_reader.nodes), 5)
        finally:
            if os.path.exists(tmp_cp):
                os.remove(tmp_cp)
            tmp_bak = f"{tmp_cp}.tmp"
            if os.path.exists(tmp_bak):
                os.remove(tmp_bak)


if __name__ == "__main__":
    unittest.main()
