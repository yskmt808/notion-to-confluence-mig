"""
Unit tests for Task B.8 (Mermaid diagram rendering & macro) and Task B.9 (Incoming links backlinks).
"""

import unittest
from src.extractor import PageNode
from src.converter import NotionToConfluenceConverter
from src.importer import ConfluenceImporter
from storage_xml_helper import assert_valid_storage_xml


class TestMermaidConverter(unittest.TestCase):
    def setUp(self):
        self.converter = NotionToConfluenceConverter()

    def test_mermaid_detection(self):
        # Explicit language
        is_m, code = NotionToConfluenceConverter.is_mermaid_code("mermaid", "graph TD\n  A-->B")
        self.assertTrue(is_m)
        self.assertEqual(code, "graph TD\n  A-->B")

        # Auto-detection from keyword
        is_m2, _ = NotionToConfluenceConverter.is_mermaid_code("", "sequenceDiagram\n  Alice->>Bob: Hello")
        self.assertTrue(is_m2)

        is_m3, _ = NotionToConfluenceConverter.is_mermaid_code("text", "flowchart LR\n  X --> Y")
        self.assertTrue(is_m3)

        # Non-mermaid code
        is_m4, _ = NotionToConfluenceConverter.is_mermaid_code("python", "def hello(): pass")
        self.assertFalse(is_m4)

    def test_mermaid_block_conversion(self):
        block = {
            "id": "11112222-3333-4444-5555-666677778888",
            "type": "code",
            "code": {
                "language": "mermaid",
                "rich_text": [{"type": "text", "plain_text": "graph TD\n  A-->B"}],
            },
        }
        res = self.converter.blocks_to_storage_format([block])
        assert_valid_storage_xml(self, res)
        # 方式②: Confluenceネイティブ codeマクロ (language: mermaid) の生成検証
        self.assertIn('<ac:structured-macro ac:name="code">', res)
        self.assertIn('<ac:parameter ac:name="language">mermaid</ac:parameter>', res)
        self.assertIn('<ac:parameter ac:name="title">📊 Mermaid ダイアグラム</ac:parameter>', res)
        self.assertIn("graph TD\n  A-->B", res)


class TestIncomingLinksBacklinks(unittest.TestCase):
    def setUp(self):
        self.converter = NotionToConfluenceConverter()

    def test_extract_referenced_node_ids(self):
        importer = ConfluenceImporter(
            url="https://mock.atlassian.net",
            username="test@example.com",
            token="dummy",
            space_key="TEST",
        )
        target_uuid = "12345678-1234-5678-9abc-def012345678"
        blocks = [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": "Link to target",
                                "link": {"url": f"https://notion.so/{target_uuid}"},
                            },
                        }
                    ]
                },
            },
            {
                "type": "link_to_page",
                "link_to_page": {
                    "page_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                },
            },
        ]
        refs = importer._extract_referenced_node_ids(blocks)
        self.assertIn("12345678123456789abcdef012345678", refs)
        self.assertIn("aaaaaaaabbbbccccddddeeeeeeeeeeee", refs)

    def test_build_incoming_links_map(self):
        importer = ConfluenceImporter(
            url="https://mock.atlassian.net",
            username="test@example.com",
            token="dummy",
            space_key="TEST",
        )
        node_b_id = "bbbbbbbb-1111-2222-3333-444444444444"
        node_b = PageNode(id=node_b_id, title="Target Page B", node_type="page", blocks=[])

        # Page A links to Page B
        node_a = PageNode(
            id="aaaaaaaa-1111-2222-3333-444444444444",
            title="Source Page A",
            node_type="page",
            blocks=[
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": "Check Page B",
                                    "link": {"url": f"https://notion.so/{node_b_id}"},
                                },
                            }
                        ]
                    },
                }
            ],
        )

        root = PageNode(id="root-node-id", title="Root", node_type="page", children=[node_a, node_b])
        importer.migration_map = {
            node_a.id: {"confluence_id": "1001", "url": "https://mock.atlassian.net/wiki/pages/1001"},
            node_b.id: {"confluence_id": "1002", "url": "https://mock.atlassian.net/wiki/pages/1002"},
        }

        inc_map = importer._build_incoming_links_map(root)
        self.assertIn(node_b_id, inc_map)
        self.assertEqual(len(inc_map[node_b_id]), 1)
        self.assertEqual(inc_map[node_b_id][0]["title"], "Source Page A")

    def test_generate_page_content_with_incoming_links(self):
        node = PageNode(
            id="test-node-id",
            title="Target Page",
            node_type="page",
            blocks=[
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": "Page body content."}}],
                    },
                }
            ],
        )
        incoming = [
            {"id": "source-1", "title": "Source Page 1", "url": "https://example.com/p1"},
            {"id": "source-2", "title": "Source Page 2", "url": None},
        ]
        html_out = self.converter.generate_page_content(node, incoming_links=incoming)
        assert_valid_storage_xml(self, html_out)

        # Check body text
        self.assertIn("Page body content.", html_out)
        # Check incoming links expand container
        self.assertIn("🔗 このページへのリンク元 (受信リンク: 2件)", html_out)
        self.assertIn("Source Page 1", html_out)
        self.assertIn("Source Page 2", html_out)
        self.assertIn('<ri:page ri:content-title="Source Page 2"', html_out)


if __name__ == "__main__":
    unittest.main()
