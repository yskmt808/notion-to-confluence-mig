"""移行計画作成用のページツリー調査・CSV/Markdownサマリー出力モジュール。"""

import csv
import json
import logging
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from src.extractor import PageNode, normalize_notion_id
from src.html_extractor import HtmlZipExtractor, MultiZipExtractor

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
            return data.get("results", []), data.get("next_cursor"), bool(data.get("search_completed", False))
        except Exception as e:
            logger.warning(f"Failed to load search cache {self.cache_file}: {e}")
            return [], None, False

    def _save_cache(self, results: List[Dict[str, Any]], next_cursor: Optional[str], completed: bool) -> None:
        if not self.cache_file:
            return
        parent = os.path.dirname(os.path.abspath(self.cache_file))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{self.cache_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "results": results,
                    "next_cursor": next_cursor,
                    "search_completed": completed,
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp_path, self.cache_file)

    def _resolve_block_parent_page(self, block_id: str, known_ids: Set[str]) -> Optional[str]:
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
    ) -> List[PlanTreeNode]:
        """POST /v1/search で全ページ/DBを一括取得し、ツリー構造を構築してルートリストを返す。"""
        all_items, next_cursor, search_completed = self._load_cache()
        seen_ids = {normalize_notion_id(str(it.get("id") or "")) for it in all_items if it.get("id")}

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
                if progress_cb:
                    progress_cb(len(all_items))
                has_more = bool(data.get("has_more"))
                next_cursor = data.get("next_cursor")
                self._save_cache(all_items, next_cursor, not has_more)
                if not has_more or not next_cursor:
                    break

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
                    parent_norm = self._resolve_block_parent_page(blk_id, known_ids)

            if parent_norm and parent_norm in nodes_by_norm and parent_norm != norm:
                parent_node = nodes_by_norm[parent_norm]
                node.parent_id = parent_node.node_id
                parent_node.children.append(node)
            else:
                roots.append(node)

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


def scan_zips_to_plan_trees(zip_paths: List[str]) -> List[PlanTreeNode]:
    """エクスポートZIP（単一または複数）からAPIコールなしでPlanTreeNodeのルートリストを構築する。"""
    if not zip_paths:
        raise ValueError("No ZIP files provided.")
    if len(zip_paths) == 1:
        root_pnode = HtmlZipExtractor(zip_paths[0]).extract_tree()
    else:
        root_pnode = MultiZipExtractor(zip_paths=zip_paths).extract_tree()
    return [_page_node_to_plan_node(root_pnode)]


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
        rows.append({
            "tree_order": order_counter,
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "depth": depth,
            "node_type": node.node_type,
            "title": node.title,
            "path": " > ".join(curr_path),
            "level_1": curr_path[1] if len(curr_path) > 1 else curr_path[0],
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
    ]
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
