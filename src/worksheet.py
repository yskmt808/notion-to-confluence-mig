"""
人間確認用チェックシート出力＆再読込マネージャー (Human-in-the-Loop Worksheet Mode / Task B.4)。
HTML/CSVエクスポートからヒューリスティックに推測されたデータベースのカラムデータ型
（単一選択 vs 複数選択、日付、数値、テキスト等）およびビュー定義（Board/カンバン、Gallery、Table、Group by）を
人間がExcel等で目視確認・修正・承認できるチェックシート（CSV/JSON）として出力・再読込します。
"""

import os
import io
import csv
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from src.extractor import PageNode

logger = logging.getLogger(__name__)

ALLOWED_DATA_TYPES = [
    "select",
    "multi_select",
    "status",
    "text",
    "number",
    "date",
    "checkbox",
    "user",
    "relation",
]

ALLOWED_VIEW_LAYOUTS = [
    "table",
    "board",
    "gallery",
    "list",
]


class WorksheetManager:
    """チェックシートの出力・読込・適用を司るマネージャー。"""

    @staticmethod
    def extract_metadata(tree: PageNode) -> List[Dict[str, Any]]:
        """ツリー内の全データベースから、カラム定義とビュー定義のメタデータを抽出する。"""
        db_list = []

        def traverse(node: PageNode):
            if node.node_type == "database":
                db_data = {
                    "database_id": node.id,
                    "database_title": node.title,
                    "columns": WorksheetManager._extract_db_columns(node),
                    "views": WorksheetManager._extract_db_views(node),
                }
                db_list.append(db_data)
            for child in node.children:
                traverse(child)

        traverse(tree)
        return db_list

    @staticmethod
    def _extract_db_columns(db_node: PageNode) -> List[Dict[str, Any]]:
        """データベースの子レコードまたは既存スキーマからカラム情報を集約・推測する。"""
        schema = db_node.properties.get("schema", {})
        columns = []

        if schema:
            for c_name, c_meta in schema.items():
                p_type = c_meta.get("type", "text")
                # オプション一覧
                opts = []
                if p_type in ("select", "status") and "select" in c_meta:
                    opts = [o.get("name") for o in c_meta["select"].get("options", [])]
                elif p_type == "multi_select" and "multi_select" in c_meta:
                    opts = [o.get("name") for o in c_meta["multi_select"].get("options", [])]

                columns.append({
                    "column_name": c_name,
                    "inferred_type": p_type,
                    "confirmed_type": p_type,
                    "options_sample": ", ".join(opts[:6]) if opts else "",
                    "notes": "",
                })
        elif db_node.children:
            col_samples: Dict[str, List[Any]] = {}
            for child in db_node.children:
                for k, v in child.properties.items():
                    if k not in col_samples:
                        col_samples[k] = []
                    val = v.get("value") if isinstance(v, dict) else v
                    if val is not None and val != "":
                        col_samples[k].append(val)

            for col_name, samples in col_samples.items():
                inferred = WorksheetManager._infer_column_type(col_name, samples)
                sample_opts = []
                for s in samples:
                    if isinstance(s, list):
                        for it in s:
                            lbl = it.get("name") or it.get("label") if isinstance(it, dict) else str(it)
                            if lbl and lbl not in sample_opts:
                                sample_opts.append(lbl)
                    elif s and str(s) not in sample_opts:
                        sample_opts.append(str(s))

                columns.append({
                    "column_name": col_name,
                    "inferred_type": inferred,
                    "confirmed_type": inferred,
                    "options_sample": ", ".join(sample_opts[:6]),
                    "notes": "",
                })

        return columns

    @staticmethod
    def _infer_column_type(col_name: str, samples: List[Any]) -> str:
        """カラム名とサンプル値から最適な型を推測する。"""
        name_lower = col_name.lower()
        if any(w in name_lower for w in ["ステータス", "status", "進捗", "状態"]):
            return "status"
        if any(w in name_lower for w in ["期日", "日付", "date", "due"]):
            return "date"
        if any(w in name_lower for w in ["優先度", "priority"]):
            return "select"
        if any(w in name_lower for w in ["タグ", "カテゴリ", "tags", "labels", "category"]):
            return "multi_select"
        if any(w in name_lower for w in ["担当", "assignee", "user", "owner", "people"]):
            return "user"

        for s in samples:
            if isinstance(s, list) or (isinstance(s, str) and "," in s):
                return "multi_select"
            if s in (True, False, "Yes", "No", "true", "false", "True", "False"):
                return "checkbox"
            try:
                float(s)
                return "number"
            except (ValueError, TypeError):
                pass

        return "select" if len(set(str(s) for s in samples)) <= 8 else "text"

    @staticmethod
    def _extract_db_views(db_node: PageNode) -> List[Dict[str, Any]]:
        """データベースのビュー一覧を抽出または推測する。"""
        views = []
        views_config = db_node.properties.get("views_config")
        if views_config:
            for v in views_config:
                v_type = (v.get("type") or "table").strip().lower()
                v_name = v.get("name", "Default view")
                # Confluence Databases は table, board, gallery, list をサポート。
                # Notion の calendar / timeline は未対応のため、日付順テーブルに推測・マッピング
                if v_type in ("calendar", "timeline"):
                    inferred = "table"
                    notes = f"Notion {v_type} ビュー (Confluence DBはカレンダーレイアウト未対応のため日付順テーブルとして作成)"
                elif v_type in ALLOWED_VIEW_LAYOUTS:
                    inferred = v_type
                    notes = ""
                else:
                    inferred = "table"
                    notes = f"Notion {v_type} ビュー (未対応レイアウトのためテーブルにフォールバック)"

                views.append({
                    "view_name": v_name,
                    "inferred_layout": inferred,
                    "confirmed_layout": inferred,
                    "group_by_column": v.get("configuration", {}).get("group_by", ""),
                    "notes": notes,
                })
        else:
            # デフォルト推測ビュー
            views.append({
                "view_name": "デフォルトテーブル",
                "inferred_layout": "table",
                "confirmed_layout": "table",
                "group_by_column": "",
                "notes": "全件一覧表",
            })
            # 単一選択やステータスカラムに基づくボードビューの推測
            for col in WorksheetManager._extract_db_columns(db_node):
                if col["inferred_type"] in ("status", "select"):
                    views.append({
                        "view_name": f"{col['column_name']}別ボード",
                        "inferred_layout": "board",
                        "confirmed_layout": "board",
                        "group_by_column": col["column_name"],
                        "notes": f"{col['column_name']}を軸とするカンバンボード",
                    })
            views.append({
                "view_name": "ギャラリーカード",
                "inferred_layout": "gallery",
                "confirmed_layout": "gallery",
                "group_by_column": "",
                "notes": "カードグリッド表示",
            })
        return views

    def export_worksheet(self, tree: PageNode, file_path: str):
        """データベース定義チェックシートをCSVまたはJSON形式で出力する。"""
        db_list = self.extract_metadata(tree)

        if file_path.endswith(".json"):
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump({"databases": db_list}, f, ensure_ascii=False, indent=2)
            logger.info(f"Exported database worksheet (JSON): {file_path}")
            return

        # デフォルトはCSV形式
        lines = []
        lines.append("# ============================================================")
        lines.append("# Notion to Confluence 移行: データベース属性・ビュー設定チェックシート")
        lines.append("# 編集方法:")
        lines.append("#   1. confirmed_type 列: カラムのデータ型を修正・確認してください。")
        lines.append(f"#      選択可能型: {', '.join(ALLOWED_DATA_TYPES)}")
        lines.append("#   2. confirmed_layout 列: ビュー形式を修正・確認してください。")
        lines.append(f"#      選択可能形式: {', '.join(ALLOWED_VIEW_LAYOUTS)}")
        lines.append("#   3. group_by_column 列: ボードまたはグループ化の基準カラム名を指定してください。")
        lines.append("# ============================================================")
        lines.append("")
        lines.append("# --- SECTION: COLUMNS ---")
        lines.append("database_id,database_title,column_name,inferred_type,confirmed_type,allowed_types,options_sample,notes")

        for db in db_list:
            for col in db["columns"]:
                row = [
                    db["database_id"],
                    db["database_title"],
                    col["column_name"],
                    col["inferred_type"],
                    col["confirmed_type"],
                    "/".join(ALLOWED_DATA_TYPES),
                    col["options_sample"].replace('"', '""'),
                    col["notes"],
                ]
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(row)
                lines.append(output.getvalue().strip())

        lines.append("")
        lines.append("# --- SECTION: VIEWS ---")
        lines.append("database_id,database_title,view_name,inferred_layout,confirmed_layout,allowed_layouts,group_by_column,notes")

        for db in db_list:
            for v in db["views"]:
                row = [
                    db["database_id"],
                    db["database_title"],
                    v["view_name"],
                    v["inferred_layout"],
                    v["confirmed_layout"],
                    "/".join(ALLOWED_VIEW_LAYOUTS),
                    v["group_by_column"],
                    v["notes"],
                ]
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(row)
                lines.append(output.getvalue().strip())

        with open(file_path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"Exported database worksheet (CSV): {file_path}")

    def import_worksheet(self, file_path: str) -> Dict[str, Any]:
        """編集されたチェックシート（CSVまたはJSON）を読み込み、正規化辞書を返す。"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Worksheet file not found: {file_path}")

        if file_path.endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._normalize_json_worksheet(data)

        # CSV 読み込み
        return self._parse_csv_worksheet(file_path)

    def _normalize_json_worksheet(self, data: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for db in data.get("databases", []):
            db_id = db.get("database_id")
            columns = {}
            for c in db.get("columns", []):
                c_name = c.get("column_name")
                c_type = c.get("confirmed_type") or c.get("inferred_type", "text")
                columns[c_name] = c_type.strip().lower()

            views = []
            for v in db.get("views", []):
                views.append({
                    "name": v.get("view_name"),
                    "layout": (v.get("confirmed_layout") or v.get("inferred_layout", "table")).strip().lower(),
                    "group_by": v.get("group_by_column", "").strip(),
                })
            result[db_id] = {
                "database_title": db.get("database_title"),
                "columns": columns,
                "views": views,
            }
        return result

    def _parse_csv_worksheet(self, file_path: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        with open(file_path, "r", encoding="utf-8-sig") as f:
            lines = [l.strip() for l in f if l.strip()]

        current_section = None
        for line in lines:
            if line.startswith("# --- SECTION: COLUMNS ---"):
                current_section = "COLUMNS"
                continue
            elif line.startswith("# --- SECTION: VIEWS ---"):
                current_section = "VIEWS"
                continue
            elif line.startswith("#") or line.startswith("database_id,"):
                continue

            reader = csv.reader(io.StringIO(line))
            rows = list(reader)
            if not rows:
                continue
            row = rows[0]

            if current_section == "COLUMNS" and len(row) >= 5:
                db_id, db_title, col_name, inferred_type, confirmed_type = row[:5]
                c_type = (confirmed_type or inferred_type or "text").strip().lower()
                if db_id not in result:
                    result[db_id] = {"database_title": db_title, "columns": {}, "views": []}
                result[db_id]["columns"][col_name.strip()] = c_type

            elif current_section == "VIEWS" and len(row) >= 5:
                db_id = row[0]
                db_title = row[1]
                v_name = row[2].strip()
                inferred_layout = row[3]
                confirmed_layout = row[4]
                group_by = row[6].strip() if len(row) >= 7 else ""
                v_layout = (confirmed_layout or inferred_layout or "table").strip().lower()
                if db_id not in result:
                    result[db_id] = {"database_title": db_title, "columns": {}, "views": []}
                result[db_id]["views"].append({
                    "name": v_name,
                    "layout": v_layout,
                    "group_by": group_by,
                })

        logger.info(f"Loaded worksheet with {len(result)} database definitions from [{file_path}]")
        return result

    def apply_worksheet_to_tree(self, tree: PageNode, worksheet_data: Dict[str, Any]) -> Dict[str, int]:
        """チェックシートで承認されたカラム型およびビュー設定をPageNodeツリーへ適用する。"""
        stats = {"databases_updated": 0, "columns_updated": 0, "views_updated": 0}

        def traverse(node: PageNode):
            if node.node_type == "database":
                # IDまたはタイトルによる照合
                matched_db = worksheet_data.get(node.id)
                if not matched_db:
                    # 32桁 / 36桁 ハイフン有無マッチング
                    raw_id = node.id.replace("-", "")
                    for wid, wval in worksheet_data.items():
                        if wid.replace("-", "") == raw_id or wval.get("database_title") == node.title:
                            matched_db = wval
                            break

                if matched_db:
                    confirmed_columns = matched_db.get("columns", {})
                    confirmed_views = matched_db.get("views", [])

                    # 1. データベースノードのスキーマプロパティを更新
                    schema = node.properties.get("schema", {})
                    for c_name, c_type in confirmed_columns.items():
                        if c_name not in schema:
                            schema[c_name] = {"type": c_type}
                        else:
                            schema[c_name]["type"] = c_type
                        stats["columns_updated"] += 1
                    node.properties["schema"] = schema

                    # 2. ビュー設定を更新
                    if confirmed_views:
                        node.properties["confirmed_views"] = confirmed_views
                        stats["views_updated"] += len(confirmed_views)

                    # 3. 各子レコード（database_row）のプロパティ型を再調整
                    for row in node.children:
                        for c_name, c_type in confirmed_columns.items():
                            if c_name in row.properties:
                                cur_prop = row.properties[c_name]
                                raw_val = cur_prop.get("value") if isinstance(cur_prop, dict) else cur_prop
                                if c_type == "multi_select":
                                    if isinstance(raw_val, list):
                                        items = raw_val
                                    elif isinstance(raw_val, str):
                                        items = [s.strip() for s in raw_val.split(",") if s.strip()]
                                    else:
                                        items = []
                                    item_dicts = [{"name": it if isinstance(it, str) else it.get("name", "")} for it in items]
                                    row.properties[c_name] = {"type": "multi_select", "multi_select": item_dicts, "value": item_dicts}
                                elif c_type in ("select", "status"):
                                    row.properties[c_name] = {"type": "select", "select": {"name": str(raw_val)}, "value": str(raw_val)}
                                elif c_type == "checkbox":
                                    is_chk = raw_val in (True, "True", "true", "Yes", "yes", "1")
                                    row.properties[c_name] = {"type": "checkbox", "checkbox": is_chk, "value": is_chk}
                                else:
                                    row.properties[c_name] = {"type": c_type, "value": raw_val}

                    stats["databases_updated"] += 1
                    logger.info(
                        f"[WORKSHEET] Applied confirmed schema to [{node.title}]: "
                        f"{len(confirmed_columns)} columns, {len(confirmed_views)} views."
                    )

            for child in node.children:
                traverse(child)

        traverse(tree)
        return stats

