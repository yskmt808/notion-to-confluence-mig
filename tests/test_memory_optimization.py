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


if __name__ == "__main__":
    unittest.main()
