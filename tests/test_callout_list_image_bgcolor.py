"""
Unit tests for nested callouts in lists, images inside callouts, and background colors.
"""

import unittest
import zipfile
import io
import os
from src.converter import NotionToConfluenceConverter, NOTION_BG_COLOR_MAP
from src.html_extractor import HtmlZipExtractor
from storage_xml_helper import assert_valid_storage_xml


class TestCalloutListImageBgColor(unittest.TestCase):

    def setUp(self):
        self.conv = NotionToConfluenceConverter()

    def test_callout_inside_bulleted_list_conversion(self):
        """箇条書きリスト内のコールアウトが <li>...<ac:structured-macro ac:name="panel">...</li> に正しく変換されること"""
        blocks = [
            {
                "id": "li-1",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": "親リスト項目"}, "plain_text": "親リスト項目"}],
                },
                "children_blocks": [
                    {
                        "id": "co-1",
                        "type": "callout",
                        "callout": {
                            "rich_text": [{"type": "text", "text": {"content": "リスト内の補足情報"}, "plain_text": "リスト内の補足情報"}],
                            "icon": {"type": "emoji", "emoji": "💡"},
                            "color": "yellow_background",
                        },
                    }
                ],
            }
        ]
        storage = self.conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, storage)
        self.assertIn("<ul><li>親リスト項目", storage)
        self.assertIn('<ac:structured-macro ac:name="panel">', storage)
        self.assertIn('<ac:parameter ac:name="bgColor">#fbf3db</ac:parameter>', storage)
        self.assertIn("<p>💡 リスト内の補足情報</p>", storage)
        self.assertIn("</li></ul>", storage)

    def test_callout_with_embedded_image_conversion(self):
        """コールアウト内の画像が panel マクロの rich-text-body 内部に出力されること"""
        blocks = [
            {
                "id": "co-img",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": "図解解説"}, "plain_text": "図解解説"}],
                    "icon": {"type": "emoji", "emoji": "🖼️"},
                    "color": "blue_background",
                },
                "children_blocks": [
                    {
                        "id": "img-1",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {"url": "diagram.png"},
                            "caption": [],
                        },
                        "_attachment_filename": "diagram.png",
                    }
                ],
            }
        ]
        storage = self.conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, storage)
        self.assertIn('<ac:structured-macro ac:name="panel">', storage)
        self.assertIn('<ac:parameter ac:name="bgColor">#edf5fb</ac:parameter>', storage)
        self.assertIn("<p>🖼️ 図解解説</p>", storage)
        self.assertIn('<ri:attachment ri:filename="diagram.png"', storage)
        # 画像が panel マクロより前に閉じられていないこと
        panel_end_idx = storage.index("</ac:structured-macro>")
        img_idx = storage.index('ri:filename="diagram.png"')
        self.assertLess(img_idx, panel_end_idx, "Image must be placed inside the panel macro")

    def test_all_background_colors_rendering(self):
        """全9種類のNotion背景色が <span style="background-color: ..."> に正しく変換されること"""
        colors = [
            "gray_background", "brown_background", "orange_background", "yellow_background",
            "green_background", "teal_background", "blue_background", "purple_background", "pink_background", "red_background"
        ]
        for c in colors:
            rt = [
                {
                    "type": "text",
                    "text": {"content": f"Text with {c}"},
                    "plain_text": f"Text with {c}",
                    "annotations": {"color": c},
                }
            ]
            html_out = self.conv.convert_rich_text(rt)
            assert_valid_storage_xml(self, html_out)
            expected_hex = NOTION_BG_COLOR_MAP["green_background" if c == "teal_background" else c]
            self.assertIn(f"background-color: {expected_hex};", html_out)

    def test_block_level_color_inheritance(self):
        """ブロックレベルの背景色 (paragraph.color) が annotations.color == 'default' のテキストに継承されること"""
        blocks = [
            {
                "id": "p-yellow",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": "黄色背景の段落"}, "plain_text": "黄色背景の段落", "annotations": {"color": "default"}}],
                    "color": "yellow_background",
                },
            }
        ]
        storage = self.conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, storage)
        self.assertIn(f"background-color: {NOTION_BG_COLOR_MAP['yellow_background']};", storage)

    def test_html_extractor_nested_callout_in_list(self):
        """HTMLエクスポート内の li 直下にある aside.callout が children_blocks として正しく分離抽出されること"""
        import tempfile
        html_content = """<!DOCTYPE html>
<html>
<head><title>Test List Callout</title></head>
<body>
<div class="page-body">
  <ul class="bulleted-list">
    <li style="list-style-type:disc">親項目テキスト
      <aside class="block-color-blue_background callout" style="white-space:pre-wrap;display:flex">
        <div style="font-size:1.5em"><span class="icon">ℹ️</span></div>
        <div style="width:100%"><p>ネストされたコールアウト本文</p></div>
      </aside>
    </li>
  </ul>
</div>
</body>
</html>
"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_zip = tmp.name
        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("test_page 4137be77284d804189c8d080073331b4.html", html_content)

            extractor = HtmlZipExtractor(zip_path=tmp_zip)
            node = extractor.extract_tree()
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

        self.assertEqual(len(node.blocks), 1)
        li_block = node.blocks[0]
        self.assertEqual(li_block["type"], "bulleted_list_item")
        # リスト項目のテキストにコールアウトの文言が混入していないこと
        li_text = "".join(t["plain_text"] for t in li_block["bulleted_list_item"]["rich_text"])
        self.assertEqual(li_text.strip(), "親項目テキスト")

        # children_blocks に callout が抽出されていること
        self.assertEqual(len(li_block["children_blocks"]), 1)
        co_block = li_block["children_blocks"][0]
        self.assertEqual(co_block["type"], "callout")
        co_text = "".join(t["plain_text"] for t in co_block["callout"]["rich_text"])
        self.assertEqual(co_text.strip(), "ネストされたコールアウト本文")

    def test_html_extractor_callout_with_image(self):
        """HTMLエクスポート内のコールアウト内部にある画像が children_blocks として抽出されること"""
        import tempfile
        html_content = """<!DOCTYPE html>
<html>
<head><title>Test Callout Image</title></head>
<body>
<div class="page-body">
  <aside class="block-color-gray_background callout" style="white-space:pre-wrap;display:flex">
    <div style="font-size:1.5em"><span class="icon">💡</span></div>
    <div style="width:100%">
      <p>コールアウト上部テキスト</p>
      <figure class="image"><img src="sample.png"/></figure>
    </div>
  </aside>
</div>
</body>
</html>
"""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_zip = tmp.name
        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                zf.writestr("callout_img 4137be77284d804189c8d080073331b4.html", html_content)
                zf.writestr("sample.png", b"fake_png_data")

            extractor = HtmlZipExtractor(zip_path=tmp_zip)
            node = extractor.extract_tree()

            self.assertEqual(len(node.blocks), 1)
            co_block = node.blocks[0]
            self.assertEqual(co_block["type"], "callout")
            co_text = "".join(t["plain_text"] for t in co_block["callout"]["rich_text"])
            self.assertEqual(co_text.strip(), "コールアウト上部テキスト")

            self.assertEqual(len(co_block["children_blocks"]), 1)
            img_block = co_block["children_blocks"][0]
            self.assertEqual(img_block["type"], "image")
            self.assertEqual(img_block["_attachment_filename"], "sample.png")
            self.assertEqual(img_block["_attachment_bytes"], b"fake_png_data")
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)


if __name__ == "__main__":
    unittest.main()
