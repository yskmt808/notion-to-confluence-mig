"""
Notion API ハイブリッド補完モード (API Enrichment Mode / Task B.3)。
HTML/CSVエクスポートから高速解析したPageNodeツリーに対し、Notion APIをピンポイントで呼び出して
データベースの正確なプロパティ型スキーマ（select, multi_select, status, relation等）および
ビュー定義（Board/カンバン、Gallery、Table、Group by設定）を高精度に復元・マージします。
"""

import logging
import requests
from typing import Dict, Any, List, Optional
from src.extractor import PageNode

logger = logging.getLogger(__name__)


class NotionApiEnricher:
    """Notion API を用いてデータベーススキーマとビューメタデータを補完するエンリッチャー。"""

    def __init__(self, token: str):
        self.token = token
        self.headers_v1 = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        self.headers_v2 = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        }

    def enrich_tree(self, root_node: PageNode) -> Dict[str, Any]:
        """ツリー内の全データベースを再帰検索し、Notion APIから正確なプロパティ定義とビュー属性をマージする。"""
        summary = {
            "databases_enriched": 0,
            "properties_mapped": 0,
            "views_enriched": 0,
            "errors": [],
        }

        def traverse(node: PageNode):
            if node.node_type == "database":
                self._enrich_database(node, summary)
            for child in node.children:
                traverse(child)

        traverse(root_node)
        logger.info(
            f"API Enrichment completed: {summary['databases_enriched']} databases enriched, "
            f"{summary['properties_mapped']} properties mapped, {summary['views_enriched']} views fetched."
        )
        return summary

    def _enrich_database(self, db_node: PageNode, summary: Dict[str, Any]):
        """単一データベースのスキーマとビュー定義をAPI取得してノードへ統合する。"""
        db_id = db_node.id.replace("-", "")
        # 36桁UUID形式に変換
        if len(db_id) == 32:
            formatted_id = f"{db_id[:8]}-{db_id[8:12]}-{db_id[12:16]}-{db_id[16:20]}-{db_id[20:]}"
        else:
            formatted_id = db_node.id

        logger.info(f"[API-ENRICH] Fetching exact schema for database [{db_node.title}] ({formatted_id})")

        # 1. データベースのプロパティスキーマ取得 (2022-06-28 API)
        url_db = f"https://api.notion.com/v1/databases/{formatted_id}"
        schema = {}
        try:
            res = requests.get(url_db, headers=self.headers_v1, timeout=15)
            if res.status_code == 200:
                data = res.json()
                schema = data.get("properties", {})
                db_node.properties["schema"] = schema
                summary["databases_enriched"] += 1
                summary["properties_mapped"] += len(schema)
                logger.info(f"[API-ENRICH] Mapped {len(schema)} exact properties for [{db_node.title}]: {list(schema.keys())}")
            else:
                err_msg = f"Failed to retrieve DB schema for {db_node.title}: HTTP {res.status_code} - {res.text}"
                logger.warning(err_msg)
                summary["errors"].append(err_msg)
        except Exception as e:
            err_msg = f"Exception fetching DB schema for {db_node.title}: {e}"
            logger.warning(err_msg)
            summary["errors"].append(err_msg)

        # 2. ビュー定義の取得 (2025-09-03 Views API)
        views_info = []
        try:
            url_views = f"https://api.notion.com/v1/views?database_id={formatted_id}"
            res_v = requests.get(url_views, headers=self.headers_v2, timeout=15)
            if res_v.status_code == 200:
                raw_views = res_v.json().get("results", [])
                for v in raw_views:
                    vid = v.get("id")
                    if vid:
                        v_detail = requests.get(f"https://api.notion.com/v1/views/{vid}", headers=self.headers_v2, timeout=15)
                        if v_detail.ok:
                            vd = v_detail.json()
                            views_info.append({
                                "id": vd.get("id"),
                                "name": vd.get("name"),
                                "type": vd.get("type"),
                                "filter": vd.get("filter"),
                                "sorts": vd.get("sorts"),
                                "configuration": vd.get("configuration", {}),
                            })
                if views_info:
                    db_node.properties["views_config"] = views_info
                    summary["views_enriched"] += len(views_info)
                    logger.info(f"[API-ENRICH] Retrieved {len(views_info)} view configs for [{db_node.title}]: {[v['name'] for v in views_info]}")
        except Exception as ve:
            logger.debug(f"[API-ENRICH] Views API skipped or not available for {db_node.title}: {ve}")

        # 3. 各子レコード（database_row）のプロパティを正確なスキーマ型へ同期
        if schema and db_node.children:
            self._align_row_properties(db_node.children, schema)

    def _align_row_properties(self, rows: List[PageNode], schema: Dict[str, Any]):
        """子レコードの各プロパティ値を、APIから取得した正確なスキーマ定義に整合させる。"""
        for row in rows:
            aligned_props = {}
            for col_name, col_meta in schema.items():
                p_type = col_meta.get("type", "rich_text")
                existing = row.properties.get(col_name)

                if existing is None:
                    continue

                raw_val = existing.get("value") if isinstance(existing, dict) else existing

                # スキーマ型に応じた正規化
                if p_type == "multi_select":
                    # カンマ区切りテキストやリストを正規化
                    if isinstance(raw_val, list):
                        items = raw_val
                    elif isinstance(raw_val, str) and raw_val:
                        items = [s.strip() for s in raw_val.split(",") if s.strip()]
                    else:
                        items = []
                    item_dicts = [{"name": it if isinstance(it, str) else it.get("name", "")} for it in items]
                    aligned_props[col_name] = {
                        "type": "multi_select",
                        "multi_select": item_dicts,
                        "value": item_dicts,
                    }

                elif p_type in ("select", "status"):
                    val_str = str(raw_val) if raw_val is not None else ""
                    aligned_props[col_name] = {
                        "type": "select",
                        "select": {"name": val_str},
                        "value": val_str,
                    }

                elif p_type == "checkbox":
                    is_checked = raw_val in (True, "True", "true", "Yes", "yes", "1")
                    aligned_props[col_name] = {
                        "type": "checkbox",
                        "checkbox": is_checked,
                        "value": is_checked,
                    }

                elif p_type == "date":
                    aligned_props[col_name] = {
                        "type": "date",
                        "date": {"start": str(raw_val) if raw_val else None},
                        "value": str(raw_val) if raw_val else None,
                    }

                elif p_type == "number":
                    aligned_props[col_name] = {
                        "type": "number",
                        "number": raw_val,
                        "value": raw_val,
                    }

                elif p_type == "relation":
                    aligned_props[col_name] = existing if isinstance(existing, dict) and existing.get("type") == "relation" else {
                        "type": "relation",
                        "relation": [],
                        "value": raw_val,
                    }

                else:
                    aligned_props[col_name] = {
                        "type": p_type,
                        "value": raw_val,
                    }

            row.properties.update(aligned_props)

