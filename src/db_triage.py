"""DB移行方式トリアージ（サイジング分析・CSVエクスポート・個別方式上書き読込）モジュール。"""

import csv
import os
from typing import Any, Dict, List, Optional

from src.extractor import (
    analyze_database_child_page_stats,
    database_row_has_substantive_body,
    is_empty_database_row,
    is_individual_child_pages_database,
    normalize_db_title,
    normalize_notion_id,
)

VALID_DB_MODES = ("aggregated", "individual", "static_table", "skip")

LARGE_DB_ROW_THRESHOLD = 300
LARGE_DB_BODY_BLOCKS_THRESHOLD = 1000
SMALL_DB_BODY_ROW_THRESHOLD = 30


def resolve_db_migration_mode(
    db_node: Any,
    individual_db_child_threshold: int = 0,
    mode_overrides: Optional[Dict[str, str]] = None,
) -> str:
    """DBノードに適用される移行方式 ('aggregated' / 'individual' / 'static_table' / 'skip') を解決する。"""
    if mode_overrides:
        norm_id = normalize_notion_id(getattr(db_node, "id", "") or "")
        if norm_id in mode_overrides:
            return mode_overrides[norm_id]
        raw_id = getattr(db_node, "id", "") or ""
        if raw_id in mode_overrides:
            return mode_overrides[raw_id]
    if is_individual_child_pages_database(
        db_node,
        individual_db_child_threshold=individual_db_child_threshold,
    ):
        return "individual"
    return "aggregated"


def _recommend_db_triage(total_rows: int, body_rows: int, total_body_blocks: int) -> str:
    if body_rows == 0:
        return "詳細なし (textキーのみ)"
    if total_rows >= LARGE_DB_ROW_THRESHOLD or total_body_blocks >= LARGE_DB_BODY_BLOCKS_THRESHOLD:
        return "⚠️ 大規模DB注意 (static_table / skip検討)"
    if 1 <= body_rows <= SMALL_DB_BODY_ROW_THRESHOLD:
        return "💡 小規模DB (individual候補)"
    return "✅ 標準 (aggregated推奨)"


def _count_embedded_db_views(root_node: Any) -> Dict[str, int]:
    """ツリー全体のブロックを走査し、各DB(正規化ID)が他ページからビュー埋め込み/リンク参照されている回数を集計する。"""
    db_by_norm_id: Dict[str, str] = {}
    db_by_title: Dict[str, str] = {}

    def _collect_dbs(n: Any):
        if getattr(n, "node_type", "") == "database":
            nid = normalize_notion_id(getattr(n, "id", "") or "")
            if nid:
                db_by_norm_id[nid] = nid
            title = getattr(n, "title", "")
            if title:
                s_title = str(title).strip()
                db_by_title[s_title] = nid
                norm_t = normalize_db_title(s_title)
                if norm_t and norm_t not in db_by_title:
                    db_by_title[norm_t] = nid
        for c in (getattr(n, "children", None) or []):
            _collect_dbs(c)

    _collect_dbs(root_node)
    counts: Dict[str, int] = {nid: 0 for nid in db_by_norm_id}
    if not db_by_norm_id:
        return counts

    def _resolve_target(candidate_ids: List[str], candidate_title: str = "") -> Optional[str]:
        for cid in candidate_ids:
            if cid:
                nid = normalize_notion_id(cid)
                if nid in db_by_norm_id:
                    return nid
        if candidate_title:
            s = str(candidate_title).strip()
            if s in db_by_title:
                return db_by_title[s]
            nt = normalize_db_title(s)
            if nt in db_by_title:
                return db_by_title[nt]
        return None

    def _scan_blocks(blocks: List[Dict[str, Any]]):
        for b in (blocks or []):
            if not isinstance(b, dict):
                continue
            b_type = b.get("type", "")
            if b_type == "child_database" and b.get("is_linked_database"):
                c_ids = [b.get("database_id"), b.get("id")]
                src_name = ""
                for ds in (b.get("data_sources") or []):
                    if isinstance(ds, dict):
                        if ds.get("id"):
                            c_ids.append(ds["id"])
                        if not src_name and ds.get("name"):
                            src_name = ds["name"]
                target_db = _resolve_target([c for c in c_ids if c], src_name)
                if target_db:
                    counts[target_db] = counts.get(target_db, 0) + 1
            elif b_type == "link_to_page":
                lp = b.get("link_to_page") or {}
                target_id = lp.get("page_id") or lp.get("database_id")
                target_db = _resolve_target([target_id] if target_id else [])
                if target_db:
                    counts[target_db] = counts.get(target_db, 0) + 1
            if b.get("children_blocks"):
                _scan_blocks(b["children_blocks"])

    def _walk_pages(n: Any):
        _scan_blocks(getattr(n, "blocks", None) or [])
        for c in (getattr(n, "children", None) or []):
            _walk_pages(c)

    _walk_pages(root_node)
    return counts


def collect_db_triage_rows(
    root_node: Any,
    individual_db_child_threshold: int = 0,
    mode_overrides: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """ツリー内の全データベースを走査し、トリアージ分析行リストを返す。"""
    rows: List[Dict[str, Any]] = []
    if not root_node:
        return rows

    embedded_counts = _count_embedded_db_views(root_node)

    def _walk(node: Any):
        ntype = getattr(node, "node_type", "")
        title = getattr(node, "title", "") or "(無題)"
        if ntype == "database":
            stats = analyze_database_child_page_stats(node)
            children = getattr(node, "children", None) or []
            total_body_blocks = sum(
                len(getattr(c, "blocks", None) or [])
                for c in children
                if not is_empty_database_row(c) and database_row_has_substantive_body(c)
            )
            mode = resolve_db_migration_mode(
                node,
                individual_db_child_threshold=individual_db_child_threshold,
                mode_overrides=mode_overrides,
            )
            norm_id = normalize_notion_id(getattr(node, "id", "") or "") or (getattr(node, "id", "") or "")
            rows.append({
                "db_id": norm_id,
                "db_title": title,
                "total_rows": stats["total_rows"],
                "body_rows": stats["body_rows"],
                "body_ratio_pct": round(stats["body_ratio_pct"], 1),
                "total_body_blocks": total_body_blocks,
                "embedded_view_count": embedded_counts.get(norm_id, 0),
                "migration_mode": mode,
                "recommendation": _recommend_db_triage(
                    stats["total_rows"],
                    stats["body_rows"],
                    total_body_blocks,
                ),
            })
        for child in (getattr(node, "children", None) or []):
            _walk(child)

    _walk(root_node)
    return rows


def export_db_triage_csv(rows: List[Dict[str, Any]], output_path: str) -> str:
    """トリアージ分析結果を UTF-8-BOM 付き CSV ファイルへ書き出す。"""
    parent_dir = os.path.dirname(os.path.abspath(output_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    fieldnames = [
        "db_id",
        "db_title",
        "total_rows",
        "body_rows",
        "body_ratio_pct",
        "total_body_blocks",
        "embedded_view_count",
        "migration_mode",
        "recommendation",
    ]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    return output_path


def load_db_triage_overrides(csv_path: str) -> Dict[str, str]:
    """編集済みトリアージ CSV から DB ID -> migration_mode の上書きマップを読み込む。"""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"DBトリアージCSVが見つかりません: {csv_path}")
    overrides: Dict[str, str] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "db_id" not in reader.fieldnames or "migration_mode" not in reader.fieldnames:
            raise ValueError(
                f"DBトリアージCSVに必須列 (db_id, migration_mode) がありません: {csv_path}"
            )
        for line_no, row in enumerate(reader, start=2):
            raw_id = (row.get("db_id") or "").strip()
            mode = (row.get("migration_mode") or "").strip().lower()
            if not raw_id or not mode:
                continue
            if mode not in VALID_DB_MODES:
                raise ValueError(
                    f"DBトリアージCSV {line_no}行目の migration_mode '{mode}' は不正です "
                    f"(指定可能: {', '.join(VALID_DB_MODES)})"
                )
            norm_id = normalize_notion_id(raw_id) or raw_id
            overrides[norm_id] = mode
    return overrides


def format_db_triage_table(rows: List[Dict[str, Any]]) -> str:
    """標準出力用の DB トリアージ一覧テーブル文字列を整形する。"""
    if not rows:
        return "📊 [DB Triage] 対象ツリー内にデータベースは検出されませんでした。"
    lines = [
        f"📊 [DB Triage] データベース移行方式トリアージ一覧 ({len(rows)} 件):",
        "  --------------------------------------------------------------------------------",
        "  DB名 | 総行 / 本文行 (率) | 本文ブロック | ビュー埋め込み | 予定方式 | 判定メモ",
        "  --------------------------------------------------------------------------------",
    ]
    for r in rows:
        lines.append(
            f"  - {r['db_title']} ({r['db_id'][:8]}...) | "
            f"{r['total_rows']}行 / {r['body_rows']}行 ({r['body_ratio_pct']}%) | "
            f"{r['total_body_blocks']} blocks | "
            f"埋め込み: {r.get('embedded_view_count', 0)}件 | "
            f"[{r['migration_mode']}] | {r['recommendation']}"
        )
    lines.append("  --------------------------------------------------------------------------------")
    return "\n".join(lines)
