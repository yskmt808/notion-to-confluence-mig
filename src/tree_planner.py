"""移行計画作成用のページツリー調査・CSV/Markdownサマリー出力モジュール。"""

import csv
import html as _html
import json
import logging
import os
import random
import re
import time
import unicodedata
import urllib.parse
import zipfile
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from src.extractor import PageNode, normalize_notion_id
from src.html_extractor import HtmlZipExtractor, MultiZipExtractor, extract_uuids

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"


def _format_uuid(raw_id: str) -> str:
    norm = normalize_notion_id(raw_id)
    if len(norm) == 32:
        return f"{norm[:8]}-{norm[8:12]}-{norm[12:16]}-{norm[16:20]}-{norm[20:]}"
    return raw_id


@dataclass
class PlanTreeNode:
    node_id: str
    parent_id: str
    node_type: str  # "page" | "database" | "database_row"
    title: str
    notion_url: str = ""
    last_edited_time: str = ""
    archived: bool = False
    db_row_count: Optional[int] = None
    children: List["PlanTreeNode"] = field(default_factory=list)


class _RateLimiter:
    """Notion API レートリミット回避用のシンプルなトークンバケット/最小間隔スロットラ。"""

    def __init__(self, requests_per_sec: float = 2.8):
        self.min_interval = 1.0 / max(0.1, float(requests_per_sec))
        self._last_ts = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_ts = time.monotonic()


class ApiTreeScanner:
    """POST /v1/search を中心に最小API呼び出しでNotionワークスペースのページツリーを構築する。"""

    def __init__(
        self,
        token: str,
        rate_limit: float = 2.8,
        max_retries: int = 5,
        cache_file: Optional[str] = None,
        resume: bool = False,
    ):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        self.limiter = _RateLimiter(requests_per_sec=rate_limit)
        self.max_retries = max_retries
        self.cache_file = cache_file
        self.resume = resume
        self.api_calls = 0
        self._block_parent_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.last_search_stats: Dict[str, Any] = {
            "page": 0,
            "database": 0,
            "database_row": 0,
            "latest_title": "",
            "has_more": False,
        }

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(self.max_retries):
            self.limiter.wait()
            self.api_calls += 1
            try:
                res = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt >= self.max_retries - 1:
                    raise
                backoff = (2 ** attempt) + random.uniform(0.2, 0.8)
                logger.warning(f"Notion API network error ({e}), retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue

            if res.status_code == 429:
                retry_after = res.headers.get("Retry-After")
                try:
                    wait_sec = float(retry_after) if retry_after is not None else (2 ** attempt) + 1.0
                except ValueError:
                    wait_sec = (2 ** attempt) + 1.0
                wait_sec += random.uniform(0.1, 0.5)
                logger.warning(f"HTTP 429 rate limited. Waiting {wait_sec:.1f}s before retry...")
                time.sleep(wait_sec)
                continue

            if res.status_code in (500, 502, 503, 504) and attempt < self.max_retries - 1:
                backoff = (2 ** attempt) + random.uniform(0.2, 0.8)
                logger.warning(f"Notion API HTTP {res.status_code}, retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue

            return res
        return res

    @staticmethod
    def _extract_title(obj: Dict[str, Any]) -> str:
        obj_type = obj.get("object", "")
        if obj_type == "database":
            title_arr = obj.get("title") or []
            t = "".join(item.get("plain_text", "") for item in title_arr if isinstance(item, dict)).strip()
            return t or "Untitled Database"
        props = obj.get("properties") or {}
        if isinstance(props, dict):
            for p_val in props.values():
                if isinstance(p_val, dict) and p_val.get("type") == "title":
                    t_arr = p_val.get("title") or []
                    t = "".join(item.get("plain_text", "") for item in t_arr if isinstance(item, dict)).strip()
                    if t:
                        return t
        return "Untitled"

    def _load_cache(self) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        if not self.cache_file or not self.resume or not os.path.exists(self.cache_file):
            return [], None, False
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            bp = data.get("block_parents")
            if isinstance(bp, dict):
                self._block_parent_cache.update(bp)
            return data.get("results", []), data.get("next_cursor"), bool(data.get("search_completed", False))
        except Exception as e:
            logger.warning(f"Failed to load search cache {self.cache_file}: {e}")
            return [], None, False

    def _atomic_write_json(self, path: str, payload: Dict[str, Any]) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        for attempt in range(6):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as e:
                if attempt < 5:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                logger.warning(f"Failed to replace cache file {path} ({e}); falling back to direct write.")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to write cache file {path}: {e}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _save_cache(self, results: List[Dict[str, Any]], next_cursor: Optional[str], completed: bool) -> None:
        if not self.cache_file:
            return
        self._atomic_write_json(
            self.cache_file,
            {
                "results": results,
                "next_cursor": next_cursor,
                "search_completed": completed,
                "block_parents": self._block_parent_cache,
            },
        )

    def _resolve_block_parent_page(
        self,
        block_id: str,
        known_ids: Set[str],
        offline_only: bool = False,
    ) -> Optional[str]:
        """親が block_id（トグル・カラム等）の場合のみ GET /v1/blocks/{id} を辿って親ページ/DBを解決する。"""
        visited: Set[str] = set()
        curr_id = _format_uuid(block_id)
        while curr_id:
            norm = normalize_notion_id(curr_id)
            if norm in visited:
                return None
            visited.add(norm)
            if norm in known_ids:
                return norm
            if norm in self._block_parent_cache:
                parent_obj = self._block_parent_cache[norm]
            elif offline_only:
                return None
            else:
                res = self._request_with_retry("GET", f"https://api.notion.com/v1/blocks/{curr_id}")
                if not res.ok:
                    self._block_parent_cache[norm] = None
                    return None
                parent_obj = res.json().get("parent")
                self._block_parent_cache[norm] = parent_obj
            if not isinstance(parent_obj, dict):
                return None
            p_type = parent_obj.get("type")
            if p_type in ("page_id", "database_id"):
                return normalize_notion_id(str(parent_obj.get(p_type) or ""))
            if p_type == "block_id":
                curr_id = _format_uuid(str(parent_obj.get("block_id") or ""))
                continue
            return None
        return None

    def scan(
        self,
        root_page_id: Optional[str] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        parent_progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[PlanTreeNode]:
        """POST /v1/search で全ページ/DBを一括取得し、ツリー構造を構築してルートリストを返す。"""
        all_items, next_cursor, search_completed = self._load_cache()
        seen_ids = {normalize_notion_id(str(it.get("id") or "")) for it in all_items if it.get("id")}

        def _update_search_stats(items: List[Dict[str, Any]], has_more_flag: bool) -> None:
            p_cnt = 0
            d_cnt = 0
            r_cnt = 0
            for it in items:
                okind = it.get("object", "page")
                pt = (it.get("parent") or {}).get("type", "")
                if okind == "database":
                    d_cnt += 1
                elif pt in ("database_id", "data_source_id"):
                    r_cnt += 1
                else:
                    p_cnt += 1
            self.last_search_stats = {
                "page": p_cnt,
                "database": d_cnt,
                "database_row": r_cnt,
                "latest_title": self._extract_title(items[-1]) if items else "",
                "has_more": has_more_flag,
            }

        if all_items:
            _update_search_stats(all_items, not search_completed)

        if not search_completed:
            while True:
                payload: Dict[str, Any] = {"page_size": 100}
                if next_cursor:
                    payload["start_cursor"] = next_cursor
                res = self._request_with_retry("POST", "https://api.notion.com/v1/search", json=payload)
                if not res.ok:
                    raise RuntimeError(f"Notion search API failed: HTTP {res.status_code} - {res.text}")
                data = res.json()
                for item in data.get("results") or []:
                    nid = normalize_notion_id(str(item.get("id") or ""))
                    if nid and nid not in seen_ids:
                        seen_ids.add(nid)
                        all_items.append(item)
                has_more = bool(data.get("has_more"))
                next_cursor = data.get("next_cursor")
                _update_search_stats(all_items, has_more)
                if progress_cb:
                    progress_cb(len(all_items))
                self._save_cache(all_items, next_cursor, not has_more)
                if not has_more or not next_cursor:
                    break

        return self._build_trees_from_items(
            all_items=all_items,
            root_page_id=root_page_id,
            offline_only=False,
            parent_progress_cb=parent_progress_cb,
            save_after_parent_resolve=(all_items, next_cursor),
        )

    def _build_trees_from_items(
        self,
        all_items: List[Dict[str, Any]],
        root_page_id: Optional[str] = None,
        offline_only: bool = False,
        parent_progress_cb: Optional[Callable[[int, int], None]] = None,
        save_after_parent_resolve: Optional[Tuple[List[Dict[str, Any]], Optional[str]]] = None,
    ) -> List[PlanTreeNode]:
        nodes_by_norm: Dict[str, PlanTreeNode] = {}
        raw_parent_by_norm: Dict[str, Dict[str, Any]] = {}
        for item in all_items:
            raw_id = str(item.get("id") or "")
            norm = normalize_notion_id(raw_id)
            if not norm:
                continue
            obj_kind = item.get("object", "page")
            parent_obj = item.get("parent") or {}
            p_type = parent_obj.get("type", "")
            if obj_kind == "database":
                n_type = "database"
            elif p_type in ("database_id", "data_source_id"):
                n_type = "database_row"
            else:
                n_type = "page"

            nodes_by_norm[norm] = PlanTreeNode(
                node_id=_format_uuid(raw_id),
                parent_id="",
                node_type=n_type,
                title=self._extract_title(item),
                notion_url=str(item.get("url") or f"https://www.notion.so/{norm}"),
                last_edited_time=str(item.get("last_edited_time") or ""),
                archived=bool(item.get("archived") or item.get("in_trash")),
            )
            raw_parent_by_norm[norm] = parent_obj

        known_ids = set(nodes_by_norm.keys())
        block_parent_targets = [
            norm for norm, po in raw_parent_by_norm.items()
            if po.get("type") == "block_id" and po.get("block_id")
        ]
        block_parent_total = len(block_parent_targets)
        block_parent_done = 0
        before_bp_cnt = len(self._block_parent_cache)

        roots: List[PlanTreeNode] = []
        for norm, node in nodes_by_norm.items():
            parent_obj = raw_parent_by_norm.get(norm) or {}
            p_type = parent_obj.get("type", "")
            parent_norm: Optional[str] = None
            if p_type == "page_id":
                parent_norm = normalize_notion_id(str(parent_obj.get("page_id") or ""))
            elif p_type in ("database_id", "data_source_id"):
                parent_norm = normalize_notion_id(str(parent_obj.get(p_type) or parent_obj.get("database_id") or ""))
            elif p_type == "block_id":
                blk_id = str(parent_obj.get("block_id") or "")
                if blk_id:
                    parent_norm = self._resolve_block_parent_page(blk_id, known_ids, offline_only=offline_only)
                block_parent_done += 1
                if parent_progress_cb and block_parent_total:
                    parent_progress_cb(block_parent_done, block_parent_total)

            if parent_norm and parent_norm in nodes_by_norm and parent_norm != norm:
                parent_node = nodes_by_norm[parent_norm]
                node.parent_id = parent_node.node_id
                parent_node.children.append(node)
            else:
                roots.append(node)

        if save_after_parent_resolve and len(self._block_parent_cache) > before_bp_cnt:
            items_ref, cursor_ref = save_after_parent_resolve
            self._save_cache(items_ref, cursor_ref, True)

        for node in nodes_by_norm.values():
            if node.node_type == "database":
                node.db_row_count = sum(1 for c in node.children if c.node_type == "database_row")

        if root_page_id:
            target_norm = normalize_notion_id(root_page_id)
            target_node = nodes_by_norm.get(target_norm)
            if not target_node:
                raise ValueError(f"Root page/database ID '{root_page_id}' not found in Notion search results.")
            target_node.parent_id = ""
            return [target_node]

        return roots

    @classmethod
    def build_trees_from_cache(
        cls,
        cache_file: str,
        root_page_id: Optional[str] = None,
    ) -> List[PlanTreeNode]:
        """保存済みの api_search_cache.json から API 呼び出し 0 回でツリー（または特定サブツリー）を再構築する。"""
        if not os.path.exists(cache_file):
            raise FileNotFoundError(f"Cache file not found: {cache_file}")
        scanner = cls(token="", cache_file=cache_file, resume=True)
        all_items, _, _ = scanner._load_cache()
        if not all_items:
            raise ValueError(f"No search results found in cache file: {cache_file}")
        return scanner._build_trees_from_items(
            all_items=all_items,
            root_page_id=root_page_id,
            offline_only=True,
        )

    def _load_subtree_cache(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.cache_file or not self.resume or not os.path.exists(self.cache_file):
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("subtree_blocks") or {}
        except Exception as e:
            logger.warning(f"Failed to load subtree cache {self.cache_file}: {e}")
            return {}

    def _save_subtree_cache(self, subtree_blocks: Dict[str, List[Dict[str, Any]]]) -> None:
        if not self.cache_file:
            return
        self._atomic_write_json(self.cache_file, {"subtree_blocks": subtree_blocks})

    @staticmethod
    def _compact_block_for_cache(blk: Dict[str, Any]) -> Dict[str, Any]:
        b_type = blk.get("type", "")
        compact: Dict[str, Any] = {
            "id": blk.get("id"),
            "type": b_type,
            "has_children": bool(blk.get("has_children")),
            "last_edited_time": blk.get("last_edited_time") or "",
            "archived": bool(blk.get("archived")),
            "in_trash": bool(blk.get("in_trash")),
        }
        if b_type in ("child_page", "child_database") and isinstance(blk.get(b_type), dict):
            compact[b_type] = {"title": blk[b_type].get("title", "")}
        return compact

    def _fetch_block_children_all(
        self,
        block_id: str,
        subtree_cache: Dict[str, List[Dict[str, Any]]],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        norm_id = normalize_notion_id(block_id)
        if norm_id in subtree_cache:
            return subtree_cache[norm_id], False
        formatted = _format_uuid(block_id)
        results: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if next_cursor:
                params["start_cursor"] = next_cursor
            res = self._request_with_retry(
                "GET",
                f"https://api.notion.com/v1/blocks/{formatted}/children",
                params=params,
            )
            if not res.ok:
                logger.warning(f"Failed to fetch block children for {formatted}: HTTP {res.status_code}")
                break
            data = res.json()
            for item in data.get("results") or []:
                if isinstance(item, dict):
                    results.append(self._compact_block_for_cache(item))
            if not data.get("has_more") or not data.get("next_cursor"):
                break
            next_cursor = data.get("next_cursor")
        subtree_cache[norm_id] = results
        return results, True

    _SUBTREE_CONTAINER_BLOCK_TYPES = frozenset({
        "column_list",
        "column",
        "toggle",
        "callout",
        "synced_block",
        "template",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
    })

    def scan_subtree(
        self,
        root_page_id: str,
        progress_cb: Optional[Callable[[int, int, int], None]] = None,
        exclude_ids: Optional[Set[str]] = None,
    ) -> List[PlanTreeNode]:
        """--root-page-id を起点に blocks/{id}/children のみでサブツリーを直接探索する（DB行には入らない高速モード）。"""
        if not root_page_id:
            raise ValueError("scan_subtree requires root_page_id.")
        ex_ids = {normalize_notion_id(x) for x in (exclude_ids or set()) if normalize_notion_id(x)}
        formatted_root = _format_uuid(root_page_id)
        root_norm = normalize_notion_id(root_page_id)
        if root_norm in ex_ids:
            return []

        res = self._request_with_retry("GET", f"https://api.notion.com/v1/pages/{formatted_root}")
        if res.ok:
            root_obj = res.json()
            root_node = PlanTreeNode(
                node_id=_format_uuid(str(root_obj.get("id") or formatted_root)),
                parent_id="",
                node_type="page",
                title=self._extract_title(root_obj),
                notion_url=str(root_obj.get("url") or f"https://www.notion.so/{root_norm}"),
                last_edited_time=str(root_obj.get("last_edited_time") or ""),
                archived=bool(root_obj.get("archived") or root_obj.get("in_trash")),
            )
        else:
            res_db = self._request_with_retry("GET", f"https://api.notion.com/v1/databases/{formatted_root}")
            if not res_db.ok:
                raise RuntimeError(
                    f"Root page/database '{root_page_id}' could not be retrieved: HTTP {res.status_code} / {res_db.status_code}"
                )
            db_obj = res_db.json()
            return [
                PlanTreeNode(
                    node_id=_format_uuid(str(db_obj.get("id") or formatted_root)),
                    parent_id="",
                    node_type="database",
                    title=self._extract_title(db_obj),
                    notion_url=str(db_obj.get("url") or f"https://www.notion.so/{root_norm}"),
                    last_edited_time=str(db_obj.get("last_edited_time") or ""),
                    archived=bool(db_obj.get("archived") or db_obj.get("in_trash")),
                    db_row_count=None,
                )
            ]

        subtree_cache = self._load_subtree_cache()
        visited_containers: Set[str] = set()
        queue: deque = deque([(root_node, root_norm)])
        page_count = 1
        db_count = 0
        dirty_count = 0
        last_save_ts = time.monotonic()

        try:
            while queue:
                parent_page_node, container_norm = queue.popleft()
                if container_norm in visited_containers:
                    continue
                visited_containers.add(container_norm)

                children_blocks, fetched_new = self._fetch_block_children_all(container_norm, subtree_cache)
                if fetched_new:
                    dirty_count += 1
                    now_ts = time.monotonic()
                    if dirty_count >= 25 or (now_ts - last_save_ts) >= 5.0:
                        self._save_subtree_cache(subtree_cache)
                        dirty_count = 0
                        last_save_ts = now_ts

                for blk in children_blocks:
                    if not isinstance(blk, dict):
                        continue
                    b_type = blk.get("type", "")
                    b_id = str(blk.get("id") or "")
                    b_norm = normalize_notion_id(b_id)
                    if not b_norm:
                        continue

                    if b_type == "child_page":
                        if b_norm in ex_ids:
                            continue
                        title = str((blk.get("child_page") or {}).get("title") or "").strip() or "Untitled"
                        child_node = PlanTreeNode(
                            node_id=_format_uuid(b_id),
                            parent_id=parent_page_node.node_id,
                            node_type="page",
                            title=title,
                            notion_url=f"https://www.notion.so/{b_norm}",
                            last_edited_time=str(blk.get("last_edited_time") or ""),
                            archived=bool(blk.get("archived") or blk.get("in_trash")),
                        )
                        parent_page_node.children.append(child_node)
                        page_count += 1
                        queue.append((child_node, b_norm))
                    elif b_type == "child_database":
                        if b_norm in ex_ids:
                            continue
                        title = str((blk.get("child_database") or {}).get("title") or "").strip() or "Untitled Database"
                        db_node = PlanTreeNode(
                            node_id=_format_uuid(b_id),
                            parent_id=parent_page_node.node_id,
                            node_type="database",
                            title=title,
                            notion_url=f"https://www.notion.so/{b_norm}",
                            last_edited_time=str(blk.get("last_edited_time") or ""),
                            archived=bool(blk.get("archived") or blk.get("in_trash")),
                            db_row_count=None,
                        )
                        parent_page_node.children.append(db_node)
                        db_count += 1
                    elif blk.get("has_children") and b_type in self._SUBTREE_CONTAINER_BLOCK_TYPES:
                        queue.append((parent_page_node, b_norm))

                if progress_cb:
                    progress_cb(page_count, db_count, len(queue))
        finally:
            if dirty_count > 0:
                self._save_subtree_cache(subtree_cache)

        return [root_node]


def _page_node_to_plan_node(pnode: PageNode, parent_id: str = "") -> PlanTreeNode:
    norm = normalize_notion_id(getattr(pnode, "id", "") or "")
    formatted_id = _format_uuid(pnode.id) if len(norm) == 32 else str(pnode.id)
    notion_url = f"https://www.notion.so/{norm}" if len(norm) == 32 else ""
    children = [_page_node_to_plan_node(c, parent_id=formatted_id) for c in (getattr(pnode, "children", None) or [])]
    db_row_count = None
    if pnode.node_type == "database":
        db_row_count = sum(1 for c in children if c.node_type == "database_row")
    return PlanTreeNode(
        node_id=formatted_id,
        parent_id=parent_id,
        node_type=pnode.node_type,
        title=pnode.title or "Untitled",
        notion_url=notion_url,
        last_edited_time=str(getattr(pnode, "last_edited_time", "") or ""),
        archived=False,
        db_row_count=db_row_count,
        children=children,
    )


def count_zip_source_files(zip_paths: List[str]) -> Tuple[int, int]:
    """ZIP内の .html および .csv ファイル総数を事前にカウントして返す。"""
    html_cnt = 0
    csv_cnt = 0
    for zp in zip_paths:
        with zipfile.ZipFile(zp, "r") as zf:
            for name in zf.namelist():
                lower = name.lower()
                if lower.endswith(".html"):
                    html_cnt += 1
                elif lower.endswith(".csv"):
                    csv_cnt += 1
    return html_cnt, csv_cnt


def count_unique_db_csvs(zip_paths: List[str]) -> int:
    """ZIP内のユニークなNotion DB CSV数（_all.csvペア重複・添付CSV・複数ZIP重複を除外）を返す。"""
    seen_db_ids: Set[str] = set()
    for zp in zip_paths:
        with zipfile.ZipFile(zp, "r") as zf:
            for name in zf.namelist():
                if not _is_notion_db_csv_filename(name):
                    continue
                base_no_ext = os.path.splitext(_norm_zip_path(name))[0]
                if base_no_ext.lower().endswith("_all"):
                    base_no_ext = base_no_ext[:-4]
                uids = extract_uuids(os.path.basename(base_no_ext))
                norm_uid = normalize_notion_id(uids[-1]) if uids else normalize_notion_id(base_no_ext)
                if norm_uid:
                    seen_db_ids.add(norm_uid)
    return len(seen_db_ids)


def _find_plan_node_by_id(roots: List[PlanTreeNode], target_id: str) -> Optional[PlanTreeNode]:
    target_norm = normalize_notion_id(target_id)
    if not target_norm:
        return None
    stack = list(roots)
    while stack:
        curr = stack.pop(0)
        if normalize_notion_id(curr.node_id) == target_norm:
            return curr
        stack.extend(curr.children)
    return None


def scan_zips_to_plan_trees(
    zip_paths: List[str],
    root_page_id: Optional[str] = None,
) -> List[PlanTreeNode]:
    """エクスポートZIP（単一または複数）からAPIコールなしでPlanTreeNodeのルートリストを構築する。"""
    if not zip_paths:
        raise ValueError("No ZIP files provided.")
    if len(zip_paths) == 1:
        root_pnode = HtmlZipExtractor(zip_paths[0]).extract_tree()
    else:
        root_pnode = MultiZipExtractor(zip_paths=zip_paths).extract_tree()
    roots = [_page_node_to_plan_node(root_pnode)]
    if root_page_id:
        target = _find_plan_node_by_id(roots, root_page_id)
        if not target:
            raise ValueError(f"Root page/database ID '{root_page_id}' not found in ZIP export.")
        target.parent_id = ""
        return [target]
    return roots


def _compute_subtree_counts(node: PlanTreeNode, include_db_rows: bool) -> int:
    count = 0
    for c in node.children:
        if not include_db_rows and c.node_type == "database_row":
            continue
        count += 1 + _compute_subtree_counts(c, include_db_rows=include_db_rows)
    return count


def flatten_plan_trees(
    roots: List[PlanTreeNode],
    include_db_rows: bool = True,
) -> List[Dict[str, Any]]:
    """ツリーを深さ優先探索でフラット化し、1ページ1行の辞書リストを返す。"""
    rows: List[Dict[str, Any]] = []
    order_counter = 0

    def _walk(node: PlanTreeNode, depth: int, path_parts: List[str]) -> None:
        nonlocal order_counter
        if not include_db_rows and node.node_type == "database_row":
            return
        order_counter += 1
        curr_path = path_parts + [node.title]
        visible_children = [
            c for c in node.children if include_db_rows or c.node_type != "database_row"
        ]
        subtree_count = _compute_subtree_counts(node, include_db_rows=include_db_rows)
        if node.node_type == "database_row" and depth == 1:
            level_1_val = curr_path[0]
        else:
            level_1_val = curr_path[1] if len(curr_path) > 1 else curr_path[0]
        rows.append({
            "tree_order": order_counter,
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "depth": depth,
            "node_type": node.node_type,
            "title": node.title,
            "path": " > ".join(curr_path),
            "level_1": level_1_val,
            "level_2": curr_path[2] if len(curr_path) > 2 else "",
            "level_3": curr_path[3] if len(curr_path) > 3 else "",
            "direct_children_count": len(visible_children),
            "subtree_node_count": subtree_count,
            "db_row_count": "" if node.db_row_count is None else node.db_row_count,
            "last_edited_time": node.last_edited_time,
            "archived": "true" if node.archived else "false",
            "notion_url": node.notion_url,
            "migration_action": "",
            "phase": "",
            "owner": "",
            "notes": "",
        })
        for child in visible_children:
            _walk(child, depth + 1, curr_path)

    for r in roots:
        _walk(r, 0, [])
    return rows


def export_pages_csv(rows: List[Dict[str, Any]], output_path: str) -> str:
    """1ページ1行の明細CSV（UTF-8-BOM）を書き出す。サマリー行は混入させない。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fieldnames = [
        "tree_order",
        "node_id",
        "parent_id",
        "depth",
        "node_type",
        "title",
        "path",
        "level_1",
        "level_2",
        "level_3",
        "direct_children_count",
        "subtree_node_count",
        "db_row_count",
        "last_edited_time",
        "archived",
        "notion_url",
        "migration_action",
        "phase",
        "owner",
        "notes",
    ]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return output_path


def build_summary_markdown(
    roots: List[PlanTreeNode],
    rows: List[Dict[str, Any]],
    source_label: str,
    api_calls: int = 0,
    elapsed_seconds: float = 0.0,
) -> str:
    """サマリー情報をMarkdown形式で生成する。"""
    total = len(rows)
    by_type: Dict[str, int] = {"page": 0, "database": 0, "database_row": 0}
    archived_cnt = 0
    max_depth = 0
    by_depth: Dict[int, int] = {}

    for r in rows:
        nt = r["node_type"]
        by_type[nt] = by_type.get(nt, 0) + 1
        if r["archived"] == "true":
            archived_cnt += 1
        d = int(r["depth"])
        max_depth = max(max_depth, d)
        by_depth[d] = by_depth.get(d, 0) + 1

    # DB総レコード数（include_db_rows=False 時も実DB行数を合算）
    total_db_rows_actual = 0
    db_list: List[Tuple[str, str, int]] = []

    def _collect_db_stats(node: PlanTreeNode) -> None:
        nonlocal total_db_rows_actual
        if node.node_type == "database":
            rc = node.db_row_count or 0
            total_db_rows_actual += rc
            db_list.append((node.title, node.node_id, rc))
        for c in node.children:
            _collect_db_stats(c)

    for root in roots:
        _collect_db_stats(root)

    # 第1階層 (移行単位候補) 集計
    level1_stats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        l1 = r["level_1"] or r["title"]
        st = level1_stats.setdefault(
            l1,
            {"total": 0, "pages": 0, "dbs": 0, "rows": 0, "latest": ""},
        )
        st["total"] += 1
        if r["node_type"] == "page":
            st["pages"] += 1
        elif r["node_type"] == "database":
            st["dbs"] += 1
        elif r["node_type"] == "database_row":
            st["rows"] += 1
        le = r.get("last_edited_time") or ""
        if le > st["latest"]:
            st["latest"] = le

    now_str = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "# Notion ページツリー調査サマリー",
        "",
        f"- **調査ソース**: {source_label}",
        f"- **生成日時**: {now_str}",
        f"- **所要時間**: {elapsed_seconds:.2f} 秒 (APIリクエスト数: {api_calls})",
    ]
    if len(roots) == 1:
        r0 = roots[0]
        lines.append(f"- **ルートページ**: {r0.title} (`{r0.node_type}` / ID: `{r0.node_id}`)")
        if r0.notion_url:
            lines.append(f"- **ルートURL**: {r0.notion_url}")
    elif roots:
        lines.append(f"- **ルートページ数**: {len(roots)} 件")

    if roots:
        lines.extend([
            "",
            "## 0. ルートページ情報",
            "",
            "| タイトル | 種別 | ID | 直下子数 | URL |",
            "| :--- | :--- | :--- | ---: | :--- |",
        ])
        for rn in roots[:20]:
            url_str = rn.notion_url or "-"
            lines.append(f"| {rn.title} | `{rn.node_type}` | `{rn.node_id}` | {len(rn.children)} | {url_str} |")
        if len(roots) > 20:
            lines.append(f"| ... (他 {len(roots) - 20} 件) | | | | |")

    lines.extend([
        "",
        "## 1. 全体統計",
        "",
        "| 項目 | 件数 |",
        "| :--- | ---: |",
        f"| 出力ノード総数 (CSV行数) | {total} |",
        f"| 通常ページ (`page`) | {by_type.get('page', 0)} |",
        f"| データベース (`database`) | {by_type.get('database', 0)} |",
        f"| DB行レコード (`database_row` 実数) | {total_db_rows_actual} |",
        f"| アーカイブ / ゴミ箱 (`archived`) | {archived_cnt} |",
        f"| 最大階層深さ (`depth`) | {max_depth} |",
        "",
        "## 2. 階層深さ別分布",
        "",
        "| depth | ノード数 |",
        "| ---: | ---: |",
    ])
    for d in sorted(by_depth.keys()):
        lines.append(f"| {d} | {by_depth[d]} |")

    lines.extend([
        "",
        "## 3. 移行単位候補（第1階層 `level_1` 別）",
        "",
        "| level_1 | 総ノード数 | ページ | DB | DB行 | 最終更新 |",
        "| :--- | ---: | ---: | ---: | ---: | :--- |",
    ])
    for l1, st in sorted(level1_stats.items(), key=lambda x: (-x[1]["total"], x[0])):
        latest_short = st["latest"][:10] if st["latest"] else "-"
        lines.append(
            f"| {l1} | {st['total']} | {st['pages']} | {st['dbs']} | {st['rows']} | {latest_short} |"
        )

    if db_list:
        lines.extend([
            "",
            "## 4. データベース一覧（行数降順 Top 20）",
            "",
            "| データベース名 | 行数 | ID |",
            "| :--- | ---: | :--- |",
        ])
        for title, nid, rc in sorted(db_list, key=lambda x: (-x[2], x[0]))[:20]:
            lines.append(f"| {title} | {rc} | `{nid}` |")

    lines.append("")
    return "\n".join(lines)


def export_summary_markdown(summary_md: str, output_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(summary_md)
    return output_path


_ZIP_UUID_SUFFIX_RE = re.compile(r"\s+([0-9a-fA-F]{32})$")
_NOTION_DB_CSV_SUFFIX_RE = re.compile(r"(?:^|[\s_-])([0-9a-fA-F]{32})(?:_all)?$", re.IGNORECASE)


def _is_notion_db_csv_filename(path: str) -> bool:
    """Notion DBのエクスポートCSV（末尾32桁UUID付き .csv / _all.csv）か判定し、添付CSVと区別する。"""
    if not path.lower().endswith(".csv"):
        return False
    stem = urllib.parse.unquote(os.path.basename(os.path.splitext(path)[0]))
    return bool(_NOTION_DB_CSV_SUFFIX_RE.search(stem))


def _count_csv_data_rows_fast(raw_bytes: bytes) -> int:
    """BeautifulSoup等を使わずにCSVバイト列のデータ行数（ヘッダ除く）を高速カウントする。"""
    text = HtmlZipExtractor._decode_csv_bytes(raw_bytes)
    if not text.strip():
        return 0
    import io as _io
    reader = csv.reader(_io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return 0
    return sum(1 for row in reader if any(cell.strip() for cell in row))


def analyze_zips_lightweight(
    zip_paths: List[str],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    exclude_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """HTMLパースを行わず、ZIPメタデータとCSV行数カウントのみでエクスポート規模を高速算出する。"""
    ex_ids = {normalize_notion_id(x) for x in (exclude_ids or set()) if normalize_notion_id(x)}
    total_html = 0
    total_csv = 0
    total_other = 0
    total_compressed_bytes = 0
    total_uncompressed_bytes = 0
    total_csv_rows = 0
    ext_counts: Dict[str, int] = {}
    ext_bytes: Dict[str, int] = {}
    folder_stats: Dict[str, Dict[str, Any]] = {}
    db_entries: List[Dict[str, Any]] = []
    largest_files: List[Dict[str, Any]] = []

    for z_idx, zp in enumerate(zip_paths, start=1):
        z_name = os.path.basename(zp)
        if progress_cb:
            progress_cb(z_idx, len(zip_paths), z_name)
        with zipfile.ZipFile(zp, "r") as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            excluded_prefixes: List[str] = []
            if ex_ids:
                for info in infos:
                    fn = info.filename
                    low = fn.lower()
                    if low.endswith((".html", ".csv")):
                        base_no_ext = os.path.splitext(fn)[0]
                        uids = extract_uuids(os.path.basename(base_no_ext))
                        if uids and normalize_notion_id(uids[-1]) in ex_ids:
                            uid_l = normalize_notion_id(uids[-1])
                            clean_base = _ZIP_UUID_SUFFIX_RE.sub("", base_no_ext).rstrip("/")
                            excluded_prefixes.append(clean_base + "/")
                            excluded_prefixes.append(f"{clean_base} {uid_l[:4]}-{uid_l[-4:]}/")
                            excluded_prefixes.append(f"{clean_base} {uid_l}/")

            html_paths: List[str] = []

            for info in infos:
                fname = info.filename
                if ex_ids:
                    if any(fname.startswith(pref) for pref in excluded_prefixes):
                        continue
                    fn_uids = extract_uuids(os.path.basename(os.path.splitext(fname)[0]))
                    if fn_uids and normalize_notion_id(fn_uids[-1]) in ex_ids:
                        continue
                lower = fname.lower()
                ext = os.path.splitext(lower)[1] or "(no_ext)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                ext_bytes[ext] = ext_bytes.get(ext, 0) + info.file_size
                total_compressed_bytes += info.compress_size
                total_uncompressed_bytes += info.file_size

                parts = [p for p in fname.split("/") if p]
                top_folder = parts[0] if len(parts) > 1 else "(root)"
                fst = folder_stats.setdefault(
                    top_folder,
                    {"folder": top_folder, "html_count": 0, "csv_count": 0, "csv_rows": 0, "other_count": 0, "total_bytes": 0},
                )
                fst["total_bytes"] += info.file_size

                largest_files.append({"zip_name": z_name, "path": fname, "bytes": info.file_size})

                if lower.endswith(".html"):
                    total_html += 1
                    fst["html_count"] += 1
                    html_paths.append(fname)
                elif _is_notion_db_csv_filename(fname):
                    total_csv += 1
                    fst["csv_count"] += 1
                    raw_csv = zf.read(fname)
                    row_cnt = _count_csv_data_rows_fast(raw_csv)
                    total_csv_rows += row_cnt
                    fst["csv_rows"] += row_cnt

                    base_no_ext = os.path.splitext(fname)[0]
                    base_name = os.path.basename(base_no_ext)
                    m_uid = _ZIP_UUID_SUFFIX_RE.search(base_name)
                    db_id = _format_uuid(m_uid.group(1)) if m_uid else ""
                    db_title = _ZIP_UUID_SUFFIX_RE.sub("", base_name).strip() or base_name
                    db_entries.append({
                        "zip_name": z_name,
                        "csv_path": fname,
                        "db_title": db_title,
                        "db_id": db_id,
                        "row_count": row_cnt,
                        "csv_bytes": info.file_size,
                        "_dir_prefix": _ZIP_UUID_SUFFIX_RE.sub("", base_no_ext).rstrip("/") + "/",
                        "_short_uid_prefix": (
                            f"{_ZIP_UUID_SUFFIX_RE.sub('', base_no_ext).rstrip('/')} {m_uid.group(1).lower()[:4]}-{m_uid.group(1).lower()[-4:]}/"
                            if m_uid
                            else ""
                        ),
                    })
                else:
                    total_other += 1
                    fst["other_count"] += 1

            # 同じZIP内の各DBに対応する行HTMLディレクトリ配下のHTML件数を高速集計
            for db_e in db_entries:
                if db_e["zip_name"] != z_name:
                    continue
                p1 = db_e.pop("_dir_prefix")
                p2 = db_e.pop("_short_uid_prefix")
                row_html_cnt = sum(
                    1 for hp in html_paths
                    if hp.startswith(p1) or (p2 and hp.startswith(p2))
                )
                db_e["row_html_count"] = row_html_cnt

    largest_files.sort(key=lambda x: -x["bytes"])
    db_entries.sort(key=lambda x: (-x["row_count"], -x["csv_bytes"], x["db_title"]))
    folders_sorted = sorted(folder_stats.values(), key=lambda x: (-x["total_bytes"], -x["html_count"], x["folder"]))

    return {
        "zip_count": len(zip_paths),
        "total_files": total_html + total_csv + total_other,
        "total_html": total_html,
        "total_csv": total_csv,
        "total_other": total_other,
        "total_csv_rows": total_csv_rows,
        "total_compressed_bytes": total_compressed_bytes,
        "total_uncompressed_bytes": total_uncompressed_bytes,
        "ext_counts": ext_counts,
        "ext_bytes": ext_bytes,
        "folders": folders_sorted,
        "databases": db_entries,
        "largest_files": largest_files[:20],
    }


def build_zip_stats_markdown(stats: Dict[str, Any], elapsed_seconds: float = 0.0) -> str:
    """analyze_zips_lightweight の結果を Markdown レポートとして整形する。"""
    def _fmt_mb(b: int) -> str:
        return f"{b / (1024 * 1024):.2f} MB"

    now_str = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "# Notion エクスポートZIP 軽量規模サマリー (`--stats-only`)",
        "",
        f"- **生成日時**: {now_str}",
        f"- **対象ZIP数**: {stats['zip_count']} 件",
        f"- **解析時間**: {elapsed_seconds:.2f} 秒 (HTMLフルパースなし)",
        "",
        "## 1. 全体規模",
        "",
        "| 項目 | 値 |",
        "| :--- | ---: |",
        f"| 総ファイル数 | {stats['total_files']} |",
        f"| HTMLファイル数 (ページ＋DB行HTML) | {stats['total_html']} |",
        f"| DB CSVファイル数 | {stats['total_csv']} |",
        f"| DB CSV総レコード行数 | {stats['total_csv_rows']} |",
        f"| 添付・その他ファイル数 | {stats['total_other']} |",
        f"| 非圧縮合計サイズ | {_fmt_mb(stats['total_uncompressed_bytes'])} |",
        f"| ZIP圧縮合計サイズ | {_fmt_mb(stats['total_compressed_bytes'])} |",
        "",
        "## 2. トップレベルフォルダ別規模",
        "",
        "| フォルダ | HTML | DB CSV | CSV行数 | 添付/他 | 合計サイズ |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for fst in stats["folders"][:30]:
        lines.append(
            f"| {fst['folder']} | {fst['html_count']} | {fst['csv_count']} | {fst['csv_rows']} | {fst['other_count']} | {_fmt_mb(fst['total_bytes'])} |"
        )
    if len(stats["folders"]) > 30:
        lines.append(f"| ... (他 {len(stats['folders']) - 30} フォルダ) | | | | | |")

    if stats["databases"]:
        lines.extend([
            "",
            "## 3. データベース一覧（CSV行数降順 Top 30）",
            "",
            "| データベース名 | CSV行数 | 行HTML数 | CSVサイズ | ID |",
            "| :--- | ---: | ---: | ---: | :--- |",
        ])
        for db in stats["databases"][:30]:
            lines.append(
                f"| {db['db_title']} | {db['row_count']} | {db.get('row_html_count', 0)} | {db['csv_bytes']:,} B | `{db['db_id']}` |"
            )

    lines.append("")
    return "\n".join(lines)


def export_zip_stats_csvs(stats: Dict[str, Any], output_dir: str) -> Tuple[str, str]:
    """軽量規模分析のDB一覧CSV・フォルダ一覧CSVを書き出す。"""
    os.makedirs(output_dir, exist_ok=True)
    db_csv_path = os.path.join(output_dir, "zip_databases.csv")
    folder_csv_path = os.path.join(output_dir, "zip_folders.csv")

    with open(db_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["zip_name", "db_title", "db_id", "row_count", "row_html_count", "csv_bytes", "csv_path"],
        )
        writer.writeheader()
        for db in stats["databases"]:
            writer.writerow(db)

    with open(folder_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["folder", "html_count", "csv_count", "csv_rows", "other_count", "total_bytes"],
        )
        writer.writeheader()
        for fst in stats["folders"]:
            writer.writerow(fst)

    return db_csv_path, folder_csv_path


def load_exclude_ids(
    exclude_ids: Optional[List[str]] = None,
    exclude_file: Optional[str] = None,
) -> Set[str]:
    """コマンドライン引数および除外リストファイルから正規化済みNotion ID集合を読み込む。"""
    result: Set[str] = set()
    for raw in exclude_ids or []:
        uids = extract_uuids(str(raw))
        if uids:
            for u in uids:
                result.add(normalize_notion_id(u))
        else:
            norm = normalize_notion_id(str(raw))
            if norm:
                result.add(norm)
    if exclude_file:
        if not os.path.exists(exclude_file):
            raise FileNotFoundError(f"Exclude file not found: {exclude_file}")
        with open(exclude_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                stripped = line.split("#", 1)[0].strip()
                if not stripped:
                    continue
                uids = extract_uuids(stripped)
                if uids:
                    result.add(normalize_notion_id(uids[0]))
                else:
                    norm = normalize_notion_id(stripped)
                    if norm and len(norm) >= 32:
                        result.add(norm)
    return {x for x in result if x}


def prune_excluded_subtrees(
    roots: List[PlanTreeNode],
    exclude_ids: Optional[Set[str]],
) -> List[PlanTreeNode]:
    """指定された除外IDに一致するノードとその配下サブツリーをツリーから除去する。"""
    ex_ids = {normalize_notion_id(x) for x in (exclude_ids or set()) if normalize_notion_id(x)}
    if not ex_ids:
        return roots

    def _filter_node(node: PlanTreeNode) -> Optional[PlanTreeNode]:
        if normalize_notion_id(node.node_id) in ex_ids:
            return None
        had_row_children = any(c.node_type == "database_row" for c in node.children)
        kept_children: List[PlanTreeNode] = []
        for c in node.children:
            fc = _filter_node(c)
            if fc is not None:
                kept_children.append(fc)
        node.children = kept_children
        if node.node_type == "database" and had_row_children:
            node.db_row_count = sum(1 for c in node.children if c.node_type == "database_row")
        return node

    pruned: List[PlanTreeNode] = []
    for r in roots:
        fr = _filter_node(r)
        if fr is not None:
            pruned.append(fr)
    return pruned


_HTML_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _extract_title_fast_from_zip(zf: zipfile.ZipFile, entry_path: str, fallback_stem: str) -> str:
    """HTMLの先頭数KBのみから<title>を正規表現で高速抽出し、失敗時はファイル名から補完する。"""
    try:
        with zf.open(entry_path, "r") as fh:
            head_bytes = fh.read(4096)
        head_text = head_bytes.decode("utf-8", errors="ignore")
        m = _HTML_TITLE_TAG_RE.search(head_text)
        if m:
            t = _html.unescape(m.group(1)).strip()
            if t:
                return t
    except Exception:
        pass
    clean = _ZIP_UUID_SUFFIX_RE.sub("", fallback_stem)
    clean = re.sub(r"\s+[0-9a-fA-F]{4}-[0-9a-fA-F]{4}$", "", clean)
    return urllib.parse.unquote(clean).strip() or "Untitled"


def _norm_zip_path(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def scan_zips_fast_tree(
    zip_paths: List[str],
    root_page_id: Optional[str] = None,
    include_db_rows: bool = True,
    exclude_ids: Optional[Set[str]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[PlanTreeNode]:
    """HTML本文パースをスキップし、全ZIP横断のパス階層・<title>・CSV行数のみから高速にツリーを構築する。"""
    if not zip_paths:
        raise ValueError("No ZIP files provided.")
    ex_ids = {normalize_notion_id(x) for x in (exclude_ids or set()) if normalize_notion_id(x)}

    nodes_by_norm: Dict[str, PlanTreeNode] = {}
    dir_to_parent: Dict[str, PlanTreeNode] = {}
    roots: List[PlanTreeNode] = []
    db_dir_prefixes: Dict[str, PlanTreeNode] = {}
    db_folder_name_map: Dict[str, List[PlanTreeNode]] = {}
    excluded_dir_prefixes: List[str] = []

    with ExitStack() as stack:
        zfs: List[zipfile.ZipFile] = []
        all_entries: List[Tuple[str, str, int]] = []  # (norm_fname, raw_fname, zf_idx)
        all_norm_set: Set[str] = set()

        for idx, zp in enumerate(zip_paths, start=1):
            if progress_cb:
                progress_cb(idx, len(zip_paths), os.path.basename(zp))
            zf = stack.enter_context(zipfile.ZipFile(zp, "r"))
            zf_idx = len(zfs)
            zfs.append(zf)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                raw_fn = info.filename
                if not raw_fn.lower().endswith((".html", ".csv")):
                    continue
                norm_fn = _norm_zip_path(raw_fn)
                if norm_fn in all_norm_set:
                    continue
                all_norm_set.add(norm_fn)
                all_entries.append((norm_fn, raw_fn, zf_idx))

        # 階層が浅い順（ディレクトリ深さ昇順）かつCSV/HTMLを安定順で走査
        all_entries.sort(key=lambda item: (item[0].count("/"), 0 if item[0].lower().endswith(".csv") else 1, item[0]))

        if ex_ids:
            for norm_fn, _, _ in all_entries:
                base_no_ext = os.path.splitext(norm_fn)[0]
                uids = extract_uuids(os.path.basename(base_no_ext))
                if uids and normalize_notion_id(uids[-1]) in ex_ids:
                    uid_l = normalize_notion_id(uids[-1])
                    clean_base = _ZIP_UUID_SUFFIX_RE.sub("", base_no_ext).rstrip("/")
                    excluded_dir_prefixes.append(clean_base + "/")
                    excluded_dir_prefixes.append(f"{clean_base} {uid_l[:4]}-{uid_l[-4:]}/")
                    excluded_dir_prefixes.append(f"{clean_base} {uid_l}/")

        def _is_excluded(norm_fn: str) -> bool:
            if not ex_ids:
                return False
            if any(norm_fn.startswith(pref) for pref in excluded_dir_prefixes):
                return True
            uids = extract_uuids(os.path.basename(os.path.splitext(norm_fn)[0]))
            return bool(uids and normalize_notion_id(uids[-1]) in ex_ids)

        def _find_parent_for_path(norm_fn: str) -> Optional[PlanTreeNode]:
            parent_dir = os.path.dirname(norm_fn).rstrip("/")
            while parent_dir:
                if parent_dir in dir_to_parent:
                    return dir_to_parent[parent_dir]
                parent_dir = os.path.dirname(parent_dir).rstrip("/")
            return None

        def _find_enclosing_db(norm_fn: str) -> Tuple[Optional[PlanTreeNode], bool]:
            """(直接の親DB, 何らかのDB配下か) を親ディレクトリを遡って O(depth) で判定する。"""
            direct_dir = os.path.dirname(norm_fn).rstrip("/")
            if not direct_dir:
                return None, False
            direct_slash = direct_dir + "/"
            if direct_slash in db_dir_prefixes:
                return db_dir_prefixes[direct_slash], True
            cur = os.path.dirname(direct_dir).rstrip("/")
            while cur:
                if (cur + "/") in db_dir_prefixes:
                    return None, True
                cur = os.path.dirname(cur).rstrip("/")
            # フルパス一致しない場合（グローバル側DBと別階層に行HTMLがある場合等）はフォルダ名一致でフォールバック
            direct_folder = os.path.basename(direct_dir)
            if direct_folder in db_folder_name_map and len(db_folder_name_map[direct_folder]) == 1:
                return db_folder_name_map[direct_folder][0], True
            cur = os.path.dirname(direct_dir).rstrip("/")
            while cur:
                seg = os.path.basename(cur)
                if seg in db_folder_name_map and len(db_folder_name_map[seg]) == 1:
                    return None, True
                cur = os.path.dirname(cur).rstrip("/")
            return None, False

        def _register_dir_aliases(base_no_ext: str, norm_uid: str, node: PlanTreeNode) -> None:
            clean_base = _ZIP_UUID_SUFFIX_RE.sub("", base_no_ext).rstrip("/")
            clean_base = re.sub(r"\s+[0-9a-fA-F]{4}-[0-9a-fA-F]{4}$", "", clean_base).rstrip("/")
            for cand in (
                base_no_ext.rstrip("/"),
                clean_base,
                f"{clean_base} {norm_uid[:4]}-{norm_uid[-4:]}" if len(norm_uid) == 32 else "",
                f"{clean_base} {norm_uid}" if len(norm_uid) == 32 else "",
            ):
                if cand and cand not in dir_to_parent:
                    dir_to_parent[cand] = node

        # Pass 1: 全ZIPの DB CSV を解析し、統合 db_dir_prefixes と pending_dbs を構築
        pending_dbs: List[Tuple[str, str, str, PlanTreeNode]] = []
        for norm_fn, raw_fn, zf_idx in all_entries:
            if not _is_notion_db_csv_filename(norm_fn) or _is_excluded(norm_fn):
                continue
            if norm_fn.lower().endswith("_all.csv") and (norm_fn[:-8] + ".csv") in all_norm_set:
                continue
            base_no_ext = os.path.splitext(norm_fn)[0]
            if base_no_ext.endswith("_all"):
                base_no_ext = base_no_ext[:-4]
            base_name = os.path.basename(base_no_ext)
            uids = extract_uuids(base_name)
            norm_uid = normalize_notion_id(uids[-1]) if uids else normalize_notion_id(norm_fn)
            if norm_uid in nodes_by_norm:
                continue
            db_title = urllib.parse.unquote(_ZIP_UUID_SUFFIX_RE.sub("", base_name)).strip() or "Untitled Database"
            row_cnt = _count_csv_data_rows_fast(zfs[zf_idx].read(raw_fn))
            db_node = PlanTreeNode(
                node_id=_format_uuid(norm_uid),
                parent_id="",
                node_type="database",
                title=db_title,
                notion_url=f"https://www.notion.so/{norm_uid}" if len(norm_uid) == 32 else "",
                db_row_count=row_cnt,
            )
            nodes_by_norm[norm_uid] = db_node
            pending_dbs.append((norm_fn, base_no_ext, norm_uid, db_node))

            clean_base = _ZIP_UUID_SUFFIX_RE.sub("", base_no_ext).rstrip("/")
            for d_pref in (
                clean_base + "/",
                f"{clean_base} {norm_uid[:4]}-{norm_uid[-4:]}/" if len(norm_uid) == 32 else "",
                f"{clean_base} {norm_uid}/" if len(norm_uid) == 32 else "",
            ):
                if d_pref:
                    db_dir_prefixes[d_pref] = db_node

            clean_folder = os.path.basename(clean_base)
            for f_name in (
                clean_folder,
                f"{clean_folder} {norm_uid[:4]}-{norm_uid[-4:]}" if len(norm_uid) == 32 else "",
                f"{clean_folder} {norm_uid}" if len(norm_uid) == 32 else "",
            ):
                if f_name:
                    db_folder_name_map.setdefault(f_name, []).append(db_node)

        # Pass 2: 通常ページHTML（DBディレクトリ外）を浅い階層順に処理
        pending_row_htmls: List[Tuple[str, str, str, str, str, int, Optional[PlanTreeNode]]] = []
        for norm_fn, raw_fn, zf_idx in all_entries:
            if not norm_fn.lower().endswith(".html") or _is_excluded(norm_fn):
                continue
            base_no_ext = os.path.splitext(norm_fn)[0]
            base_name = os.path.basename(base_no_ext)
            uids = extract_uuids(base_name)
            norm_uid = normalize_notion_id(uids[-1]) if uids else normalize_notion_id(norm_fn)

            if norm_uid in nodes_by_norm:
                continue

            owner_db, under_db = _find_enclosing_db(norm_fn)
            if under_db:
                if include_db_rows:
                    pending_row_htmls.append((norm_fn, raw_fn, base_no_ext, base_name, norm_uid, zf_idx, owner_db))
                continue

            parent_node = _find_parent_for_path(norm_fn)
            page_title = _extract_title_fast_from_zip(zfs[zf_idx], raw_fn, base_name)
            page_node = PlanTreeNode(
                node_id=_format_uuid(norm_uid),
                parent_id=parent_node.node_id if parent_node else "",
                node_type="page",
                title=page_title,
                notion_url=f"https://www.notion.so/{norm_uid}" if len(norm_uid) == 32 else "",
            )
            nodes_by_norm[norm_uid] = page_node
            if parent_node:
                parent_node.children.append(page_node)
            else:
                roots.append(page_node)
            _register_dir_aliases(base_no_ext, norm_uid, page_node)

        # Pass 3: DBノードを親ページへ紐付け
        for norm_fn, base_no_ext, norm_uid, db_node in pending_dbs:
            parent_node = _find_parent_for_path(norm_fn)
            if parent_node:
                db_node.parent_id = parent_node.node_id
                parent_node.children.append(db_node)
            else:
                roots.append(db_node)
            _register_dir_aliases(base_no_ext, norm_uid, db_node)

        # Pass 4: DB行HTMLおよびその配下のネストページを登録 (include_db_rows=True時のみ)
        for norm_fn, raw_fn, base_no_ext, base_name, norm_uid, zf_idx, owner_db in pending_row_htmls:
            parent_node = owner_db if owner_db is not None else _find_parent_for_path(norm_fn)
            n_type = "database_row" if owner_db is not None else "page"
            row_title = _extract_title_fast_from_zip(zfs[zf_idx], raw_fn, base_name)
            row_node = PlanTreeNode(
                node_id=_format_uuid(norm_uid),
                parent_id=parent_node.node_id if parent_node else "",
                node_type=n_type,
                title=row_title,
                notion_url=f"https://www.notion.so/{norm_uid}" if len(norm_uid) == 32 else "",
            )
            nodes_by_norm[norm_uid] = row_node
            if parent_node:
                parent_node.children.append(row_node)
            else:
                roots.append(row_node)
            _register_dir_aliases(base_no_ext, norm_uid, row_node)

    if root_page_id:
        target = _find_plan_node_by_id(roots, root_page_id)
        if not target:
            raise ValueError(f"Root page/database ID '{root_page_id}' not found in ZIP export.")
        target.parent_id = ""
        return [target]
    return roots
