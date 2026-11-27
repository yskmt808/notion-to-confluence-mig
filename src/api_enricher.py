"""
Notion API ハイブリッド補完モード (API Enrichment Mode / Task B.3)。
HTML/CSVエクスポートから高速解析したPageNodeツリーに対し、Notion APIをピンポイントで呼び出して
データベースの正確なプロパティ型スキーマ（select, multi_select, status, relation等）および
ビュー定義（Board/カンバン、Gallery、Table、Group by設定）を高精度に復元・マージします。
"""

import copy
import datetime
import json
import logging
import os
import re
import requests
from typing import Dict, Any, List, Optional, Set, Tuple
from src.extractor import (
    PageNode,
    collect_external_synced_blocks_from_blocks,
    mark_unresolved_external_references,
    normalize_notion_block,
)
from src.user_mapper import UserMappingManager

logger = logging.getLogger(__name__)

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _normalize_uuid(uid: Optional[str]) -> str:
    return (uid or "").strip().replace("-", "").lower()


def _format_uuid_36(uid: Optional[str]) -> Optional[str]:
    norm = _normalize_uuid(uid)
    if not _HEX32_RE.match(norm):
        return None
    return f"{norm[:8]}-{norm[8:12]}-{norm[12:16]}-{norm[16:20]}-{norm[20:]}"


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
        self._block_children_cache: Dict[str, Tuple[int, List[Dict[str, Any]]]] = {}

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
                if res.status_code in (401, 403):
                    logger.info(
                        f"[API-ENRICH] Skipping workspace users list (/v1/users returned HTTP {res.status_code}; "
                        "token lacks user-list permission or is a user token)."
                    )
                    break
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

    def enrich_tree(self, root_node: PageNode, dump_path: Optional[str] = None) -> Dict[str, Any]:
        """ツリー内の全データベース・ユーザー情報をNotion APIから補完し、同姓同名警告を検知する。"""
        summary: Dict[str, Any] = {
            "databases_enriched": 0,
            "properties_mapped": 0,
            "views_enriched": 0,
            "users_fetched": 0,
            "users_enriched": 0,
            "rows_enriched": 0,
            "synced_blocks_resolved": 0,
            "empty_synced_blocks_removed": 0,
            "homonym_warnings": [],
            "errors": [],
        }

        # 1. ワークスペースユーザー一覧を最初に一括キャッシュ
        self._fetch_workspace_users(summary)

        # 2. ツリー走査（DB補完＋未展開同期ブロックのピンポイント解決＋全ノードのユーザー補完）
        db_nodes_by_norm_id: Dict[str, PageNode] = {}
        db_nodes_order: List[PageNode] = []

        def traverse(node: PageNode):
            if node.node_type == "database":
                self._enrich_database(node, summary)
                norm_db = (node.id or "").replace("-", "").lower()
                if norm_db:
                    db_nodes_by_norm_id[norm_db] = node
                db_nodes_order.append(node)
            self._enrich_node_external_synced_blocks(node, summary)
            self._enrich_node_users(node, summary)
            for child in node.children:
                traverse(child)

        traverse(root_node)

        # 2.5. ページ内に埋め込まれた Linked View (child_database ブロック) 側のビュー定義も取得・統合
        dumped_linked_views = self._enrich_linked_database_views(root_node, db_nodes_by_norm_id, summary)

        # 2.6. APIで新規展開された同期ブロック内のリンク・DB参照を含め、内部/外部参照判定を再実行
        mark_unresolved_external_references(root_node)

        # 3. 同姓同名検知
        summary["homonym_warnings"] = UserMappingManager.detect_homonyms(
            root_node,
            workspace_users=self.workspace_users,
        )

        if dump_path:
            dumped_databases: List[Dict[str, Any]] = []
            for db_n in db_nodes_order:
                schema = (db_n.properties or {}).get("schema") or {}
                views_cfg = (db_n.properties or {}).get("views_config") or []
                dumped_databases.append({
                    "database_id": db_n.id,
                    "title": db_n.title,
                    "row_count": len(db_n.children or []),
                    "column_order": list(schema.keys()),
                    "properties": schema,
                    "views": views_cfg,
                })
            self._write_enrichment_dump(dump_path, summary, dumped_databases, linked_database_views=dumped_linked_views)

        logger.info(
            f"API Enrichment completed: {summary['databases_enriched']} databases enriched, "
            f"{summary['properties_mapped']} properties mapped, {summary['views_enriched']} views fetched, "
            f"{summary['users_fetched']} workspace users cached, {summary['users_enriched']} user refs enriched."
        )
        return summary

    @staticmethod
    def _write_enrichment_dump(
        dump_path: str,
        summary: Dict[str, Any],
        databases: List[Dict[str, Any]],
        linked_database_views: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """取得したDBプロパティ・View定義の詳細をJSONファイルに出力する。"""
        parent_dir = os.path.dirname(os.path.abspath(dump_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        payload = {
            "dumped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "summary": summary,
            "databases": databases,
            "linked_database_views": linked_database_views or [],
        }
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"[API-ENRICH] Dumped API enrichment details ({len(databases)} DBs) to: {dump_path}")

    @staticmethod
    def _format_uuid(raw_id: str) -> str:
        clean = (raw_id or "").replace("-", "")
        if len(clean) == 32:
            return f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
        return raw_id

    def _fetch_views_for_database_id(self, raw_id: str, label: str = "") -> List[Dict[str, Any]]:
        """指定した database_id / Linked View block_id / view_id に対して Notion Views API を呼び出す。"""
        formatted_id = self._format_uuid(raw_id)
        views_info: List[Dict[str, Any]] = []

        def _to_view_dict(vd: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "id": vd.get("id"),
                "name": vd.get("name"),
                "type": vd.get("type"),
                "data_source_id": vd.get("data_source_id"),
                "filter": vd.get("filter"),
                "quick_filters": vd.get("quick_filters"),
                "sorts": vd.get("sorts"),
                "configuration": vd.get("configuration", {}),
            }

        try:
            url_views = f"https://api.notion.com/v1/views?database_id={formatted_id}"
            res_v = requests.get(url_views, headers=self.headers_v2, timeout=15)
            if res_v.status_code == 200:
                raw_views = res_v.json().get("results", [])
                if not raw_views:
                    logger.info(
                        f"[API-ENRICH] Views API returned 0 results for {label or formatted_id} "
                        f"(query database_id={formatted_id})"
                    )
                for v in raw_views:
                    vid = v.get("id")
                    if vid:
                        v_detail = requests.get(f"https://api.notion.com/v1/views/{vid}", headers=self.headers_v2, timeout=15)
                        if v_detail.ok:
                            views_info.append(_to_view_dict(v_detail.json()))
                        else:
                            logger.warning(
                                f"[API-ENRICH] Failed to fetch view detail {vid} for {label or formatted_id}: "
                                f"HTTP {v_detail.status_code}"
                            )
            else:
                logger.info(
                    f"[API-ENRICH] Views API query by database_id={formatted_id} returned HTTP {res_v.status_code} "
                    f"for {label or formatted_id}"
                )

            if not views_info and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", formatted_id):
                res_single = requests.get(f"https://api.notion.com/v1/views/{formatted_id}", headers=self.headers_v2, timeout=15)
                if res_single.ok:
                    vd = res_single.json()
                    if isinstance(vd, dict) and vd.get("id"):
                        views_info.append(_to_view_dict(vd))
                        logger.info(
                            f"[API-ENRICH] Resolved view directly via /v1/views/{formatted_id} "
                            f"for {label or formatted_id}: name={vd.get('name')!r}, type={vd.get('type')!r}"
                        )
                else:
                    logger.info(
                        f"[API-ENRICH] Direct /v1/views/{formatted_id} returned HTTP {res_single.status_code} "
                        f"for {label or formatted_id}"
                    )
        except Exception as ve:
            logger.warning(f"[API-ENRICH] Views API exception for {label or formatted_id}: {ve}")
        return views_info

    @staticmethod
    def _build_view_config_signature(vc: Dict[str, Any]) -> str:
        """ビューの実質的設定（レイアウト・フィルタ・ソート・非表示列・group_by）から集約用の比較キーを生成する。"""
        v_type = (vc.get("type") or "table").strip().lower()
        effective_layout = v_type if v_type in ("board", "gallery") else "table"
        cfg = vc.get("configuration") or {}
        hidden_cols = sorted(
            str(cp.get("property_name") or cp.get("property_id") or "").strip()
            for cp in (cfg.get("properties") or [])
            if isinstance(cp, dict) and cp.get("visible") is False
        )
        sig_obj = {
            "layout": effective_layout,
            "filter": vc.get("filter") or None,
            "quick_filters": vc.get("quick_filters") or None,
            "sorts": vc.get("sorts") or None,
            "hidden": hidden_cols,
            "group_by": (cfg.get("group_by") or None) if effective_layout == "board" else None,
        }
        return json.dumps(sig_obj, sort_keys=True, ensure_ascii=False)

    def _enrich_linked_database_views(
        self,
        root_node: PageNode,
        db_nodes_by_norm_id: Dict[str, PageNode],
        summary: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """ページ本文中の Linked View (child_database ブロック) からビュー定義を取得し、埋め込みと元DBへ統合する。"""
        linked_dumps: List[Dict[str, Any]] = []
        fetched_block_cache: Dict[str, List[Dict[str, Any]]] = {}

        def _walk_blocks(blocks: List[Dict[str, Any]], page_title: str) -> None:
            for b in blocks or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "child_database":
                    block_id = str(b.get("id") or "").strip()
                    target_id = str(
                        b.get("database_id")
                        or ((b.get("data_sources") or [{}])[0].get("id") if b.get("data_sources") else "")
                        or ""
                    ).strip()
                    if b.get("_is_unresolved_external_db"):
                        logger.info(
                            f"[API-ENRICH] Skipping unresolved external child_database on [{page_title}] "
                            f"(block={block_id}, target={target_id}, views={b.get('views')})"
                        )
                        continue
                    norm_bid = block_id.replace("-", "").lower()
                    norm_tid = target_id.replace("-", "").lower()
                    if not norm_bid or not norm_tid or norm_bid == norm_tid:
                        logger.info(
                            f"[API-ENRICH] Skipping child_database linked-view lookup on [{page_title}] "
                            f"(block={block_id}, target={target_id}, is_linked={b.get('is_linked_database')}, "
                            f"views={b.get('views')})"
                        )
                    else:
                        if norm_bid not in fetched_block_cache:
                            fetched_block_cache[norm_bid] = self._fetch_views_for_database_id(
                                block_id, label=f"linked view on [{page_title}]"
                            )
                        linked_views = fetched_block_cache[norm_bid]
                        if not linked_views:
                            logger.info(
                                f"[API-ENRICH] No linked views retrieved on [{page_title}] "
                                f"(block={block_id}, target={target_id}, orig_views={b.get('views')})"
                            )
                        if linked_views:
                            summary["views_enriched"] += len(linked_views)
                            orig_views = b.get("views") or []
                            orig_hint_name = (
                                str(orig_views[0].get("name") or "").strip()
                                if orig_views and isinstance(orig_views[0], dict)
                                else ""
                            )
                            target_db = db_nodes_by_norm_id.get(norm_tid)
                            resolved_block_views: List[Dict[str, Any]] = []

                            if target_db is not None:
                                existing_cfg = target_db.properties.setdefault("views_config", [])
                                existing_names = {
                                    (ev.get("name") or "").strip()
                                    for ev in existing_cfg
                                    if isinstance(ev, dict) and ev.get("name")
                                }
                                existing_ids = {
                                    ev.get("id")
                                    for ev in existing_cfg
                                    if isinstance(ev, dict) and ev.get("id")
                                }
                                sig_to_name: Dict[str, str] = {}
                                for ev in existing_cfg:
                                    if isinstance(ev, dict) and ev.get("name"):
                                        ev_sig = self._build_view_config_signature(ev)
                                        if ev_sig not in sig_to_name:
                                            ev_n = (ev.get("name") or "").strip()
                                            # Confluence のデフォルトビューと完全一致する場合は「すべてのデータ」へ集約
                                            if (
                                                ev_n in ("Untitled", "Default view")
                                                and not ev.get("filter")
                                                and not ev.get("quick_filters")
                                                and not ev.get("sorts")
                                                and (ev.get("type") or "table").strip().lower() not in ("board", "gallery")
                                                and not any(
                                                    isinstance(cp, dict) and cp.get("visible") is False
                                                    for cp in ((ev.get("configuration") or {}).get("properties") or [])
                                                )
                                            ):
                                                sig_to_name[ev_sig] = "すべてのデータ"
                                            else:
                                                sig_to_name[ev_sig] = ev_n
                                db_title = (target_db.title or "").strip()
                                for lv_idx, lv in enumerate(linked_views):
                                    raw_name = (lv.get("name") or "").strip()
                                    lv_sig = self._build_view_config_signature(lv)
                                    if lv.get("id") and lv.get("id") in existing_ids:
                                        existing_match = next(
                                            (ev for ev in existing_cfg if isinstance(ev, dict) and ev.get("id") == lv.get("id")),
                                            None,
                                        )
                                        assigned_name = (existing_match.get("name") if existing_match else raw_name) or raw_name
                                    elif lv_sig in sig_to_name:
                                        assigned_name = sig_to_name[lv_sig]
                                        logger.debug(
                                            f"[API-ENRICH] Deduplicated linked view on [{page_title}] (raw={raw_name!r}) -> reusing [{assigned_name}]"
                                        )
                                    else:
                                        if raw_name in ("", "Untitled", "Default view"):
                                            if lv_idx == 0 and orig_hint_name and orig_hint_name not in ("Untitled", "Default view", db_title):
                                                base_name = orig_hint_name
                                            else:
                                                base_name = page_title or "Embedded View"
                                        elif raw_name not in existing_names:
                                            base_name = raw_name
                                        else:
                                            base_name = f"{page_title} - {raw_name}" if page_title else raw_name

                                        base_name = re.sub(r"[\r\n]+", " ", base_name).replace(",", "、").strip() or "Embedded View"
                                        assigned_name = base_name
                                        if assigned_name in existing_names:
                                            suffix = 2
                                            while f"{base_name} ({suffix})" in existing_names:
                                                suffix += 1
                                            assigned_name = f"{base_name} ({suffix})"

                                        merged = dict(lv)
                                        merged["name"] = assigned_name
                                        merged["source_block_id"] = block_id
                                        if page_title:
                                            merged["source_page_title"] = page_title
                                        existing_cfg.append(merged)
                                        existing_names.add(assigned_name)
                                        sig_to_name[lv_sig] = assigned_name
                                        if lv.get("id"):
                                            existing_ids.add(lv.get("id"))

                                    resolved_block_views.append({
                                        "id": lv.get("id"),
                                        "name": assigned_name,
                                        "type": lv.get("type") or "table",
                                        "configuration": lv.get("configuration") or {},
                                    })
                            else:
                                resolved_block_views = [
                                    {
                                        "id": lv.get("id"),
                                        "name": lv.get("name"),
                                        "type": lv.get("type") or "table",
                                        "configuration": lv.get("configuration") or {},
                                    }
                                    for lv in linked_views
                                ]

                            b["views"] = resolved_block_views
                            linked_dumps.append({
                                "page_title": page_title,
                                "block_id": block_id,
                                "target_database_id": target_id,
                                "views": resolved_block_views,
                            })
                            logger.info(
                                f"[API-ENRICH] Retrieved {len(linked_views)} linked view(s) on [{page_title}] "
                                f"(block={block_id}): {[rv.get('name') for rv in resolved_block_views]}"
                            )
                if b.get("children_blocks"):
                    _walk_blocks(b["children_blocks"], page_title)

        def _walk_nodes(node: PageNode) -> None:
            if getattr(node, "blocks", None):
                _walk_blocks(node.blocks, node.title or "")
            for c in getattr(node, "children", None) or []:
                _walk_nodes(c)

        _walk_nodes(root_node)
        return linked_dumps

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
        existing_col_order: List[str] = []
        if db_node.properties.get("schema"):
            existing_col_order = list(db_node.properties["schema"].keys())
        elif db_node.children:
            for c in db_node.children:
                for pk in (c.properties or {}).keys():
                    if pk not in existing_col_order:
                        existing_col_order.append(pk)

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
                                "quick_filters": vd.get("quick_filters"),
                                "sorts": vd.get("sorts"),
                                "configuration": vd.get("configuration", {}),
                            })
                if views_info:
                    db_node.properties["views_config"] = views_info
                    summary["views_enriched"] += len(views_info)
                    logger.info(f"[API-ENRICH] Retrieved {len(views_info)} view configs for [{db_node.title}]: {[v['name'] for v in views_info]}")
        except Exception as ve:
            logger.debug(f"[API-ENRICH] Views API skipped or not available for {db_node.title}: {ve}")

        # 2.5. Notion上の見た目（ビューのproperties順、または既存のHTML/CSV列順）に合わせてschemaキー順を整列
        if schema:
            view_prop_order: List[str] = []
            for vc in views_info:
                cfg_props = (vc.get("configuration") or {}).get("properties") or []
                if isinstance(cfg_props, list) and cfg_props:
                    for cp in cfg_props:
                        if isinstance(cp, dict) and cp.get("property_name"):
                            pname = cp["property_name"]
                            if pname not in view_prop_order:
                                view_prop_order.append(pname)
                    if view_prop_order:
                        break
            preferred_order = view_prop_order or existing_col_order
            if preferred_order:
                ordered_schema: Dict[str, Any] = {}
                for pname in preferred_order:
                    if pname in schema and pname not in ordered_schema:
                        ordered_schema[pname] = schema[pname]
                for pname, pinfo in schema.items():
                    if pname not in ordered_schema:
                        ordered_schema[pname] = pinfo
                schema = ordered_schema
                db_node.properties["schema"] = schema

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
                        elif p_type in ("created_time", "last_edited_time"):
                            ts_val = col_val.get(p_type)
                            if ts_val:
                                target_row.properties[col_name] = {
                                    "type": p_type,
                                    p_type: ts_val,
                                    "start": ts_val,
                                    "value": ts_val,
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
                    if p_type in ("created_time", "last_edited_time"):
                        fallback_ts = getattr(row, p_type, None)
                        if fallback_ts:
                            aligned_props[col_name] = {
                                "type": p_type,
                                p_type: str(fallback_ts),
                                "start": str(fallback_ts),
                                "value": str(fallback_ts),
                            }
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
                        "type": p_type,
                        p_type: {"name": val_str},
                        "value": val_str,
                    }

                elif p_type == "checkbox":
                    is_checked = raw_val in (True, "True", "true", "Yes", "yes", "1", "☑", "✅")
                    aligned_props[col_name] = {
                        "type": "checkbox",
                        "checkbox": is_checked,
                        "value": is_checked,
                    }

                elif p_type == "date":
                    raw_start = (
                        existing.get("start")
                        or (existing.get("date") or {}).get("start")
                        or raw_val
                    ) if isinstance(existing, dict) else raw_val
                    raw_end = (
                        existing.get("end")
                        or (existing.get("date") or {}).get("end")
                    ) if isinstance(existing, dict) else None
                    date_obj: Dict[str, Any] = {"start": str(raw_start) if raw_start else None}
                    if raw_end:
                        date_obj["end"] = str(raw_end)
                    aligned_date: Dict[str, Any] = {
                        "type": "date",
                        "date": date_obj,
                        "start": str(raw_start) if raw_start else None,
                        "value": str(raw_start) if raw_start else None,
                    }
                    if raw_end:
                        aligned_date["end"] = str(raw_end)
                    aligned_props[col_name] = aligned_date

                elif p_type in ("created_time", "last_edited_time"):
                    ts_val = None
                    if isinstance(existing, dict):
                        ts_val = existing.get(p_type) or existing.get("start") or existing.get("value")
                    elif existing is not None:
                        ts_val = existing
                    if not ts_val:
                        ts_val = getattr(row, p_type, None)
                    ts_str = str(ts_val) if ts_val else None
                    aligned_props[col_name] = {
                        "type": p_type,
                        p_type: ts_str,
                        "start": ts_str,
                        "value": ts_str,
                    }

                elif p_type == "url":
                    u_val = (existing.get("url") or raw_val) if isinstance(existing, dict) else raw_val
                    aligned_props[col_name] = {
                        "type": "url",
                        "url": u_val,
                        "value": u_val,
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

    def _fetch_block_children_cached(self, block_id: str) -> Tuple[int, List[Dict[str, Any]]]:
        """Notion API から指定ブロックの子要素一覧をキャッシュ付きで取得する。"""
        formatted = _format_uuid_36(block_id)
        if not formatted:
            return 400, []
        if formatted in self._block_children_cache:
            return self._block_children_cache[formatted]

        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        last_status = 200
        try:
            while True:
                params: Dict[str, Any] = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                res = requests.get(
                    f"https://api.notion.com/v1/blocks/{formatted}/children",
                    headers=self.headers_v1,
                    params=params,
                    timeout=15,
                )
                last_status = res.status_code
                if res.status_code != 200:
                    break
                data = res.json() or {}
                results.extend(b for b in data.get("results", []) if isinstance(b, dict))
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                cursor = data.get("next_cursor")
        except Exception as e:
            logger.debug(f"[API-ENRICH] Error fetching children for block {formatted}: {e}")
            last_status = 500

        out = (last_status, results)
        self._block_children_cache[formatted] = out
        return out

    def _resolve_synced_block_leaf_children(
        self,
        api_sb: Dict[str, Any],
        visited: Optional[Set[str]] = None,
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        """synced_block の synced_from および入れ子 synced_block を多段で辿り、実体子ブロック群を返す。
        戻り値: (status: 'resolved' | 'inaccessible' | 'error', target_id, leaf_blocks)
        """
        if visited is None:
            visited = set()
        sb_data = api_sb.get("synced_block") or {}
        synced_from = sb_data.get("synced_from") if isinstance(sb_data, dict) else None
        raw_target = (synced_from.get("block_id") if isinstance(synced_from, dict) else None) or api_sb.get("id") or ""
        norm_target = _normalize_uuid(raw_target)
        if not norm_target or norm_target in visited:
            return "resolved", raw_target, []

        new_visited = set(visited)
        new_visited.add(norm_target)

        status_code, children = self._fetch_block_children_cached(raw_target)
        if status_code in (403, 404):
            return "inaccessible", raw_target, []
        if status_code != 200:
            return "error", raw_target, []

        def _expand_nested_block(cb: Dict[str, Any], cur_visited: Set[str]) -> List[Dict[str, Any]]:
            cb_type = cb.get("type")
            if cb_type == "synced_block":
                sub_status, sub_target, sub_leaves = self._resolve_synced_block_leaf_children(cb, cur_visited)
                if sub_status in ("inaccessible", "error"):
                    raise RuntimeError(f"{sub_status}:{sub_target}")
                return sub_leaves
            cb_copy = copy.deepcopy(cb)
            if cb_copy.get("has_children") and not cb_copy.get("children_blocks"):
                c_stat, sub_ch = self._fetch_block_children_cached(cb_copy.get("id", ""))
                if c_stat == 200 and sub_ch:
                    expanded_sub: List[Dict[str, Any]] = []
                    for sc in sub_ch:
                        expanded_sub.extend(_expand_nested_block(sc, cur_visited))
                    cb_copy["children_blocks"] = expanded_sub
            normalize_notion_block(cb_copy)
            cb_copy["_via_synced_block"] = True
            return [cb_copy]

        leaf_blocks: List[Dict[str, Any]] = []
        for cb in children:
            try:
                leaf_blocks.extend(_expand_nested_block(cb, new_visited))
            except RuntimeError as re_err:
                parts = str(re_err).split(":", 1)
                err_st = parts[0] if parts[0] in ("inaccessible", "error") else "error"
                err_target = parts[1] if len(parts) > 1 and parts[1] else raw_target
                return err_st, err_target, []

        return "resolved", raw_target, leaf_blocks

    def _enrich_node_external_synced_blocks(
        self,
        node: PageNode,
        summary: Optional[Dict[str, Any]] = None,
    ) -> int:
        """未展開同期ブロック (_is_external_synced_block) を持つページについてのみ Notion API を呼び出し、
        多段参照の同期ブロック展開および不可視の空要素除外を行う。
        """
        if not self.token or not getattr(node, "blocks", None):
            return 0
        if not collect_external_synced_blocks_from_blocks(node.blocks):
            return 0
        if not _format_uuid_36(getattr(node, "id", "")):
            return 0

        status_code, api_blocks = self._fetch_block_children_cached(node.id)
        if status_code != 200:
            return 0

        api_synced_blocks = [b for b in api_blocks if b.get("type") == "synced_block"]
        resolved_slots = [self._resolve_synced_block_leaf_children(sb) for sb in api_synced_blocks]

        # node.blocks 内の同期スロット（展開済み _via_synced_block 連続ラン または 未展開 _is_external_synced_block）を走査
        new_blocks: List[Dict[str, Any]] = []
        slot_idx = 0
        resolved_count = 0
        removed_empty_count = 0
        i = 0
        n_len = len(node.blocks)
        while i < n_len:
            b = node.blocks[i]
            if isinstance(b, dict) and b.get("_is_external_synced_block"):
                explicit_ref = ((b.get("synced_block") or {}).get("synced_from") or {}).get("block_id")
                matched_slot = None
                if explicit_ref and _format_uuid_36(explicit_ref):
                    matched_slot = self._resolve_synced_block_leaf_children(b)
                elif slot_idx < len(resolved_slots):
                    matched_slot = resolved_slots[slot_idx]
                    slot_idx += 1

                if matched_slot is not None:
                    st, t_id, leaves = matched_slot
                    if st == "resolved":
                        if leaves:
                            new_blocks.extend(copy.deepcopy(leaves))
                            resolved_count += 1
                        else:
                            # Notion上でも中身が空の不可視同期ブロックは除去して警告対象外とする
                            removed_empty_count += 1
                        i += 1
                        continue
                    elif st == "inaccessible":
                        b.setdefault("synced_block", {})["synced_from"] = {"type": "block_id", "block_id": t_id}
                        new_blocks.append(b)
                        i += 1
                        continue
                elif not explicit_ref:
                    # ページAPIに存在しない余剰な空 <div dir="auto">（不可視の空要素）は除去する
                    removed_empty_count += 1
                    i += 1
                    continue

                new_blocks.append(b)
                i += 1
            elif isinstance(b, dict) and b.get("_via_synced_block"):
                # HTML側で既に展開されている1つ分の同期ブロック群を1スロットとして進める
                slot_idx += 1
                while i < n_len and isinstance(node.blocks[i], dict) and node.blocks[i].get("_via_synced_block"):
                    new_blocks.append(node.blocks[i])
                    i += 1
            else:
                new_blocks.append(b)
                i += 1

        if resolved_count or removed_empty_count:
            node.blocks = new_blocks
            node.external_synced_blocks = collect_external_synced_blocks_from_blocks(node.blocks)
            if summary is not None:
                summary["synced_blocks_resolved"] = summary.get("synced_blocks_resolved", 0) + resolved_count
                summary["empty_synced_blocks_removed"] = summary.get("empty_synced_blocks_removed", 0) + removed_empty_count
            logger.info(
                f"[API-ENRICH] Page [{node.title}] ({node.id}): "
                f"resolved {resolved_count} multi-hop synced block(s), removed {removed_empty_count} empty/invisible block(s)."
            )
        return resolved_count + removed_empty_count

    def enrich_external_synced_blocks(
        self,
        root_node: PageNode,
        summary: Optional[Dict[str, Any]] = None,
    ) -> int:
        """ツリー内の未展開同期ブロックを持つページのみピンポイントでAPI補完・展開する。"""
        total_changed = 0

        def _walk(n: PageNode):
            nonlocal total_changed
            total_changed += self._enrich_node_external_synced_blocks(n, summary)
            for c in getattr(n, "children", None) or []:
                _walk(c)

        if root_node:
            _walk(root_node)
            if total_changed > 0:
                mark_unresolved_external_references(root_node)
        return total_changed

