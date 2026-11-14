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
from src.user_mapper import UserMappingManager

logger = logging.getLogger(__name__)


def _normalize_uuid(uid: Optional[str]) -> str:
    return (uid or "").strip().replace("-", "").lower()


class NotionApiEnricher:
    """Notion API を用いてデータベーススキーマ、ビューメタデータ、およびユーザー情報を補完するエンリッチャー。"""

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
        self.workspace_users: List[Dict[str, Any]] = []
        self.users_by_id: Dict[str, Dict[str, Any]] = {}
        self.users_by_name: Dict[str, List[Dict[str, Any]]] = {}

    def _fetch_workspace_users(self, summary: Dict[str, Any]):
        """GET /v1/users (page_size=100) を一括取得し、ローカルのID/表示名辞書を構築する。"""
        self.workspace_users = []
        self.users_by_id = {}
        self.users_by_name = {}

        cursor: Optional[str] = None
        seen_ids = set()

        try:
            while True:
                params: Dict[str, Any] = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                res = requests.get(
                    "https://api.notion.com/v1/users",
                    headers=self.headers_v1,
                    params=params,
                    timeout=15,
                )
                if res.status_code != 200:
                    err_msg = f"Failed to fetch workspace users: HTTP {res.status_code} - {getattr(res, 'text', '')}"
                    logger.warning(err_msg)
                    summary["errors"].append(err_msg)
                    break

                data = res.json() or {}
                for u in data.get("results", []):
                    if not isinstance(u, dict):
                        continue
                    uid = u.get("id", "")
                    norm_id = _normalize_uuid(uid)
                    if not norm_id or norm_id in seen_ids:
                        continue
                    seen_ids.add(norm_id)

                    p_obj = u.get("person")
                    email = p_obj.get("email", "") if isinstance(p_obj, dict) else ""
                    user_entry: Dict[str, Any] = {
                        "id": uid,
                        "name": (u.get("name") or "").strip(),
                    }
                    if email:
                        user_entry["person"] = {"email": email}
                    if u.get("type"):
                        user_entry["type"] = u["type"]

                    self.workspace_users.append(user_entry)
                    self.users_by_id[norm_id] = user_entry
                    if user_entry["name"]:
                        self.users_by_name.setdefault(user_entry["name"].lower(), []).append(user_entry)

                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                cursor = data.get("next_cursor")
        except Exception as e:
            err_msg = f"Exception fetching workspace users: {e}"
            logger.warning(err_msg)
            summary["errors"].append(err_msg)

        summary["users_fetched"] = len(self.users_by_id)

    def _enrich_user_dict(self, user_obj: Any, summary: Dict[str, Any]) -> bool:
        """
        キャッシュ済み /v1/users 辞書を使って、追加APIコール0回でユーザー情報の id / name / email を補完する。
        同姓同名（同一表示名で複数候補）の場合は、名前のみ参照の自動確定を行わない。
        """
        if not isinstance(user_obj, dict):
            return False

        changed = False
        uid = (user_obj.get("id") or "").strip()
        if uid:
            cached = self.users_by_id.get(_normalize_uuid(uid))
            if cached:
                if not user_obj.get("name") and cached.get("name"):
                    user_obj["name"] = cached["name"]
                    changed = True
                cached_email = (cached.get("person") or {}).get("email")
                if cached_email:
                    cur_person = user_obj.get("person")
                    if not isinstance(cur_person, dict) or not cur_person.get("email"):
                        user_obj.setdefault("person", {})
                        if isinstance(user_obj["person"], dict):
                            user_obj["person"]["email"] = cached_email
                            changed = True
        else:
            uname = (user_obj.get("name") or "").strip()
            if uname:
                cands = self.users_by_name.get(uname.lower(), [])
                if len(cands) == 1:
                    matched = cands[0]
                    user_obj["id"] = matched["id"]
                    changed = True
                    matched_email = (matched.get("person") or {}).get("email")
                    if matched_email:
                        cur_person = user_obj.get("person")
                        if not isinstance(cur_person, dict) or not cur_person.get("email"):
                            user_obj.setdefault("person", {})
                            if isinstance(user_obj["person"], dict):
                                user_obj["person"]["email"] = matched_email
                elif len(cands) > 1:
                    logger.debug(f"[API-ENRICH] Ambiguous homonym user name '{uname}' ({len(cands)} candidates); skipping auto-resolution.")

        if changed:
            summary["users_enriched"] = summary.get("users_enriched", 0) + 1
        return changed

    def _enrich_node_users(self, node: PageNode, summary: Dict[str, Any]):
        """単一ノードの作成者・更新者・コメント発言者・プロパティ・本文メンションをキャッシュ辞書から補完する。"""
        if getattr(node, "created_by", None):
            self._enrich_user_dict(node.created_by, summary)
        if getattr(node, "last_edited_by", None):
            self._enrich_user_dict(node.last_edited_by, summary)

        for c in getattr(node, "comments", []) or []:
            if isinstance(c, dict) and isinstance(c.get("created_by"), dict):
                self._enrich_user_dict(c["created_by"], summary)

        if isinstance(node.properties, dict):
            for p_val in node.properties.values():
                if isinstance(p_val, dict) and p_val.get("type") in ("people", "created_by", "last_edited_by"):
                    for u in p_val.get("users", []):
                        self._enrich_user_dict(u, summary)
                    if p_val.get("type") in ("created_by", "last_edited_by") and isinstance(p_val.get(p_val["type"]), dict):
                        self._enrich_user_dict(p_val[p_val["type"]], summary)

        for u in UserMappingManager._iter_block_user_mentions(getattr(node, "blocks", []) or []):
            self._enrich_user_dict(u, summary)

    def enrich_tree(self, root_node: PageNode) -> Dict[str, Any]:
        """ツリー内の全データベース・ユーザー情報をNotion APIから補完し、同姓同名警告を検知する。"""
        summary: Dict[str, Any] = {
            "databases_enriched": 0,
            "properties_mapped": 0,
            "views_enriched": 0,
            "users_fetched": 0,
            "users_enriched": 0,
            "rows_enriched": 0,
            "homonym_warnings": [],
            "errors": [],
        }

        # 1. ワークスペースユーザー一覧を最初に一括キャッシュ
        self._fetch_workspace_users(summary)

        # 2. ツリー走査（DB補完＋全ノードのユーザー補完）
        def traverse(node: PageNode):
            if node.node_type == "database":
                self._enrich_database(node, summary)
            self._enrich_node_users(node, summary)
            for child in node.children:
                traverse(child)

        traverse(root_node)

        # 3. 同姓同名検知
        summary["homonym_warnings"] = UserMappingManager.detect_homonyms(
            root_node,
            workspace_users=self.workspace_users,
        )

        logger.info(
            f"API Enrichment completed: {summary['databases_enriched']} databases enriched, "
            f"{summary['properties_mapped']} properties mapped, {summary['views_enriched']} views fetched, "
            f"{summary['users_fetched']} workspace users cached, {summary['users_enriched']} user refs enriched."
        )
        return summary

    def _enrich_database(self, db_node: PageNode, summary: Dict[str, Any]):
        """単一データベースのスキーマ・ビュー定義・行ユーザー/監査情報をAPI取得してノードへ統合する。"""
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

        # 3. DB行の people 列・作成者/更新者・日時を一括クエリ取得 (POST /v1/databases/{id}/query, page_size=100)
        if db_node.children:
            self._enrich_database_rows_via_query(formatted_id, db_node, summary)

        # 4. 各子レコード（database_row）のプロパティを正確なスキーマ型へ同期
        if schema and db_node.children:
            self._align_row_properties(db_node.children, schema)

    def _enrich_database_rows_via_query(self, formatted_id: str, db_node: PageNode, summary: Dict[str, Any]):
        """DBごとに POST /v1/databases/{id}/query (page_size=100) を実行し、行ユーザー・監査情報を一括マージする。"""
        row_by_id: Dict[str, PageNode] = {}
        row_by_title: Dict[str, List[PageNode]] = {}
        for r in db_node.children:
            nid = _normalize_uuid(r.id)
            if nid:
                row_by_id[nid] = r
            tkey = (r.title or "").strip().lower()
            if tkey:
                row_by_title.setdefault(tkey, []).append(r)

        url_query = f"https://api.notion.com/v1/databases/{formatted_id}/query"
        cursor: Optional[str] = None
        try:
            while True:
                payload: Dict[str, Any] = {"page_size": 100}
                if cursor:
                    payload["start_cursor"] = cursor
                res = requests.post(url_query, headers=self.headers_v1, json=payload, timeout=20)
                if res.status_code != 200:
                    logger.debug(f"[API-ENRICH] Database query skipped/failed for {db_node.title}: HTTP {res.status_code}")
                    break

                data = res.json() or {}
                for api_row in data.get("results", []):
                    if not isinstance(api_row, dict):
                        continue
                    api_id = _normalize_uuid(api_row.get("id"))
                    target_row = row_by_id.get(api_id)

                    if target_row is None:
                        # タイトルフォールバック照合
                        api_title = ""
                        for p_val in (api_row.get("properties") or {}).values():
                            if isinstance(p_val, dict) and p_val.get("type") == "title":
                                api_title = "".join(t.get("plain_text", "") for t in p_val.get("title", []))
                                break
                        if api_title:
                            cands = row_by_title.get(api_title.strip().lower(), [])
                            if len(cands) == 1:
                                target_row = cands[0]

                    if target_row is None:
                        continue

                    if not target_row.created_time and api_row.get("created_time"):
                        target_row.created_time = api_row["created_time"]
                    if not target_row.last_edited_time and api_row.get("last_edited_time"):
                        target_row.last_edited_time = api_row["last_edited_time"]

                    if api_row.get("created_by"):
                        cb = dict(api_row["created_by"])
                        self._enrich_user_dict(cb, summary)
                        target_row.created_by = cb
                    if api_row.get("last_edited_by"):
                        leb = dict(api_row["last_edited_by"])
                        self._enrich_user_dict(leb, summary)
                        target_row.last_edited_by = leb

                    api_props = api_row.get("properties") or {}
                    for col_name, col_val in api_props.items():
                        if not isinstance(col_val, dict):
                            continue
                        p_type = col_val.get("type")
                        if p_type == "people":
                            users_enriched = []
                            for raw_u in col_val.get("people", []):
                                if isinstance(raw_u, dict):
                                    u_copy = dict(raw_u)
                                    self._enrich_user_dict(u_copy, summary)
                                    users_enriched.append(u_copy)
                            target_row.properties[col_name] = {
                                "type": "people",
                                "users": users_enriched,
                                "value": ", ".join(u.get("name", "") for u in users_enriched if u.get("name")),
                            }
                        elif p_type in ("created_by", "last_edited_by"):
                            raw_u = col_val.get(p_type)
                            users_enriched = []
                            u_copy: Dict[str, Any] = {}
                            if isinstance(raw_u, dict):
                                u_copy = dict(raw_u)
                                self._enrich_user_dict(u_copy, summary)
                                users_enriched.append(u_copy)
                            target_row.properties[col_name] = {
                                "type": p_type,
                                "users": users_enriched,
                                p_type: u_copy,
                                "value": u_copy.get("name", ""),
                            }

                    summary["rows_enriched"] = summary.get("rows_enriched", 0) + 1

                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                cursor = data.get("next_cursor")
        except Exception as qe:
            logger.debug(f"[API-ENRICH] Database query skipped or failed for {db_node.title}: {qe}")

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

                elif p_type == "people":
                    users_list: List[Dict[str, Any]] = []
                    if isinstance(existing, dict) and isinstance(existing.get("users"), list):
                        for u in existing["users"]:
                            if isinstance(u, dict):
                                users_list.append(u)
                            elif isinstance(u, str) and u.strip():
                                users_list.append({"name": u.strip().lstrip("@")})
                    elif isinstance(raw_val, list):
                        for u in raw_val:
                            if isinstance(u, dict):
                                users_list.append(u)
                            elif isinstance(u, str) and u.strip():
                                users_list.append({"name": u.strip().lstrip("@")})
                    elif isinstance(raw_val, str) and raw_val.strip():
                        for part in raw_val.split(","):
                            name_s = part.strip().lstrip("@").strip()
                            if name_s:
                                users_list.append({"name": name_s})
                    aligned_props[col_name] = {
                        "type": "people",
                        "users": users_list,
                        "value": ", ".join(u.get("name", "") for u in users_list if u.get("name")),
                    }

                elif p_type in ("created_by", "last_edited_by"):
                    users_list = []
                    if isinstance(existing, dict):
                        if isinstance(existing.get("users"), list) and existing["users"]:
                            users_list = [u for u in existing["users"] if isinstance(u, dict)]
                        elif isinstance(existing.get(p_type), dict):
                            users_list = [existing[p_type]]
                    if not users_list and isinstance(raw_val, str) and raw_val.strip():
                        users_list = [{"name": raw_val.strip().lstrip("@")}]
                    aligned_props[col_name] = {
                        "type": p_type,
                        "users": users_list,
                        p_type: users_list[0] if users_list else {},
                        "value": ", ".join(u.get("name", "") for u in users_list if u.get("name")),
                    }

                else:
                    aligned_props[col_name] = {
                        "type": p_type,
                        "value": raw_val,
                    }

            row.properties.update(aligned_props)

