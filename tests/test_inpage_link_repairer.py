import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode
from src.importer import ConfluenceClient, ConfluenceImporter
from src.inpage_link_repairer import InPageLinkRepairer


class TestInPageLinkRepairer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.report_path = os.path.join(self.test_dir, "reports", "inpage_link_repair_candidates.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_candidate_detection_and_saving(self):
        """アンカーが欠落した自ページ内リンクが検出され、レポートファイルに出力されること"""
        conv = NotionToConfluenceConverter()
        page_node = PageNode(
            id="4137be77-284d-8041-89c8-d080073331b4",
            title="エラーパターン",
            node_type="page",
            blocks=[
                {
                    "id": "callout_block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": "熊本城",
                                    "link": {"url": "エラーパターン 4137be77284d804189c8d080073331b4.html"}
                                },
                                "plain_text": "熊本城",
                                "page_id": "4137be77284d804189c8d080073331b4"
                            }
                        ]
                    }
                }
            ]
        )
        conv.generate_page_content(page_node)
        self.assertEqual(len(conv.inpage_repair_candidates), 1)
        self.assertEqual(conv.inpage_repair_candidates[0]["link_text"], "熊本城")
        self.assertEqual(conv.inpage_repair_candidates[0]["block_id"], "callout_block")

        # Importer による保存と Confluence ID 補完の検証
        importer = ConfluenceImporter("https://mock", "user", "token", "TEST")
        importer.converter = conv
        importer.migration_map = {
            "4137be77-284d-8041-89c8-d080073331b4": {"confluence_id": "2260993", "title": "エラーパターン"}
        }

        saved_path = importer.save_inpage_repair_candidates(output_path=self.report_path)
        self.assertTrue(os.path.isfile(saved_path))

        with open(saved_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["confluence_page_id"], "2260993")
        self.assertEqual(data[0]["status"], "unresolved_in_page_link")

    def test_extract_anchor_target_from_block(self):
        """Notion APIのブロックJSONから対象リンクテキストの#以降のUUIDが正確に抽出されること"""
        mock_client = MagicMock(spec=ConfluenceClient)
        repairer = InPageLinkRepairer(mock_client, notion_token="secret_test", report_path=self.report_path)

        mock_block_json = {
            "object": "block",
            "id": "4157be77-284d-8025-80d3-eef079db6088",
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "熊本城",
                            "link": {
                                "url": "https://www.notion.so/workspace/Page-4137be77284d804189c8d080073331b4#4137be77284d80739dcaeedb54b028dd"
                            }
                        },
                        "plain_text": "熊本城"
                    }
                ]
            }
        }

        target = repairer.extract_anchor_target_from_block(mock_block_json, "熊本城")
        self.assertEqual(target, "4137be77284d80739dcaeedb54b028dd")

    def test_repair_pages_flow(self):
        """Notion APIでアンカーを解決し、Confluenceページのアンカーリンクと画像要素が更新されること"""
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        candidates = [
            {
                "page_id": "4137be77-284d-8041-89c8-d080073331b4",
                "page_title": "エラーパターン",
                "confluence_page_id": "2260993",
                "block_id": "callout_block_id",
                "link_text": "熊本城",
                "href": "エラーパターン 4137be77284d804189c8d080073331b4.html",
                "status": "unresolved_in_page_link"
            }
        ]
        with open(self.report_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)

        # ターゲットページノード（リンク元コールアウト ＋ 飛び先画像ブロック）
        page_node = PageNode(
            id="4137be77-284d-8041-89c8-d080073331b4",
            title="エラーパターン",
            node_type="page",
            blocks=[
                {
                    "id": "callout_block_id",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "熊本城", "link": {"url": "エラーパターン 4137be77284d804189c8d080073331b4.html"}},
                                "plain_text": "熊本城",
                                "page_id": "4137be77-284d-8041-89c8-d080073331b4"
                            }
                        ]
                    }
                },
                {
                    "id": "4137be77-284d-8073-9dca-eedb54b028dd",
                    "type": "image",
                    "image": {
                        "type": "file",
                        "file": {"url": "kumamoto.png"},
                        "caption": []
                    },
                    "_attachment_filename": "kumamoto.png"
                }
            ]
        )

        mock_client = MagicMock(spec=ConfluenceClient)
        mock_client.space_key = "TEST"
        mock_client.get_page_by_id.return_value = {
            "id": "2260993",
            "title": "エラーパターン",
            "version": {"number": 1}
        }

        mock_notion_block = {
            "id": "callout_block_id",
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "熊本城",
                            "link": {
                                "url": "https://www.notion.so/Page#4137be77284d80739dcaeedb54b028dd"
                            }
                        },
                        "plain_text": "熊本城"
                    }
                ]
            }
        }

        conv = NotionToConfluenceConverter()
        repairer = InPageLinkRepairer(mock_client, notion_token="secret_test", report_path=self.report_path)

        with patch.object(repairer, "fetch_notion_block", return_value=mock_notion_block):
            result = repairer.repair_pages(root_node=page_node, converter=conv)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["repaired_count"], 1)

        # Confluence update_page が呼び出されたことの検証
        mock_client.update_page.assert_called_once()
        call_kwargs = mock_client.update_page.call_args[1]
        updated_body = call_kwargs["body"]

        # リンク側に ac:anchor="4137be77284d80739dcaeedb54b028dd" が設定されていること
        self.assertIn('ac:anchor="4137be77284d80739dcaeedb54b028dd"', updated_body)
        self.assertIn('<ac:link-body>熊本城</ac:link-body>', updated_body)

        # 画像要素側にアンカーマクロが設定されていること
        self.assertIn('<ac:structured-macro ac:name="anchor"><ac:parameter ac:name="">4137be77284d80739dcaeedb54b028dd</ac:parameter></ac:structured-macro>', updated_body)

        # レポートファイルのステータスが "repaired" に更新されていること
        with open(self.report_path, "r", encoding="utf-8") as f:
            updated_report = json.load(f)
        self.assertEqual(updated_report[0]["status"], "repaired")
        self.assertEqual(updated_report[0]["resolved_anchor"], "4137be77284d80739dcaeedb54b028dd")


if __name__ == "__main__":
    unittest.main()
