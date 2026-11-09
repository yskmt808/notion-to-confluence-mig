"""
Notion からページ、ブロック階層、データベースおよびレコードを再帰的に抽出するモジュール。
"""

import logging
import time
import requests
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from notion_client import Client

logger = logging.getLogger(__name__)


def _retry_notion_call(fn, max_retries: int = 4, base_delay: float = 1.0):
    """Notion API呼び出しの一時的接続断やレートリミットを指数バックオフで再試行する。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err_str = str(e).lower()
            if "linked database" in err_str:
                raise
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Notion API error ({e}), retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)


@dataclass
class PageNode:
    id: str
    title: str
    node_type: str  # "page" | "database" | "database_row"
    icon: Optional[str] = None  # 絵文字アイコン (例: "🚀", "📝")
    cover: Optional[Dict[str, Any]] = None  # カバー画像
    url: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    children: List["PageNode"] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)
    created_time: Optional[str] = None
    created_by: Optional[Dict[str, Any]] = None
    last_edited_time: Optional[str] = None
    last_edited_by: Optional[Dict[str, Any]] = None
    _cover_filename: Optional[str] = None


class NotionExtractor:
    def __init__(self, token: str):
        self.token = token
        self.notion = Client(auth=token, notion_version="2022-06-28")

    def extract_tree(self, root_page_id: str) -> PageNode:
        """指定されたルートページから配下の全ページ・ブロック・DBを再帰的に抽出する。"""
        logger.info(f"Extracting Notion page: {root_page_id}")
        page_info = _retry_notion_call(lambda: self.notion.pages.retrieve(page_id=root_page_id))
        title = self._get_page_title(page_info)
        url = page_info.get("url")

        from src.icon_mapper import resolve_notion_icon_to_emoji

        icon_obj = page_info.get("icon") or {}
        icon_emoji = resolve_notion_icon_to_emoji(icon_obj)
        cover_obj = page_info.get("cover")

        root_node = PageNode(
            id=root_page_id,
            title=title,
            node_type="page",
            icon=icon_emoji,
            cover=cover_obj,
            url=url,
            created_time=page_info.get("created_time"),
            created_by=page_info.get("created_by"),
            last_edited_time=page_info.get("last_edited_time"),
            last_edited_by=page_info.get("last_edited_by"),
        )
        if cover_obj:
            root_node._cover_filename = f"cover_{root_page_id.replace('-', '')[:8]}.jpg"

        # ページ最上位コメントの抽出
        try:
            c_res = _retry_notion_call(lambda: self.notion.comments.list(block_id=root_page_id))
            root_node.comments = c_res.get("results", [])
            if root_node.comments:
                logger.info(f"  Extracted {len(root_node.comments)} top-level comments for page [{title}]")
        except Exception as e:
            logger.debug(f"  Could not extract comments for page {root_page_id}: {e}")

        # ブロックツリーと子ページの抽出
        root_node.blocks, root_node.children = self._extract_blocks_and_children(root_page_id)
        return root_node

    def _get_page_title(self, page_info: Dict[str, Any]) -> str:
        props = page_info.get("properties", {})
        for _, prop in props.items():
            if prop.get("type") == "title":
                title_list = prop.get("title", [])
                if title_list:
                    return "".join(t.get("plain_text", "") for t in title_list)
        return "Untitled Page"

    def _get_db_title(self, db_info: Dict[str, Any]) -> str:
        title_list = db_info.get("title", [])
        if title_list:
            return "".join(t.get("plain_text", "") for t in title_list)
        return "Untitled Database"

    def _extract_blocks_and_children(self, block_id: str):
        """ブロック直下の子ブロック群をページネーション対応で全件取得し、
        child_page / child_database を子ノードとして再帰抽出する。"""
        blocks = []
        child_nodes: List[PageNode] = []

        cursor = None
        while True:
            res = _retry_notion_call(lambda: self.notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100))
            results = res.get("results", [])

            for b in results:
                b_type = b.get("type")

                if b_type == "child_page":
                    # 子ページを発見
                    child_id = b["id"]
                    child_title = b.get("child_page", {}).get("title", "Untitled Page")
                    logger.info(f"  Found child page: [{child_title}] ({child_id})")
                    child_node = self.extract_tree(child_id)
                    child_nodes.append(child_node)
                    # 本文内のリンクブロックとしても保持する
                    blocks.append(b)

                elif b_type == "child_database":
                    db_id = b["id"]
                    try:
                        # 1. 通常のデータベース取得を試行 (2022-06-28)
                        db_node = self._extract_database(db_id)
                        child_nodes.append(db_node)
                        b["database_title"] = db_node.title
                        blocks.append(b)
                        logger.info(f"  Found and extracted regular database: [{db_node.title}] ({db_id})")
                    except Exception as e:
                        # 2. "linked database" の場合、2025-09-03 Views APIでリンク先とビュー定義を抽出
                        err_str = str(e)
                        if "linked database" in err_str.lower():
                            logger.info(f"  Detected linked database view: ({db_id})")
                            try:
                                headers_v2 = {
                                    "Authorization": f"Bearer {self.token}",
                                    "Notion-Version": "2025-09-03",
                                    "Content-Type": "application/json",
                                }
                                db_meta_res = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=headers_v2, timeout=15)
                                if db_meta_res.ok:
                                    db_meta = db_meta_res.json()
                                    data_sources = db_meta.get("data_sources", [])
                                    views_info = []
                                    v_res = requests.get(f"https://api.notion.com/v1/views?database_id={db_id}", headers=headers_v2, timeout=15)
                                    if v_res.ok:
                                        for v in v_res.json().get("results", []):
                                            vid = v["id"]
                                            v_detail = requests.get(f"https://api.notion.com/v1/views/{vid}", headers=headers_v2, timeout=15)
                                            if v_detail.ok:
                                                vd = v_detail.json()
                                                views_info.append({
                                                    "name": vd.get("name"),
                                                    "type": vd.get("type"),
                                                    "filter": vd.get("filter"),
                                                    "configuration": vd.get("configuration", {}),
                                                })
                                    b["is_linked_database"] = True
                                    b["data_sources"] = data_sources
                                    b["views"] = views_info
                                    src_name = data_sources[0].get("name") if data_sources else "Unknown"
                                    logger.info(f"  Extracted linked view pointing to: [{src_name}] (Views: {[v['name'] for v in views_info]})")
                                    blocks.append(b)
                            except Exception as le:
                                logger.warning(f"Error inspecting linked view {db_id}: {le}")
                        else:
                            logger.warning(f"Error extracting database {db_id}: {e}")

                else:
                    # 通常のブロック
                    # ネストされたブロック（toggle, bulleted_list等）があれば再帰的に取得
                    if b.get("has_children"):
                        nested_blocks, nested_children = self._extract_blocks_and_children(b["id"])
                        b["children_blocks"] = nested_blocks
                        child_nodes.extend(nested_children)
                    blocks.append(b)

            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")

        return blocks, child_nodes

    def _extract_database(self, db_id: str) -> PageNode:
        """データベースのメタデータと全レコード（各レコードのプロパティおよび本文）を抽出する。"""
        db_info = _retry_notion_call(lambda: self.notion.databases.retrieve(database_id=db_id))
        title = self._get_db_title(db_info)
        schema = db_info.get("properties", {})

        from src.icon_mapper import resolve_notion_icon_to_emoji

        db_icon_obj = db_info.get("icon") or {}
        db_icon = resolve_notion_icon_to_emoji(db_icon_obj)

        db_node = PageNode(
            id=db_id,
            title=title,
            node_type="database",
            icon=db_icon,
            url=db_info.get("url"),
            properties={"schema": schema},
            created_time=db_info.get("created_time"),
            created_by=db_info.get("created_by"),
            last_edited_time=db_info.get("last_edited_time"),
            last_edited_by=db_info.get("last_edited_by"),
        )

        # 全レコードを取得 (クエリ)
        cursor = None
        while True:
            query_kwargs = {"page_size": 100}
            if cursor:
                query_kwargs["start_cursor"] = cursor
            res = _retry_notion_call(lambda: self.notion.request(path=f"databases/{db_id}/query", method="POST", body=query_kwargs))
            results = res.get("results", [])

            for row in results:
                row_id = row["id"]
                row_title = self._get_page_title(row)
                row_props = self._parse_row_properties(row.get("properties", {}))

                # レコード内のブロックを取得
                row_blocks, row_children = self._extract_blocks_and_children(row_id)

                row_icon_obj = row.get("icon") or {}
                row_icon = resolve_notion_icon_to_emoji(row_icon_obj)

                row_node = PageNode(
                    id=row_id,
                    title=row_title,
                    node_type="database_row",
                    icon=row_icon,
                    url=row.get("url"),
                    properties=row_props,
                    blocks=row_blocks,
                    children=row_children,
                    created_time=row.get("created_time"),
                    created_by=row.get("created_by"),
                    last_edited_time=row.get("last_edited_time"),
                    last_edited_by=row.get("last_edited_by"),
                )
                db_node.children.append(row_node)

            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")

        logger.info(f"  Extracted database [{title}] with {len(db_node.children)} rows.")
        return db_node

    def _parse_row_properties(self, props: Dict[str, Any]) -> Dict[str, Any]:
        """Notionレコードのプロパティを型情報や色情報を含めた辞書形式に整理する。"""
        parsed = {}
        for name, p in props.items():
            p_type = p.get("type")
            if p_type == "title":
                val = "".join(t.get("plain_text", "") for t in p.get("title", []))
                parsed[name] = {"type": "title", "value": val}
            elif p_type == "rich_text":
                val = "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
                parsed[name] = {"type": "rich_text", "value": val}
            elif p_type in ("select", "status"):
                opt = p.get(p_type)
                if opt:
                    parsed[name] = {
                        "type": p_type,
                        "value": opt.get("name", ""),
                        "color": opt.get("color", "default"),
                    }
                else:
                    parsed[name] = {"type": p_type, "value": None, "color": "default"}
            elif p_type == "multi_select":
                items = [
                    {"value": opt.get("name", ""), "color": opt.get("color", "default")}
                    for opt in p.get("multi_select", [])
                ]
                parsed[name] = {"type": "multi_select", "items": items}
            elif p_type == "date":
                dt = p.get("date")
                if dt:
                    parsed[name] = {
                        "type": "date",
                        "start": dt.get("start"),
                        "end": dt.get("end"),
                        "time_zone": dt.get("time_zone"),
                    }
                else:
                    parsed[name] = {"type": "date", "start": None, "end": None}
            elif p_type == "checkbox":
                parsed[name] = {"type": "checkbox", "value": p.get("checkbox", False)}
            elif p_type == "url":
                parsed[name] = {"type": "url", "value": p.get("url")}
            elif p_type == "number":
                parsed[name] = {"type": "number", "value": p.get("number")}
            elif p_type == "email":
                parsed[name] = {"type": "email", "value": p.get("email")}
            elif p_type == "phone_number":
                parsed[name] = {"type": "phone_number", "value": p.get("phone_number")}
            elif p_type == "people":
                people_list = [
                    {"id": u.get("id"), "name": u.get("name", "Unknown User")}
                    for u in p.get("people", [])
                ]
                parsed[name] = {"type": "people", "users": people_list}
            elif p_type == "relation":
                relations = [{"id": r.get("id")} for r in p.get("relation", [])]
                parsed[name] = {"type": "relation", "relations": relations}
            elif p_type == "rollup":
                rollup_data = p.get("rollup", {})
                r_type = rollup_data.get("type")
                if r_type == "array":
                    items = []
                    for item in rollup_data.get("array", []):
                        sub_type = item.get("type")
                        if sub_type in ("select", "status"):
                            opt = item.get(sub_type, {})
                            items.append({
                                "type": sub_type,
                                "value": opt.get("name"),
                                "color": opt.get("color", "default"),
                            })
                        elif sub_type == "title":
                            val = "".join(t.get("plain_text", "") for t in item.get("title", []))
                            items.append({"type": "title", "value": val})
                        elif sub_type == "number":
                            items.append({"type": "number", "value": item.get("number")})
                        elif sub_type == "date":
                            dt = item.get("date", {})
                            items.append({"type": "date", "start": dt.get("start") if dt else None})
                        else:
                            items.append({"type": sub_type, "value": str(item.get(sub_type, ""))})
                    parsed[name] = {"type": "rollup", "rollup_type": "array", "items": items}
                elif r_type == "number":
                    parsed[name] = {"type": "rollup", "rollup_type": "number", "value": rollup_data.get("number")}
                elif r_type == "date":
                    dt = rollup_data.get("date", {})
                    parsed[name] = {"type": "rollup", "rollup_type": "date", "start": dt.get("start") if dt else None}
                else:
                    parsed[name] = {"type": "rollup", "rollup_type": r_type, "value": str(rollup_data.get(r_type, ""))}
            elif p_type == "formula":
                form_data = p.get("formula", {})
                f_type = form_data.get("type", "string")
                val = form_data.get(f_type)
                parsed[name] = {"type": "formula", "value": val}
            else:
                raw_val = p.get(p_type)
                parsed[name] = {"type": p_type or "unknown", "value": str(raw_val) if raw_val is not None else ""}
        return parsed
