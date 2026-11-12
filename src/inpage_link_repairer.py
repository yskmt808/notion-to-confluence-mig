import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import requests

from src.extractor import PageNode
from src.importer import ConfluenceClient

logger = logging.getLogger(__name__)


class InPageLinkRepairer:
    """Notion API を用いて、エクスポート時に #フラグメントが欠落した自ページ内リンクを正確に特定・修復するクラス。"""

    def __init__(
        self,
        confluence_client: ConfluenceClient,
        notion_token: str,
        report_path: str = "reports/inpage_link_repair_candidates.json",
    ):
        self.client = confluence_client
        self.notion_token = notion_token.strip()
        self.report_path = report_path
        self.headers = {
            "Authorization": f"Bearer {self.notion_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def fetch_notion_block(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Notion API から単一ブロックの詳細情報を取得する。"""
        if not block_id:
            return None
        norm_id = block_id.replace("-", "")
        formatted_id = (
            f"{norm_id[:8]}-{norm_id[8:12]}-{norm_id[12:16]}-{norm_id[16:20]}-{norm_id[20:]}"
            if len(norm_id) == 32
            else block_id
        )
        url = f"https://api.notion.com/v1/blocks/{formatted_id}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                resp2 = requests.get(f"https://api.notion.com/v1/blocks/{norm_id}", headers=self.headers, timeout=15)
                if resp2.status_code == 200:
                    return resp2.json()
            logger.warning(f"Notion API ブロック取得失敗 ({formatted_id}): HTTP {resp.status_code} - {resp.text[:100]}")
            return None
        except Exception as e:
            logger.warning(f"Notion API 呼び出し例外 ({formatted_id}): {e}")
            return None

    def fetch_block_children(self, block_id: str) -> List[Dict[str, Any]]:
        """Notion API から子ブロック一覧を取得する。"""
        if not block_id:
            return []
        norm_id = block_id.replace("-", "")
        formatted_id = (
            f"{norm_id[:8]}-{norm_id[8:12]}-{norm_id[12:16]}-{norm_id[16:20]}-{norm_id[20:]}"
            if len(norm_id) == 32
            else block_id
        )
        url = f"https://api.notion.com/v1/blocks/{formatted_id}/children"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("results", [])
            return []
        except Exception as e:
            logger.warning(f"Notion API 子ブロック取得例外 ({formatted_id}): {e}")
            return []

    def extract_anchor_target_from_block(self, block_data: Dict[str, Any], link_text: str) -> Optional[str]:
        """ブロックデータ内の rich_text から link_text に一致するリンクの飛び先ブロックID（#以降）を抽出する。"""
        b_type = block_data.get("type", "")
        data_dict = block_data.get(b_type, {}) if isinstance(block_data.get(b_type), dict) else {}
        rich_texts = data_dict.get("rich_text", [])

        target = self._find_anchor_in_rich_texts(rich_texts, link_text)
        if target:
            return target

        if block_data.get("has_children"):
            children = self.fetch_block_children(block_data.get("id", ""))
            for child in children:
                c_target = self.extract_anchor_target_from_block(child, link_text)
                if c_target:
                    return c_target

        return None

    def _find_anchor_in_rich_texts(self, rich_texts: List[Dict[str, Any]], link_text: str) -> Optional[str]:
        clean_target_text = link_text.strip()
        for item in rich_texts:
            raw_text = (item.get("plain_text") or (item.get("text") or {}).get("content", "")).strip()
            clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
            if clean_text == clean_target_text:
                url = ((item.get("text") or {}).get("link") or {}).get("url") or item.get("href")
                if url and "#" in url:
                    anchor = urllib.parse.unquote(url.split("#", 1)[1].strip())
                    if anchor:
                        return anchor.replace("-", "").lower()
        return None

    def repair_pages(self, root_node: Optional[PageNode], converter: Any) -> Dict[str, Any]:
        """reports/inpage_link_repair_candidates.json を読み込み、Notion API で解決したアンカーで Confluence ページを更新する。"""
        if not os.path.isfile(self.report_path):
            return {"status": "SKIPPED", "reason": f"レポートファイル '{self.report_path}' が存在しません。"}

        with open(self.report_path, "r", encoding="utf-8") as f:
            candidates = json.load(f)

        if not candidates:
            return {"status": "SKIPPED", "reason": "修復対象の候補が0件です。"}

        logger.info(f"🚀 Notion API によるページ内リンク修復開始: 対象 {len(candidates)} 件")
        print(f"\n🚀 Notion API によるページ内リンク修復開始: 対象 {len(candidates)} 件")

        by_page: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            p_id = c.get("page_id", "")
            by_page.setdefault(p_id, []).append(c)

        node_map: Dict[str, PageNode] = {}
        if root_node:
            def _map_nodes(n: PageNode):
                node_map[n.id] = n
                norm = n.id.replace("-", "").lower()
                node_map[norm] = n
                for ch in n.children:
                    _map_nodes(ch)
            _map_nodes(root_node)

        repaired_count = 0
        failed_count = 0

        for p_id, page_candidates in by_page.items():
            conf_id = page_candidates[0].get("confluence_page_id")
            page_title = page_candidates[0].get("page_title", p_id)
            node = node_map.get(p_id) or node_map.get(p_id.replace("-", "").lower())

            resolved_anchors_for_page: Dict[str, str] = {}
            for cand in page_candidates:
                block_id = cand.get("block_id")
                link_text = cand.get("link_text")
                if not block_id:
                    cand["status"] = "failed_no_block_id"
                    failed_count += 1
                    continue

                b_data = self.fetch_notion_block(block_id)
                if not b_data:
                    cand["status"] = "failed_notion_api_fetch"
                    failed_count += 1
                    continue

                target_anchor = self.extract_anchor_target_from_block(b_data, link_text)
                if target_anchor:
                    cand["status"] = "repaired"
                    cand["resolved_anchor"] = target_anchor
                    resolved_anchors_for_page[link_text] = target_anchor
                    repaired_count += 1
                    logger.info(f"  ✓ 解決: [{page_title}] '{link_text}' -> アンカー '{target_anchor}'")
                    print(f"  ✓ 解決: [{page_title}] '{link_text}' -> アンカー '{target_anchor}'")
                else:
                    cand["status"] = "failed_anchor_not_found_in_block"
                    failed_count += 1
                    logger.warning(f"  ⚠️ 未解決: [{page_title}] '{link_text}' (Notion APIにアンカーURLが見つかりませんでした)")

            # ページを再生成して Confluence を更新
            if resolved_anchors_for_page and conf_id and node:
                for lt, anc in resolved_anchors_for_page.items():
                    converter.explicit_link_anchors[(p_id, lt)] = anc
                    converter.referenced_anchors.add(anc)

                latest = self.client.get_page_by_id(conf_id, expand="version")
                if latest:
                    cur_ver = latest.get("version", {}).get("number", 1)
                    new_body = converter.generate_page_content(node)
                    self.client.update_page(
                        page_id=conf_id,
                        space_key=self.client.space_key or "",
                        title=latest.get("title") or page_title,
                        body=new_body,
                        current_version=cur_ver,
                    )
                    logger.info(f"  📝 Confluence ページ更新完了: [{page_title}] (ID: {conf_id}, v{cur_ver + 1})")
                    print(f"  📝 Confluence ページ更新完了: [{page_title}] (ID: {conf_id}, v{cur_ver + 1})")

        with open(self.report_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)

        summary = {
            "status": "COMPLETED",
            "total_candidates": len(candidates),
            "repaired_count": repaired_count,
            "failed_count": failed_count,
            "report_path": self.report_path,
        }
        logger.info(f"🎉 修復処理完了: 修復成功 {repaired_count} 件 / 失敗 {failed_count} 件")
        print(f"\n🎉 ページ内リンク修復完了: 成功 {repaired_count} 件 / 失敗 {failed_count} 件\n")
        return summary
