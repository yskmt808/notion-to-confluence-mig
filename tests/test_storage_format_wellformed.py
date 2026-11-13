"""converter が生成する Confluence Storage Format の XML 整形式検証テスト (TCK_00002)。

既存テストは assertIn による部分一致が中心で、閉じタグ漏れ・属性エスケープ不備・不正ネストを
検知できない。ここでは多様なブロック種別・特殊文字を含む入力を converter に通し、
出力が XML として整形式であることを保証する。
"""

import unittest
import xml.etree.ElementTree as ET

from storage_xml_helper import assert_valid_storage_xml, parse_storage_xml
from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode

# 特殊文字: XML 上で意味を持つ文字を全て含む
NASTY = '5 < 6 & 7 > 3 "quoted" \'single\' ]]> end'


def rt(content, **extra):
    item = {"type": "text", "text": {"content": content}, "plain_text": content, "annotations": {}}
    item.update(extra)
    return item


def block(b_type, payload=None, **extra):
    b = {"id": "aaaaaaaa-0000-0000-0000-000000000001", "type": b_type, b_type: payload or {}}
    b.update(extra)
    return b


class TestStorageXmlHelper(unittest.TestCase):
    """ヘルパー自体の振る舞い（正常系・異常系）。"""

    def test_accepts_namespaced_fragment(self):
        root = assert_valid_storage_xml(
            self, '<p><ac:link><ri:page ri:content-title="A" /></ac:link></p>'
        )
        self.assertEqual(root.tag, "root")
        self.assertEqual(len(root.findall("p")), 1)

    def test_accepts_html_named_entities(self):
        parse_storage_xml("<p>a&nbsp;b&copy;</p>")

    def test_accepts_cdata(self):
        parse_storage_xml(
            '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
            "<![CDATA[if a < b && c > d]]></ac:plain-text-body></ac:structured-macro>"
        )

    def test_rejects_unclosed_tag(self):
        with self.assertRaises(ET.ParseError):
            parse_storage_xml("<p>unclosed")

    def test_rejects_unescaped_ampersand(self):
        with self.assertRaises(ET.ParseError):
            parse_storage_xml("<p>a & b</p>")

    def test_rejects_bad_nesting(self):
        with self.assertRaises(ET.ParseError):
            parse_storage_xml("<p><b>x</p></b>")

    def test_failure_reports_label_and_excerpt(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_valid_storage_xml(self, "<p>ok</p><p>broken & here</p>", label="my-case")
        msg = str(ctx.exception)
        self.assertIn("my-case", msg)
        self.assertIn("not well-formed", msg)
        self.assertIn("broken", msg)


class TestConverterOutputWellFormed(unittest.TestCase):
    def setUp(self):
        self.conv = NotionToConfluenceConverter()

    def _check(self, blocks, label):
        out = self.conv.blocks_to_storage_format(blocks)
        assert_valid_storage_xml(self, out, label=label)
        return out

    def test_text_blocks_with_special_characters(self):
        for b_type in ("paragraph", "heading_1", "heading_2", "heading_3", "quote"):
            with self.subTest(b_type=b_type):
                self._check([block(b_type, {"rich_text": [rt(NASTY)]})], b_type)

    def test_rich_text_annotations_and_links(self):
        annotated = rt(
            NASTY,
            annotations={"bold": True, "italic": True, "strikethrough": True, "underline": True,
                         "code": True, "color": "red_background"},
        )
        linked = rt("link", text={"content": "link", "link": {"url": "https://example.com/?a=1&b=2"}})
        self._check([block("paragraph", {"rich_text": [annotated, linked]})], "annotated")

    def test_lists_and_todo_with_nesting(self):
        child = block("bulleted_list_item", {"rich_text": [rt("child & <nested>")]})
        blocks = [
            block("bulleted_list_item", {"rich_text": [rt(NASTY)]}, children_blocks=[child]),
            block("bulleted_list_item", {"rich_text": [rt("second")]}),
            block("numbered_list_item", {"rich_text": [rt("one")]}),
            block("numbered_list_item", {"rich_text": [rt("two")]}, children_blocks=[child]),
            block("to_do", {"rich_text": [rt(NASTY)], "checked": True}),
            block("to_do", {"rich_text": [rt("open")], "checked": False}),
        ]
        self._check(blocks, "lists")

    def test_code_blocks_including_cdata_terminator(self):
        blocks = [
            block("code", {"language": "python", "rich_text": [rt("if a < b and c > d: x = '<&>' ]]> y")]}),
            block("code", {"language": "mermaid", "rich_text": [rt("graph TD\n A[\"x<y\"] --> B & C")]}),
            block("code", {"language": 'we"ird<lang>', "rich_text": [rt("x")]}),
        ]
        self._check(blocks, "code")

    def test_callout_toggle_and_columns(self):
        inner = block("paragraph", {"rich_text": [rt(NASTY)]})
        col = {"type": "column", "children_blocks": [inner]}
        blocks = [
            block("callout", {"rich_text": [rt(NASTY)], "color": "blue_background",
                              "icon": {"type": "emoji", "emoji": "💡"}}, children_blocks=[inner]),
            block("toggle", {"rich_text": [rt('Toggle <title> & "q"')]}, children_blocks=[inner]),
            block("column_list", {}, children_blocks=[col, col]),
        ]
        self._check(blocks, "containers")

    def test_table_divider_toc_equation(self):
        row = {"type": "table_row", "table_row": {"cells": [[rt(NASTY)], [rt("b")]]}}
        blocks = [
            block("table", {"has_column_header": True}, children_blocks=[row, row]),
            block("divider"),
            block("table_of_contents"),
            block("equation", {"expression": "a < b & c"}),
        ]
        self._check(blocks, "table-misc")

    def test_media_blocks_with_special_filenames(self):
        fname = 'we ird&"<name>.png'
        blocks = [
            block("image", {"type": "file", "file": {"url": "x.png"}, "caption": [rt(NASTY)]},
                  _attachment_filename=fname),
            block("pdf", {"file": {"url": "x.pdf"}}, _attachment_filename='a&b.pdf', _attachment_uploaded=True),
            block("file", {"file": {"url": "x.zip"}}, _attachment_filename="a&b]]>.zip", _attachment_uploaded=True),
            block("file", {"file": {"url": "https://e.com/?a=1&b=2"}}, _attachment_filename="f.zip",
                  _attachment_uploaded=False),
            block("video", {"external": {"url": "https://youtube.com/watch?v=1&t=2"}, "caption": [rt(NASTY)]}),
            block("video", {"file": {"url": "v.mp4"}}, _attachment_filename="v&1.mp4", _attachment_uploaded=True),
            block("audio", {"external": {"url": "https://spotify.com/x?a=1&b=2"}}),
            block("audio", {"file": {"url": "a.mp3"}}, _attachment_filename="a&1.mp3", _attachment_uploaded=True),
        ]
        self._check(blocks, "media")

    def test_link_blocks_and_fallbacks(self):
        blocks = [
            block("bookmark", {"url": "https://e.com/?a=1&b=2", "caption": [rt(NASTY)]}),
            block("link_preview", {"url": "https://e.com/?a=1&b=2"}),
            block("embed", {"url": "https://e.com/?a=1&b=2"}),
            block("breadcrumb"),
            block("button", {}, children_blocks=[block("paragraph", {"rich_text": [rt(NASTY)]})]),
            block('weird<type>&', {}),
        ]
        self._check(blocks, "links-fallbacks")

    def test_synced_block_unwrapping(self):
        inner = block("callout", {"rich_text": [rt(NASTY)], "color": "blue_background"})
        outer = block("synced_block", {}, children_blocks=[block("synced_block", {}, children_blocks=[inner])])
        self._check([outer], "synced")

    def test_child_page_and_page_mentions(self):
        self.conv.migration_map = {
            "p1": {"confluence_id": "1", "title": 'Page <A> & "B"', "url": "https://e.com/p/1", "node_type": "page"}
        }
        mention = {
            "type": "mention",
            "mention": {"type": "page", "page": {"id": "p1"}},
            "plain_text": "@Page",
            "annotations": {},
        }
        self._check([block("paragraph", {"rich_text": [mention]})], "mention")
        self._check([block("child_page", {"title": 'Page <A> & "B"'}, id="p1")], "child_page")


class TestGeneratedPageContentWellFormed(unittest.TestCase):
    def test_generate_page_content_with_properties_and_blocks(self):
        conv = NotionToConfluenceConverter()
        node = PageNode(
            id="page-1",
            title='Title <x> & "y"',
            node_type="database_row",
            properties={
                "Status": {"type": "select", "value": NASTY},
                "Tags": {"type": "multi_select", "value": ["a&b", "<c>"]},
                "Note": {"type": "text", "value": NASTY},
            },
            blocks=[block("paragraph", {"rich_text": [rt(NASTY)]})],
        )
        out = conv.generate_page_content(node)
        assert_valid_storage_xml(self, out, label="generate_page_content")


if __name__ == "__main__":
    unittest.main()
