#!/usr/bin/env python3
"""
Notion to Confluence 移行メインスクリプト。
Notion からページツリー・DBを取得し、Confluence 上へ同一階層で移行します。
"""

import os
import re
import sys
import json
import logging
import argparse
import warnings
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", category=UserWarning)

import glob
import zipfile
from src.extractor import (
    NotionExtractor,
    PageNode,
    count_migratable_nodes,
    get_node_external_synced_blocks,
    get_node_missing_zip_attachments,
    get_node_unresolved_external_dbs,
    get_node_unresolved_external_pages,
    get_node_unsupported_page_elements,
)
from src.html_extractor import HtmlZipExtractor, MultiZipExtractor
from src.importer import ConfluenceImporter
from src.verifier import MigrationVerifier
from src.checkpoint import MigrationCheckpoint, CheckpointWriteError
from src.db_triage import (
    collect_db_triage_rows,
    export_db_triage_csv,
    format_db_triage_table,
    load_db_triage_overrides,
)
from src.cancellation import CancellationManager, MigrationCancelledException
from src.telemetry import SanitizedTelemetry
from src.api_enricher import NotionApiEnricher
from src.worksheet import WorksheetManager
from src.dashboard import MigrationDashboard
from src.user_mapper import UserMappingManager
from src.logger_util import setup_notice_log_level, setup_uncaught_exception_handler
from src.timezone_util import get_local_timezone

setup_notice_log_level()
setup_uncaught_exception_handler()

load_dotenv()
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass



def setup_logger(verbose: bool = False, dashboard_mode: bool = False, log_file: str = "migration.log"):
    """ログ設定。ダッシュボード有効時はコンソールへのINFOログ出力を抑制し、ファイルへ記録して端末の行乱れを防止する。"""
    level = logging.DEBUG if verbose else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = []

    # 1. 常に migration.log に詳細なログを出力（UTF-8）
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    try:
        if os.path.exists(log_file):
            os.chmod(log_file, 0o600)
    except Exception:
        pass
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    root_logger.addHandler(file_handler)

    # 2. コンソール出力ハンドラー
    if not dashboard_mode:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )
        root_logger.addHandler(console_handler)
    else:
        # ダッシュボード表示中は画面乱れ防止のためCRITICALのみコンソール出力
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.CRITICAL)
        root_logger.addHandler(console_handler)


def print_tree(node: PageNode, depth: int = 0):
    indent = "  " * depth
    type_emoji = "📊" if node.node_type == "database" else ("📋" if node.node_type == "database_row" else "📄")
    print(f"{indent}{type_emoji} [{node.node_type}] {node.title} (Blocks: {len(node.blocks)})")
    for child in node.children:
        print_tree(child, depth + 1)


def collect_tree_stats(
    node: PageNode,
    individual_db_child_threshold: int = 0,
    db_mode_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    stats = {
        "total": 0,
        "pages": 0,
        "databases": 0,
        "database_rows": 0,
        "max_depth": 0,
    }

    def _traverse(n: PageNode, depth: int):
        if n.node_type == "database":
            stats["databases"] += 1
        elif n.node_type == "database_row":
            stats["database_rows"] += 1
        else:
            stats["pages"] += 1
        if depth > stats["max_depth"]:
            stats["max_depth"] = depth
        for child in n.children:
            _traverse(child, depth + 1)

    _traverse(node, 1)
    # total は実際に作成・移行されるページ数（ダッシュボード母数と完全一致）
    stats["total"] = count_migratable_nodes(
        node,
        individual_db_child_threshold=individual_db_child_threshold,
        db_mode_overrides=db_mode_overrides,
    )
    return stats


def print_tree_summary(
    root: PageNode,
    verbose: bool = False,
    show_all: bool = False,
    individual_db_child_threshold: int = 0,
    db_mode_overrides: Optional[Dict[str, str]] = None,
):
    """
    Notionツリー構造の標準出力を表示する。
    ページ数が多数（25件超）の場合は、端末画面の圧迫を防ぐため
    主要セクションとノード種別内訳をサマライズして表示する。
    """
    stats = collect_tree_stats(
        root,
        individual_db_child_threshold=individual_db_child_threshold,
        db_mode_overrides=db_mode_overrides,
    )
    threshold = 25

    if stats["total"] <= threshold or verbose or show_all:
        print("🌲 抽出されたNotionツリー構造:")
        print_tree(root)
        print("")
        return

    print(f"🌲 抽出されたNotionツリー概要 (合計: {stats['total']} ノード):")
    print(f"  • 📄 通常ページ      : {stats['pages']} 件")
    if stats["databases"]:
        print(f"  • 📊 データベース    : {stats['databases']} 件")
    if stats["database_rows"]:
        creatable_rows = max(0, stats["total"] - stats["pages"] - stats["databases"])
        print(f"  • 📋 データベース行  : {stats['database_rows']} 件 (うち子ページ作成対象: {creatable_rows} 件)")
    print(f"  • 階層の最大深度    : {stats['max_depth']} 階層")
    print("")

    print("📂 主要セクション別内訳 (上位階層プレビュー):")
    type_emoji = "📊" if root.node_type == "database" else "📄"
    print(f"{type_emoji} [{root.node_type}] {root.title}")

    max_children_preview = 8
    children_to_show = root.children[:max_children_preview]
    for idx, child in enumerate(children_to_show):
        is_last = (idx == len(root.children) - 1)
        prefix = "└── " if is_last else "├── "
        c_type_emoji = "📊" if child.node_type == "database" else ("📋" if child.node_type == "database_row" else "📄")

        c_stats = collect_tree_stats(
            child,
            individual_db_child_threshold=individual_db_child_threshold,
            db_mode_overrides=db_mode_overrides,
        )
        if c_stats["total"] > 1:
            detail_parts = []
            if c_stats["pages"]:
                detail_parts.append(f"ページ: {c_stats['pages']}件")
            if c_stats["databases"]:
                detail_parts.append(f"DB: {c_stats['databases']}件")
            if c_stats["database_rows"]:
                detail_parts.append(f"DB行: {c_stats['database_rows']}件")
            detail_str = f" ({', '.join(detail_parts)})"
        elif child.node_type == "database" and child.children:
            detail_str = f" ({len(child.children)} 行)"
        else:
            detail_str = ""

        print(f"  {prefix}{c_type_emoji} {child.title}{detail_str}")

        # 子ノード直下のデータベースをプレビュー表示
        sub_dbs = [sc for sc in child.children if sc.node_type == "database"]
        sub_prefix = "      " if is_last else "  │   "
        for sdb in sub_dbs:
            print(f"{sub_prefix}📊 [database] {sdb.title} ({len(sdb.children)} 行)")

    if len(root.children) > max_children_preview:
        remaining_children = len(root.children) - max_children_preview
        print(f"  └── ... 他 {remaining_children} 件の主要ページ・セクション")

    print("")
    print(f"💡 ページ総数が多いため概要のみ表示しています。全 {stats['total']} 件の全階層ツリーを展開表示するには `--show-all-pages` または `--verbose` を指定してください。")
    print("")


def _collect_warning_pages(root: PageNode, getter) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体から指定の警告コレクタに該当するページ一覧と総件数を収集する。"""
    total_count = 0
    affected_pages: List[Tuple[PageNode, List[Dict[str, Any]]]] = []

    def _traverse(n: PageNode):
        nonlocal total_count
        items = getter(n)
        if items:
            total_count += len(items)
            affected_pages.append((n, items))
        for c in n.children:
            _traverse(c)

    _traverse(root)
    return total_count, affected_pages


def collect_external_synced_pages(root: PageNode) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体からHTML未収録の外部同期ブロックを含むページ一覧と総件数を収集する。"""
    return _collect_warning_pages(root, get_node_external_synced_blocks)


def collect_unresolved_external_db_pages(root: PageNode) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体から移行対象外（ZIP未収録）の外部データベース参照を含むページ一覧と総件数を収集する。"""
    return _collect_warning_pages(root, get_node_unresolved_external_dbs)


def collect_unresolved_external_page_refs(root: PageNode) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体から移行対象外（ZIP未収録）の外部ページ参照を含むページ一覧と総件数を収集する。"""
    return _collect_warning_pages(root, get_node_unresolved_external_pages)


def collect_missing_zip_attachment_pages(root: PageNode) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体からZIP未収録の添付ファイル・メディアを含むページ一覧と総件数を収集する。"""
    return _collect_warning_pages(root, get_node_missing_zip_attachments)


def collect_unsupported_element_pages(root: PageNode) -> Tuple[int, List[Tuple[PageNode, List[Dict[str, Any]]]]]:
    """ツリー全体から未対応要素・外部アプリ埋め込みを含むページ一覧と総件数を収集する。"""
    return _collect_warning_pages(root, get_node_unsupported_page_elements)


def _print_dry_run_warning_section(
    header_label: str,
    total_count: int,
    affected_pages: List[Tuple[PageNode, List[Dict[str, Any]]]],
    show_details: bool = True,
    detail_hint: Optional[str] = None,
):
    if total_count <= 0:
        return
    suffix = ":" if show_details else "（詳細はデフォルト非表示）"
    print(
        f"⚠️ {header_label}を {total_count} 件検出しました"
        f"（対象ページ: {len(affected_pages)} 件）{suffix}"
    )
    if not show_details:
        if detail_hint:
            print(f"   💡 {detail_hint}")
        return
    for node, items in affected_pages:
        print(f"  - [{node.node_type}] {node.title} (ID: {node.id}): {len(items)} 件")
        for item in items:
            msg = item.get("message")
            if msg:
                print(f"      ⚠️ {msg}")


def print_dry_run_summary(
    tree: PageNode,
    telemetry: SanitizedTelemetry,
    show_external_warnings: bool = False,
):
    """ドライラン結果サマリー（WARNING件数・各種警告検出ページ一覧）を表示する。"""
    ext_total, affected_pages = collect_external_synced_pages(tree)
    ext_db_total, affected_db_pages = collect_unresolved_external_db_pages(tree)
    ext_page_total, affected_page_refs = collect_unresolved_external_page_refs(tree)
    missing_att_total, affected_att_pages = collect_missing_zip_attachment_pages(tree)
    unsup_total, affected_unsup_pages = collect_unsupported_element_pages(tree)
    warning_count = telemetry.total_warnings_count

    print("--------------------------------------------------")
    print(f"ドライラン結果サマリー: WARNING {warning_count} 件")
    _print_dry_run_warning_section("HTMLエクスポート未収録の外部同期ブロック", ext_total, affected_pages)
    _print_dry_run_warning_section("移行対象外（ZIP未収録）の外部データベース参照", ext_db_total, affected_db_pages)
    _print_dry_run_warning_section(
        "移行対象外（ZIP未収録）の外部ページ参照",
        ext_page_total,
        affected_page_refs,
        show_details=show_external_warnings,
        detail_hint="詳細を表示するには `--show-external-warnings` を指定して `--dry-run` を実行してください。",
    )
    _print_dry_run_warning_section("ZIP未収録の添付ファイル・メディア", missing_att_total, affected_att_pages)
    _print_dry_run_warning_section("未対応要素・外部アプリ埋め込み（フォールバック注記対象）", unsup_total, affected_unsup_pages)


def _is_external_link_warning(warning_msg: str) -> bool:
    return (
        "移行対象外（ZIP未収録）の外部ページ参照を検出しました" in warning_msg
        or "移行対象外（ZIP未収録）の外部データベース参照を検出しました" in warning_msg
    )


def print_verification_results(verify_results: List[Dict[str, Any]], verbose: bool = False, show_all: bool = False):
    """整合性検証結果をサマライズ表示する。"""
    pass_count = sum(1 for r in verify_results if r["status"] == "PASS")
    warn_count = sum(1 for r in verify_results if r["status"] == "WARN")
    fail_count = sum(1 for r in verify_results if r["status"] == "FAIL")
    total_warning_items = sum(len(r.get("warnings", [])) for r in verify_results)

    ext_page_warn_pages = 0
    ext_page_warn_items = 0
    ext_db_warn_pages = 0
    ext_db_warn_items = 0
    for r in verify_results:
        for w in r.get("warnings", []):
            m_items = re.search(r"\((\d+)\s*件\)", w)
            cnt = int(m_items.group(1)) if m_items else 1
            if "移行対象外（ZIP未収録）の外部ページ参照を検出しました" in w:
                ext_page_warn_pages += 1
                ext_page_warn_items += cnt
            elif "移行対象外（ZIP未収録）の外部データベース参照を検出しました" in w:
                ext_db_warn_pages += 1
                ext_db_warn_items += cnt

    print("--------------------------------------------------")
    print(f"検証サマリー: 合計 {len(verify_results)} ページ | 正常: {pass_count} | 警告: {warn_count} | 失敗: {fail_count}")
    if total_warning_items > 0:
        print(f"  ⚠️ 検出された警告詳細: {total_warning_items} 件")
    if ext_page_warn_items > 0:
        print(f"  ⚠️ 移行対象外（ZIP未収録）の外部ページ参照警告: {ext_page_warn_items} 件 (対象ページ: {ext_page_warn_pages} 件)")
    if ext_db_warn_items > 0:
        print(f"  ⚠️ 移行対象外（ZIP未収録）の外部データベース参照警告: {ext_db_warn_items} 件 (対象ページ: {ext_db_warn_pages} 件)")

    if verbose or show_all:
        for r in verify_results:
            other_warnings = [w for w in r.get("warnings", []) if not _is_external_link_warning(w)]
            icon = "✅" if r["status"] == "PASS" else ("⚠️" if r["status"] == "WARN" else "❌")
            print(f"{icon} [{r['status']}] {r['title']} (ID: {r['page_id']})")
            for chk in r["checks_passed"]:
                if verbose:
                    print(f"     ✓ {chk}")
            for w in other_warnings:
                print(f"     ⚠️ {w}")
            for iss in r["issues"]:
                print(f"     ❌ {iss}")
    else:
        # デフォルトは問題（外部リンク警告以外の警告・失敗）のあるページのみを抽出して報告し、PASS個別行や外部リンク詳細は非表示
        abnormal_results = [
            r for r in verify_results
            if r.get("issues") or any(not _is_external_link_warning(w) for w in r.get("warnings", []))
        ]
        if abnormal_results:
            print(f"\n⚠️ 確認・対応が必要なページ ({len(abnormal_results)} 件):")
            for r in abnormal_results:
                other_warnings = [w for w in r.get("warnings", []) if not _is_external_link_warning(w)]
                icon = "❌" if r.get("issues") else "⚠️"
                print(f"{icon} [{r['status']}] {r['title']} (ID: {r['page_id']})")
                for w in other_warnings:
                    print(f"     ⚠️ {w}")
                for iss in r["issues"]:
                    print(f"     ❌ {iss}")
        else:
            if ext_page_warn_items > 0 or ext_db_warn_items > 0:
                print("✅ 外部リンク警告以外の重大な警告・エラーはありません。")
            else:
                print("✅ 全ページが整合性検証に完全合格しました（警告・エラーなし）。")
        print("💡 全ページの個別検証項目を表示するには `--verbose` または `--show-all-pages` を指定してください。")


def print_result_summary(mapping: Dict[str, Dict[str, Any]], result_path: str, show_all: bool = False, verbose: bool = False):
    """移行完了時のページURL一覧をサマライズ表示する。"""
    total = len(mapping)
    threshold = 25

    if total <= threshold or show_all or verbose:
        print("--------------------------------------------------")
        for nid, item in mapping.items():
            print(f" - [{item['node_type']}] {item['title']}")
            print(f"   URL: {item['url']}")
        return

    # 大規模向けサマリー表示
    pages = [m for m in mapping.values() if m.get("node_type") == "page"]
    dbs = [m for m in mapping.values() if m.get("node_type") == "database"]
    rows = [m for m in mapping.values() if m.get("node_type") == "database_row"]

    print("--------------------------------------------------")
    print(f"📊 移行成果サマリー (合計: {total} 件):")
    print(f"  • 📄 通常ページ      : {len(pages)} 件 (Confluenceページ作成・更新完了)")
    if dbs:
        print(f"  • 📊 データベース    : {len(dbs)} 件 (Confluence Databases作成・ビュー設定完了)")
    if rows:
        print(f"  • 📋 データベース行  : {len(rows)} 件 (キーカラムリンク紐付け完了)")

    print("\n🔗 主要ConfluenceアクセスURL:")
    preview_count = 0
    for nid, item in mapping.items():
        if item.get("node_type") in ("page", "database") and preview_count < 6:
            print(f"  • [{item['node_type']}] {item['title']}")
            print(f"    URL: {item['url']}")
            preview_count += 1

    remaining = total - preview_count
    if remaining > 0:
        print(f"  ... 他 {remaining} 件の全ページURLは '{result_path}' に完全記録されています。")

    print(f"\n💡 全 {total} 件のURLを端末に出力するには `--show-all-pages` または `--verbose` を指定してください。")


def print_homonym_warnings(homonym_warnings: List[Dict[str, Any]]):
    """同姓同名ユーザー検知時の WARNING メッセージを標準出力およびログへ出力する。"""
    if not homonym_warnings:
        return
    header = f"⚠️ WARNING: 同姓同名ユーザーを {len(homonym_warnings)} 件検知しました（名前のみ参照の自動確定をスキップ）:"
    print(header)
    logging.warning(header)
    for w in homonym_warnings:
        disp_name = w.get("name", "")
        cands = w.get("candidates", [])
        locs = w.get("affected_locations", [])
        cand_strs = [
            f"id={c.get('id') or '(未取得)'} / email={c.get('email') or '(未取得)'}"
            for c in cands
        ]
        msg_line = f"  • 表示名: {disp_name} ({len(cands)} 名) | 候補: {'; '.join(cand_strs)}"
        print(msg_line)
        logging.warning(msg_line)
        if locs:
            loc_line = f"    影響箇所 ({len(locs)} 件): {', '.join(locs)}"
            print(loc_line)
            logging.warning(loc_line)
        else:
            loc_line = "    影響箇所 (0 件): 名前のみの未解決参照は検出されませんでした"
            print(loc_line)
            logging.warning(loc_line)
    print("")


_RESUMABLE_CLI_OPTIONS = (
    "notion_page_id",
    "zip_paths",
    "zip_dir",
    "title_prefix",
    "new_tree",
    "confluence_parent_id",
    "no_overwrite",
    "no_verify",
    "page_workers",
    "generate_debug_report",
    "debug_report_dir",
    "enrich_via_api",
    "import_worksheet",
    "external_mapping",
    "timezone",
    "no_dashboard",
    "user_mapping",
    "metadata_template",
    "show_all_pages",
    "show_external_warnings",
    "private",
    "visibility",
    "repair_inpage_links",
    "individual_db_child_threshold",
    "import_db_triage",
    "verbose",
)


def _get_explicit_cli_dests(parser: argparse.ArgumentParser, argv: Optional[List[str]] = None) -> set:
    """コマンドライン引数 (argv) で明示的に指定されたオプションの dest 名集合を返す。"""
    tokens = sys.argv[1:] if argv is None else argv
    option_to_dest: Dict[str, str] = {}
    for action in parser._actions:
        for opt in action.option_strings:
            option_to_dest[opt] = action.dest

    explicit: set = set()
    for token in tokens:
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            continue
        opt_name = token.split("=", 1)[0]
        if opt_name in option_to_dest:
            explicit.add(option_to_dest[opt_name])
        elif not opt_name.startswith("--") and len(opt_name) > 2:
            # 短縮オプションの結合 (-vp など) にも対応
            for ch in opt_name[1:]:
                short_opt = f"-{ch}"
                if short_opt in option_to_dest:
                    explicit.add(option_to_dest[short_opt])
    return explicit


def restore_cli_options_from_checkpoint(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    saved_source: Dict[str, Any],
    argv: Optional[List[str]] = None,
) -> List[str]:
    """チェックポイントの source_info から前回の CLI オプションを args に復元する（明示指定された引数は上書きしない）。"""
    if not isinstance(saved_source, dict):
        return []

    explicit_dests = _get_explicit_cli_dests(parser, argv=argv)
    for dest in _RESUMABLE_CLI_OPTIONS:
        if hasattr(args, dest) and getattr(args, dest) != parser.get_default(dest):
            explicit_dests.add(dest)

    # --zip / --zip-dir のいずれかが明示指定された場合は、もう片方をチェックポイントから復元しない
    zip_explicit = bool({"zip_paths", "zip_dir"} & explicit_dests)
    # --private / --visibility のいずれかが明示指定された場合は、公開範囲の上書きを防ぐ
    visibility_explicit = bool({"private", "visibility"} & explicit_dests)

    saved_cli = saved_source.get("cli_options")
    saved_cli_dict = saved_cli if isinstance(saved_cli, dict) else {}

    restored: List[str] = []
    for dest in _RESUMABLE_CLI_OPTIONS:
        if dest in explicit_dests:
            continue
        if dest in ("zip_paths", "zip_dir") and zip_explicit:
            continue
        if dest in ("private", "visibility") and visibility_explicit:
            continue

        if dest in saved_cli_dict:
            val = saved_cli_dict[dest]
        elif dest in saved_source:
            val = saved_source[dest]
        else:
            continue

        if val is None and getattr(args, dest, None) is None:
            continue
        if getattr(args, dest, None) != val:
            setattr(args, dest, val)
            restored.append(dest)

    # 旧形式チェックポイント（cli_options 未保存、private_mode のみ保存）への後方互換フォールバック
    if not visibility_explicit and "private" not in saved_cli_dict and "visibility" not in saved_cli_dict:
        if saved_source.get("private_mode") and not getattr(args, "private", False):
            args.private = True
            args.visibility = "private"
            restored.append("private_mode")

    return restored


def build_checkpoint_source_info(
    args: argparse.Namespace,
    *,
    use_zip: bool,
    zip_files: List[str],
    title_prefix: Optional[str],
    notion_page_id: Optional[str],
    conf_parent_id: Optional[str],
    conf_space: Optional[str],
    private_mode: bool,
) -> Dict[str, Any]:
    """チェックポイントに保存する source_info（全再開可能 CLI オプションを含む）を構築する。"""
    cli_options: Dict[str, Any] = {}
    for dest in _RESUMABLE_CLI_OPTIONS:
        if hasattr(args, dest):
            cli_options[dest] = getattr(args, dest)

    # 解決済みの識別子・プレフィックスも反映しておく
    cli_options["title_prefix"] = title_prefix
    cli_options["notion_page_id"] = notion_page_id
    cli_options["confluence_parent_id"] = conf_parent_id

    return {
        "use_zip": use_zip,
        "zip_paths": zip_files,
        "zip_dir": args.zip_dir,
        "title_prefix": title_prefix,
        "notion_page_id": notion_page_id,
        "confluence_parent_id": conf_parent_id,
        "space_key": conf_space,
        "page_workers": args.page_workers,
        "private_mode": private_mode,
        "cli_options": cli_options,
    }


def validate_api_credentials(
    notion_token: Optional[str] = None,
    conf_url: Optional[str] = None,
    conf_user: Optional[str] = None,
    conf_token: Optional[str] = None,
) -> None:
    """移行開始前に Notion / Confluence API キーの有効性を検証し、無効時はエラー終了する。"""
    if notion_token:
        import requests
        try:
            res = requests.get(
                "https://api.notion.com/v1/users/me",
                headers={
                    "Authorization": f"Bearer {notion_token}",
                    "Notion-Version": "2022-06-28",
                },
                timeout=15,
            )
            if res.status_code in (401, 403):
                # Personal Access Token / ユーザートークン等でユーザー情報取得が 401/403 の場合は /v1/search でトークン有効性を確認
                res_search = requests.post(
                    "https://api.notion.com/v1/search",
                    headers={
                        "Authorization": f"Bearer {notion_token}",
                        "Notion-Version": "2022-06-28",
                        "Content-Type": "application/json",
                    },
                    json={"page_size": 1},
                    timeout=15,
                )
                if not res_search.ok:
                    print(
                        f"❌ エラー: NOTION_TOKEN が無効または権限不足です "
                        f"(HTTP {res_search.status_code}: {res_search.text[:200]})。"
                    )
                    sys.exit(1)
            elif not res.ok:
                print(
                    f"❌ エラー: NOTION_TOKEN が無効または権限不足です "
                    f"(HTTP {res.status_code}: {res.text[:200]})。"
                )
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            print(f"❌ エラー: Notion API キーの有効性確認に失敗しました: {e}")
            sys.exit(1)

    if conf_url and conf_user and conf_token:
        from src.importer import ConfluenceClient
        try:
            client = ConfluenceClient(conf_url, conf_user, conf_token)
            user_info = client.get_current_user()
            if not user_info:
                print(
                    "❌ エラー: Confluence API キー (CONFLUENCE_USERNAME / CONFLUENCE_API_TOKEN) "
                    "が無効または接続できません。"
                )
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            print(f"❌ エラー: Confluence API キーの有効性確認に失敗しました: {e}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Notion to Confluence Migration Tool")
    parser.add_argument(
        "--notion-page-id",
        type=str,
        default=None,
        help="Notionの移行対象ページID (未指定時は notion_sample_info.json または .env から検出)",
    )
    parser.add_argument(
        "--zip", "--html-zip",
        dest="zip_paths",
        nargs="+",
        default=None,
        help="Notion HTMLエクスポートのZIPファイルパス（複数指定可、ワイルドカード可）",
    )
    parser.add_argument(
        "--zip-dir",
        type=str,
        default=None,
        help="Notion HTMLエクスポートZIPファイルが格納されたディレクトリ（例: 100pages）",
    )
    parser.add_argument(
        "--title-prefix",
        type=str,
        default=None,
        help="移行先Confluenceページのタイトルに付与するプレフィックス (ZIP移行時のデフォルト: '[HTML] ')",
    )
    parser.add_argument(
        "--new-tree",
        action="store_true",
        help="既存のツリーに依存せず、Confluenceスペース直下に完全に新しい親ツリーを作成して移行する",
    )
    parser.add_argument(
        "--confluence-parent-id",
        type=str,
        default=None,
        help="Confluence移行先の親ページID (未指定時は .env の CONFLUENCE_PARENT_PAGE_ID またはスペース直下)",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Confluence上に同名ページが存在する場合に上書きせず、連番を付与して新規作成する",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Confluenceへの送信を行わず、抽出と変換のプレビューのみ実行する",
    )
    parser.add_argument(
        "--show-external-warnings",
        action="store_true",
        help="--dry-run 時に外部参照（ZIP未収録ページ/DBリンク）警告のページ別詳細を表示する（デフォルトは件数のみ）",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="移行後のテキストベース自動整合性検証をスキップする",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="移行処理（Step 2）を実行せず、チェックポイントに基づいてStep 3の整合性検証のみを実行する",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="中断された移行処理をチェックポイントから再開する (Task B.6)",
    )
    parser.add_argument(
        "--page-workers",
        type=int,
        default=8,
        help="子ページ並行作成時のワーカースレッド数 (Task B.2, デフォルト: 8)",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default="migration_checkpoint.json",
        help="チェックポイントファイルの保存/読込先パス (デフォルト: migration_checkpoint.json)",
    )
    parser.add_argument(
        "--generate-debug-report",
        action="store_true",
        help="機密情報をマスキングした開発者報告用診断レポート (debug_report.json / debug_report.md) を生成する (Task B.5)",
    )
    parser.add_argument(
        "--debug-report-dir",
        type=str,
        default=".",
        help="診断レポートの出力先ディレクトリ (デフォルト: .)",
    )
    parser.add_argument(
        "--enrich-via-api",
        action="store_true",
        help="Notion APIを併用してDBのプロパティ型スキーマとビュー属性をピンポイントで補完する (Task B.3)",
    )
    parser.add_argument(
        "--dump-api-enrichment",
        type=str,
        nargs="?",
        const="reports/api_enrichment_dump.json",
        default=None,
        help="デバッグ用: --enrich-via-api で取得したDBプロパティ・View定義の詳細をJSONファイルにダンプする (デフォルト: reports/api_enrichment_dump.json)",
    )
    parser.add_argument(
        "--export-worksheet",
        type=str,
        nargs="?",
        const="db_worksheet.csv",
        default=None,
        help="データベース属性・ビューの人間確認用チェックシート (CSV/JSON) を出力して終了する (Task B.4, デフォルト: db_worksheet.csv)",
    )
    parser.add_argument(
        "--import-worksheet",
        type=str,
        default=None,
        help="人間が確認・修正したDB属性・ビューチェックシート (CSV/JSON) を読み込んで移行に適用する (Task B.4)",
    )
    parser.add_argument(
        "--external-mapping", "--import-mapping",
        dest="external_mapping",
        type=str,
        nargs="+",
        default=None,
        help="先行移行の実行結果マッピングファイル (migration_result.json 等) のパス。未収録の外部DB/ページ参照を解決するために事前ロードします (複数指定可、カンマ区切り可)",
    )
    parser.add_argument(
        "--timezone", "--tz",
        type=str,
        default=None,
        dest="timezone",
        help="時刻表示に使用するタイムゾーン (例: Asia/Tokyo, JST, UTC)。未指定時はローカル実行環境から自動判定",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Richインタラクティブ進捗ダッシュボードを無効化し、プレーンテキストログで出力する (Task B.10)",
    )
    parser.add_argument(
        "--user-mapping",
        type=str,
        default=None,
        help="NotionユーザーとConfluenceアカウントIDの対応表 (CSV/JSON) のパス (Task B.11)",
    )
    parser.add_argument(
        "--export-user-mapping",
        type=str,
        nargs="?",
        const="user_mapping.csv",
        default=None,
        help="移行ツリー内のNotionユーザーを抽出し、Confluence APIによる自動候補検索付きのマッピングCSVを出力して終了する (Task B.11, デフォルト: user_mapping.csv)",
    )
    parser.add_argument(
        "--metadata-template",
        type=str,
        default="default",
        help="ページ最上部に記載するNotion元情報（作成者、作成日時等）のテンプレート文字列またはファイルパス。空文字 \"\" または none を指定すると非表示になります。(変数: {author}, {created_time}, {last_edited_time}, {last_edited_by}, {title}, {url})",
    )
    parser.add_argument(
        "--show-all-pages",
        action="store_true",
        help="ページ数が多数の場合でもサマライズせず、全ページリスト・全階層ツリーを標準出力に展開表示する",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="移行先ページの権限を実行者本人のみのプライベート設定（閲覧・編集制限）にする",
    )
    parser.add_argument(
        "--visibility",
        type=str,
        choices=["space", "private"],
        default=None,
        help="移行先ページの公開範囲 ('space' または 'private', デフォルト: 'space')",
    )
    parser.add_argument(
        "--repair-inpage-links",
        action="store_true",
        help="Notion API を呼び出して、エクスポート時にアンカー (#) が欠落した自ページリンクを正確に特定・修復する追加PASSを実行する",
    )
    parser.add_argument(
        "--notion-token",
        type=str,
        default=os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or "",
        help="ページ内リンク修復用の Notion インテグレーション・トークン (環境変数 NOTION_TOKEN または NOTION_API_KEY も利用可能)",
    )
    parser.add_argument(
        "--individual-db-child-threshold",
        type=int,
        default=0,
        help="本文ありレコード数がこの件数以下のDBのみ個別子ページ（従来pageLink方式）で作成する閾値 (デフォルト: 0=全DBで1ページ集約方式)",
    )
    parser.add_argument(
        "--db-triage",
        nargs="?",
        const="reports/db_triage.csv",
        default=None,
        help="--dry-run 時に全DBの規模・移行方式トリアージ一覧を表示し、CSV出力する (デフォルト出力先: reports/db_triage.csv)",
    )
    parser.add_argument(
        "--import-db-triage",
        type=str,
        default=None,
        help="編集済みDBトリアージCSVを読み込み、DB単位の移行方式 (aggregated / individual / static_table / skip) を上書き適用する",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="詳細ログを出力する",
    )

    args = parser.parse_args()
    setup_logger(args.verbose)

    # 診断テレメトリとチェックポイント・シグナルマネージャーの初期化 (Task B.5, B.6, B.7)
    telemetry = SanitizedTelemetry()
    checkpoint = MigrationCheckpoint(checkpoint_path=args.checkpoint, save_interval_sec=2.0, max_pending_changes=50)

    if args.resume or args.verify_only:
        loaded = checkpoint.load()
        if not loaded:
            print(f"❌ エラー: チェックポイントファイル '{args.checkpoint}' が見つからないか読み込めません。")
            sys.exit(1)
        if args.verify_only:
            print(f"🔍 チェックポイントから整合性検証（Step 3）のみを実行します: {len(checkpoint.nodes)} 件のノードが記録済み (状態: {checkpoint.status})")
        else:
            print(f"🔄 チェックポイントから移行を再開します: {len(checkpoint.nodes)} 件のノードが記録済み (前回の状態: {checkpoint.status})")

        # 前回の実行パラメータ（全CLIオプション）をチェックポイントから自動復元
        saved_source = checkpoint.source_info or {}
        restored_opts = restore_cli_options_from_checkpoint(args, parser, saved_source)
        if args.verify_only:
            args.no_verify = False
        if restored_opts:
            if "verbose" in restored_opts and args.verbose:
                setup_logger(args.verbose)
            logging.info(f"[Resume] チェックポイントから前回のオプションを自動復元しました: {', '.join(restored_opts)}")
            print(f"   復元された前回オプション: {', '.join(restored_opts)}")

    # SIGINT/SIGTERM 安全中断ハンドラーの登録 (Task B.7)
    def _on_cancel_checkpoint():
        if not getattr(checkpoint, "write_failed", False):
            checkpoint.mark_interrupted()

    cancel_manager = CancellationManager(on_cancel_callback=_on_cancel_checkpoint)
    cancel_manager.install()

    # ZIPファイルの収集と厳格検証 (--zip / --zip-dir)
    zip_mode_requested = bool(args.zip_paths or args.zip_dir)
    zip_files = []
    if zip_mode_requested:
        try:
            if args.zip_paths:
                for p in args.zip_paths:
                    if os.path.exists(p):
                        zip_files.append(p)
                    else:
                        matched = sorted(glob.glob(p))
                        if matched:
                            zip_files.extend(matched)
                        else:
                            raise FileNotFoundError(f"指定されたZIPファイルが見つかりません: {p}")

            if args.zip_dir:
                if not os.path.exists(args.zip_dir):
                    raise FileNotFoundError(f"指定されたZIPディレクトリが見つかりません: {args.zip_dir}")
                if not os.path.isdir(args.zip_dir):
                    raise NotADirectoryError(f"指定された --zip-dir パスはディレクトリではありません: {args.zip_dir}")
                dir_zips = sorted(glob.glob(os.path.join(args.zip_dir, "*.zip")))
                if not dir_zips:
                    raise FileNotFoundError(f"指定されたディレクトリ内にZIPファイル (*.zip) が見つかりません: {args.zip_dir}")
                zip_files.extend(dir_zips)

            zip_files = sorted(list(dict.fromkeys(zip_files)))
            if not zip_files:
                raise FileNotFoundError("有効なZIPファイルが指定されていません。")

            for zp in zip_files:
                if not os.path.exists(zp):
                    raise FileNotFoundError(f"指定されたZIPファイルが見つかりません: {zp}")
                if not os.path.isfile(zp):
                    raise IsADirectoryError(f"指定されたZIPパスはファイルではありません: {zp}")
                with zipfile.ZipFile(zp, "r") as zf:
                    zf.namelist()
        except Exception as e:
            err_msg = f"指定されたZIPファイルを開けません: {e}"
            logging.error(err_msg, exc_info=args.verbose)
            print(f"❌ [ERROR] {err_msg}")
            sys.exit(1)

    use_zip = zip_mode_requested

    # 1. Notion設定の確認 (ZIP利用時はNOTION_TOKEN不要、ただし--enrich-via-api指定時は必要)
    notion_token = os.getenv("NOTION_TOKEN")
    if not use_zip and not notion_token:
        print("エラー: NOTION_TOKEN が設定されていません。")
        sys.exit(1)
    if args.enrich_via_api and not notion_token:
        print("エラー: --enrich-via-api を使用するには NOTION_TOKEN が設定されている必要があります。")
        sys.exit(1)

    notion_page_id = args.notion_page_id
    if not use_zip and not notion_page_id:
        # notion_sample_info.json があればそこから取得
        if os.path.exists("notion_sample_info.json"):
            with open("notion_sample_info.json", "r", encoding="utf-8") as f:
                info = json.load(f)
                root_page_info = info.get("resources", {}).get("root_page", {})
                notion_page_id = root_page_info.get("id")
        if not notion_page_id:
            notion_page_id = os.getenv("NOTION_PARENT_PAGE_ID")

    if not use_zip and not notion_page_id:
        print("エラー: 移行対象のNotionページIDが見つかりません。--notion-page-id または --zip で指定してください。")
        sys.exit(1)

    # 2. Confluence設定の確認
    conf_url = os.getenv("CONFLUENCE_URL")
    conf_user = os.getenv("CONFLUENCE_USERNAME")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN")
    conf_space = os.getenv("CONFLUENCE_SPACE_KEY")

    # Confluence移行先の親ページIDの決定
    # 優先順位: 1. CLI引数 (--confluence-parent-id) -> 2. .env (CONFLUENCE_PARENT_PAGE_ID) ※--new-tree 指定時を除く
    conf_parent_id = args.confluence_parent_id
    if not conf_parent_id and not args.new_tree:
        env_parent = os.getenv("CONFLUENCE_PARENT_PAGE_ID")
        if env_parent and env_parent.strip():
            conf_parent_id = env_parent.strip()

    if not args.dry_run and (not conf_url or not conf_user or not conf_token or not conf_space):
        print("エラー: Confluence の設定 (.env) が不足しています。")
        sys.exit(1)

    need_notion_check = (not use_zip) or bool(args.enrich_via_api) or bool(args.repair_inpage_links and notion_token)
    need_conf_check = not args.dry_run
    validate_api_credentials(
        notion_token=notion_token if need_notion_check else None,
        conf_url=conf_url if need_conf_check else None,
        conf_user=conf_user if need_conf_check else None,
        conf_token=conf_token if need_conf_check else None,
    )

    # タイトルプレフィックスの決定
    title_prefix = args.title_prefix
    if title_prefix is None and (use_zip or args.new_tree):
        title_prefix = "[HTML] "

    # 公開範囲（プライベートモード）の決定
    private_mode = bool(args.private or args.visibility == "private")

    # タイムゾーンの解決（CLI引数指定 or ローカル環境自動判定）
    from src.timezone_util import resolve_timezone, get_local_timezone_name
    resolved_tz = resolve_timezone(args.timezone)
    tz_display = args.timezone if args.timezone else f"{get_local_timezone_name()} (ローカル実行環境から自動判定)"

    print("==================================================")
    print("🚀 Notion to Confluence Migration Start")
    start_time = datetime.now(resolved_tz)
    print(f"Start Timestamp      : {start_time.isoformat()}")
    print(f"Timezone             : {tz_display}")
    if use_zip:
        print(f"Source               : Notion HTML Export ZIP x {len(zip_files)} ({', '.join(os.path.basename(z) for z in zip_files[:2])}...)")
    else:
        print(f"Notion Target Page ID : {notion_page_id}")
    if title_prefix:
        print(f"Title Prefix         : '{title_prefix}'")
    if not args.dry_run:
        print(f"Confluence Space     : {conf_space}")
        print(f"Confluence Parent ID : {conf_parent_id or '(Space Root - New Tree)'}")
        print(f"Overwrite Mode       : {not args.no_overwrite}")
    else:
        print("Mode                 : DRY RUN (Preview only)")
    print("==================================================\n")

    # Step 1: Notionデータ抽出 (ZIP or REST API)
    step1_start = datetime.now(resolved_tz)
    if use_zip:
        print(f"📥 [Step 1/3] Notion HTMLエクスポートZIP（{len(zip_files)}ファイル）から並行解析中... (開始: {step1_start.strftime('%H:%M:%S')})")
        try:
            extractor = MultiZipExtractor(zip_paths=zip_files)
            tree = extractor.extract_tree()
        except Exception as e:
            logging.error(f"HTMLエクスポート解析エラー: {e}", exc_info=args.verbose)
            print(f"❌ [ERROR] HTMLエクスポート解析エラー: {e}")
            sys.exit(1)
    else:
        print(f"📥 [Step 1/3] Notion APIからページ・ブロック・DBツリーを抽出中... (開始: {step1_start.strftime('%H:%M:%S')})")
        extractor = NotionExtractor(token=notion_token)
        try:
            tree = extractor.extract_tree(root_page_id=notion_page_id)
        except Exception as e:
            logging.error(f"Notionデータ抽出エラー: {e}", exc_info=args.verbose)
            sys.exit(1)

    # タイトルプレフィックスの再帰的適用
    if title_prefix:
        def _apply_prefix(node: PageNode):
            node.title = f"{title_prefix}{node.title}"
            for child in node.children:
                _apply_prefix(child)
        _apply_prefix(tree)

    step1_end = datetime.now(resolved_tz)
    step1_elapsed = (step1_end - step1_start).total_seconds()
    print(f"✓ Step 1 完了: 所要時間 {step1_elapsed:.2f} 秒\n")

    # Task B.3: Notion APIによるハイブリッド補完モード
    workspace_users: Optional[List[Dict[str, Any]]] = None
    if args.enrich_via_api:
        print("⚡ [Task B.3] Notion APIからDBプロパティスキーマ・ビュー属性をピンポイント取得中...")
        enricher = NotionApiEnricher(token=notion_token)
        if args.dump_api_enrichment:
            enrich_summary = enricher.enrich_tree(tree, dump_path=args.dump_api_enrichment)
        else:
            enrich_summary = enricher.enrich_tree(tree)
        ws_u = getattr(enricher, "workspace_users", None)
        if isinstance(ws_u, list):
            workspace_users = ws_u
        user_msg = ""
        if enrich_summary.get("users_fetched") or enrich_summary.get("users_enriched"):
            user_msg = f", {enrich_summary.get('users_fetched', 0)} 名のユーザーキャッシュ ({enrich_summary.get('users_enriched', 0)} 箇所補完)"
        print(f"✓ API補完完了: {enrich_summary['databases_enriched']} 件のデータベース, {enrich_summary['properties_mapped']} 件の正確なカラム型をマージしました{user_msg}")
        if args.dump_api_enrichment:
            print(f"📝 API補完詳細ダンプを出力しました: {args.dump_api_enrichment}")
        print()
    elif args.dump_api_enrichment:
        print("ℹ️ --dump-api-enrichment は --enrich-via-api 指定時のみ出力されます。")
    elif use_zip and notion_token and collect_external_synced_pages(tree)[0] > 0:
        NotionApiEnricher(token=notion_token).enrich_external_synced_blocks(tree)

    # Task B.4: 人間確認済みチェックシートの読込・適用
    if args.import_worksheet:
        print(f"📝 [Task B.4] 人間確認用チェックシート [{args.import_worksheet}] を読み込んで適用中...")
        ws_mgr = WorksheetManager()
        ws_data = ws_mgr.import_worksheet(args.import_worksheet)
        ws_stats = ws_mgr.apply_worksheet_to_tree(tree, ws_data)
        print(f"✓ チェックシート適用完了: {ws_stats['databases_updated']} 件のDB, {ws_stats['columns_updated']} カラム型, {ws_stats['views_updated']} ビューを承認設定に更新しました\n")

    # Task B.4: チェックシートのエクスポート要求時
    if args.export_worksheet:
        ws_mgr = WorksheetManager()
        ws_mgr.export_worksheet(tree, args.export_worksheet)
        print("==================================================")
        print(f"📋 [Task B.4] データベース属性・ビューの確認用チェックシートを出力しました:")
        print(f"   ファイルパス: {args.export_worksheet}")
        print("   確認・修正手順:")
        print(f"     1. {args.export_worksheet} をExcelまたはテキストエディタで開き、各カラムのデータ型およびビュー形式を確認・編集してください。")
        print(f"     2. 編集完了後、以下のコマンドでチェックシートを適用して移行を実行してください:")
        print(f"        python migrate.py --import-worksheet {args.export_worksheet} ...")
        print("==================================================")
        return

    # Task B.11: ユーザーマッピング定義のエクスポート要求時
    if args.export_user_mapping:
        conf_client = None
        if conf_url and conf_user and conf_token:
            from src.importer import ConfluenceClient
            conf_client = ConfluenceClient(conf_url, conf_user, conf_token)
        count = UserMappingManager.export_template(
            output_path=args.export_user_mapping,
            tree_node=tree,
            confluence_client=conf_client,
        )
        print("==================================================")
        print(f"👥 [Task B.11] ユーザーマッピング確認用CSVを出力しました ({count} 名検出):")
        print(f"   ファイルパス: {args.export_user_mapping}")
        print("   確認・編集手順:")
        print(f"     1. {args.export_user_mapping} を開き、Notionユーザーに対応する ConfluenceアカウントID を確認・編集してください。")
        print(f"     2. 編集完了後、以下のコマンドでマッピングを適用して移行を実行してください:")
        print(f"        python migrate.py --user-mapping {args.export_user_mapping} ...")
        print("==================================================")
        return

    # Task B.11: ユーザーマッピングの適用
    user_mapper = None
    if args.user_mapping:
        user_mapper = UserMappingManager(args.user_mapping)
        print(f"👥 ユーザーマッピング適用: {len(user_mapper.mappings)} 名の定義を読み込みました ({args.user_mapping})\n")

    # 同姓同名ユーザー検知
    homonym_warnings = UserMappingManager.detect_homonyms(
        tree,
        workspace_users=workspace_users,
        user_mapper=user_mapper,
    )

    # DBトリアージCSVの上書き読込
    db_mode_overrides: Dict[str, str] = {}
    if args.import_db_triage:
        try:
            db_mode_overrides = load_db_triage_overrides(args.import_db_triage)
            print(f"📋 DBトリアージ上書き定義を読み込みました: {len(db_mode_overrides)} 件 ({args.import_db_triage})\n")
        except Exception as e:
            err_msg = f"DBトリアージCSVの読み込みに失敗しました: {e}"
            logging.error(err_msg)
            print(f"❌ [ERROR] {err_msg}")
            sys.exit(1)

    # テレメトリとチェックポイントにツリー情報を記録
    telemetry.record_tree(tree)
    if not args.verify_only:
        checkpoint.source_info = build_checkpoint_source_info(
            args,
            use_zip=use_zip,
            zip_files=zip_files,
            title_prefix=title_prefix,
            notion_page_id=notion_page_id,
            conf_parent_id=conf_parent_id,
            conf_space=conf_space,
            private_mode=private_mode,
        )
        checkpoint.space_key = conf_space or ""
        checkpoint.parent_id = conf_parent_id
        try:
            checkpoint.save()
        except CheckpointWriteError as e:
            logging.error(f"チェックポイントの初期保存に失敗したため即時停止します: {e}")
            print(f"❌ エラー: チェックポイントファイル '{args.checkpoint}' への書き込みに失敗しました: {e}")
            telemetry.record_error(e, context="Step 1 Checkpoint Save")
            if args.generate_debug_report:
                json_p = os.path.join(args.debug_report_dir, "debug_report.json")
                md_p = os.path.join(args.debug_report_dir, "debug_report.md")
                telemetry.save_reports(json_path=json_p, md_path=md_p)
            sys.exit(1)

    print_tree_summary(
        tree,
        verbose=args.verbose,
        show_all=args.show_all_pages,
        individual_db_child_threshold=args.individual_db_child_threshold,
        db_mode_overrides=db_mode_overrides,
    )

    if args.dry_run:
        if homonym_warnings:
            print_homonym_warnings(homonym_warnings)
        print_dry_run_summary(
            tree,
            telemetry,
            show_external_warnings=args.show_external_warnings,
        )
        if args.db_triage:
            triage_rows = collect_db_triage_rows(
                tree,
                individual_db_child_threshold=args.individual_db_child_threshold,
                mode_overrides=db_mode_overrides,
            )
            print("\n" + format_db_triage_table(triage_rows))
            csv_out = export_db_triage_csv(triage_rows, args.db_triage)
            print(f"💾 DBトリアージCSVを出力しました: {csv_out}")
            print(f"   編集後に適用する場合: python migrate.py ... --import-db-triage {csv_out}")
        print("💡 DRY RUN モードのため、Confluence への移行はスキップされました。")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"📋 サニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        return

    def _run_step3_verification(confluence_client, target_mapping):
        step3_start = datetime.now(resolved_tz)
        print(f"🔍 [Step 3/3] テキストベース移行整合性を自動検証中... (開始: {step3_start.strftime('%H:%M:%S')})")
        verifier = MigrationVerifier(
            confluence_client,
            db_mode_overrides=db_mode_overrides or None,
            individual_db_child_threshold=getattr(args, "individual_db_child_threshold", None),
        )
        page_id_map = {nid: item["confluence_id"] for nid, item in target_mapping.items() if item.get("confluence_id")}
        results = verifier.verify_tree(tree, page_id_map, conf_parent_id)
        step3_end = datetime.now(resolved_tz)
        elapsed = (step3_end - step3_start).total_seconds()
        print(f"✓ Step 3 完了: 所要時間 {elapsed:.2f} 秒\n")
        print_verification_results(results, verbose=args.verbose, show_all=args.show_all_pages)
        return results, elapsed

    if args.verify_only:
        from src.importer import ConfluenceClient
        verify_client = ConfluenceClient(conf_url, conf_user, conf_token)
        mapping = checkpoint.get_migration_map()
        if not mapping:
            print("⚠️ 警告: チェックポイントに検証対象の移行済みノードが記録されていません。")
        _, step3_elapsed = _run_step3_verification(verify_client, mapping)
        end_time = datetime.now(resolved_tz)
        total_elapsed = (end_time - start_time).total_seconds()
        telemetry.record_timing("step1_extract_seconds", step1_elapsed)
        telemetry.record_timing("step3_verify_seconds", step3_elapsed)
        telemetry.record_timing("total_seconds", total_elapsed)
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"📋 サニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        print(f"🎉 検証のみモード完了 (抽出: {step1_elapsed:.2f}s | 検証: {step3_elapsed:.2f}s | 合計: {total_elapsed:.2f}s)")
        return

    # Step 2: Confluenceへインポート
    step2_start = datetime.now(resolved_tz)
    print(f"📤 [Step 2/3] Confluenceへ階層ツリーを移行中... (開始: {step2_start.strftime('%H:%M:%S')})")
    dashboard = MigrationDashboard(
        enabled=not args.no_dashboard,
        page_workers=args.page_workers,
        space_key=conf_space or "",
        title_prefix=title_prefix or "",
    )
    meta_tpl = args.metadata_template
    if use_zip and meta_tpl == "default":
        # HTMLエクスポート移行時はZIP更新日時の補完等は行わず、メタデータヘッダを出力しない
        meta_tpl = ""
    elif meta_tpl == "default":
        from src.converter import DEFAULT_METADATA_TEMPLATE
        meta_tpl = DEFAULT_METADATA_TEMPLATE
    elif meta_tpl.lower() in ("none", "false", "off", "0", ""):
        meta_tpl = ""

    importer = ConfluenceImporter(
        url=conf_url,
        username=conf_user,
        token=conf_token,
        space_key=conf_space,
        checkpoint=checkpoint,
        cancel_manager=cancel_manager,
        telemetry=telemetry,
        page_workers=args.page_workers,
        timezone=resolved_tz,
        dashboard=dashboard,
        user_mapper=user_mapper,
        metadata_template=meta_tpl,
        private_mode=private_mode,
        prune_blocks_on_pass2=(args.no_verify and not args.repair_inpage_links),
        title_prefix=title_prefix,
        external_mapping=args.external_mapping,
        individual_db_child_threshold=args.individual_db_child_threshold,
        db_mode_overrides=db_mode_overrides,
        verify_resume_remote=not (args.resume and args.no_verify),
    )

    def _safe_mark_interrupted():
        if getattr(checkpoint, "write_failed", False):
            return
        try:
            checkpoint.mark_interrupted()
        except CheckpointWriteError as ce:
            logging.error(f"中断時のチェックポイント保存に失敗しました: {ce}")

    setup_logger(args.verbose, dashboard_mode=dashboard.enabled)
    import_aborted_or_failed = False
    post_import_flush_error: Optional[CheckpointWriteError] = None
    try:
        mapping = importer.import_tree(
            root_node=tree,
            parent_id=conf_parent_id,
            overwrite=not args.no_overwrite,
        )
    except MigrationCancelledException:
        import_aborted_or_failed = True
        if cancel_manager.is_error_halt:
            logging.error(f"Migration aborted due to fatal error: {cancel_manager.halt_reason}")
            _safe_mark_interrupted()
            telemetry.record_error(Exception(cancel_manager.halt_reason or "Error halt"), context="Step 2 Import Tree")
            if args.generate_debug_report:
                json_p = os.path.join(args.debug_report_dir, "debug_report.json")
                md_p = os.path.join(args.debug_report_dir, "debug_report.md")
                telemetry.save_reports(json_path=json_p, md_path=md_p)
                print(f"🚨 開発者報告用のサニタイズド診断レポートを出力しました: {json_p}, {md_p}")
            sys.exit(1)
        else:
            print("\n" + "=" * 50)
            print("⏸️ 移行処理を安全に中断しました。")
            if not getattr(checkpoint, "write_failed", False):
                try:
                    checkpoint.mark_interrupted()
                    print(f"現在の進捗状況はチェックポイント '{args.checkpoint}' に保存されています。")
                    print("次回再開するには、以下のコマンドを実行してください:")
                    print(f"  python migrate.py --resume" + (f" --checkpoint {args.checkpoint}" if args.checkpoint != "migration_checkpoint.json" else ""))
                except CheckpointWriteError as ce:
                    logging.error(f"中断時のチェックポイント保存に失敗しました: {ce}")
                    print(f"❌ エラー: 中断時のチェックポイント保存に失敗しました: {ce}")
                    print("=" * 50)
                    sys.exit(1)
            print("=" * 50)
            if args.generate_debug_report:
                json_p = os.path.join(args.debug_report_dir, "debug_report.json")
                md_p = os.path.join(args.debug_report_dir, "debug_report.md")
                telemetry.save_reports(json_path=json_p, md_path=md_p)
                print(f"📋 サニタイズド診断レポートを出力しました: {json_p}, {md_p}")
            sys.exit(0)
    except Exception as e:
        import_aborted_or_failed = True
        logging.error(f"Confluence移行エラー: {e}", exc_info=args.verbose)
        _safe_mark_interrupted()
        telemetry.record_error(e, context="Step 2 Import Tree")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"🚨 開発者報告用のサニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        sys.exit(1)
    finally:
        try:
            if import_aborted_or_failed:
                if not getattr(checkpoint, "write_failed", False):
                    try:
                        checkpoint.flush()
                    except CheckpointWriteError as ce:
                        logging.error(f"終了処理中のチェックポイントフラッシュに失敗しました: {ce}")
            else:
                try:
                    checkpoint.flush()
                except CheckpointWriteError as ce:
                    post_import_flush_error = ce
        finally:
            setup_logger(args.verbose, dashboard_mode=False)

    if post_import_flush_error is not None:
        logging.error(f"Step 2 完了後のチェックポイントフラッシュに失敗したため即時停止します: {post_import_flush_error}")
        telemetry.record_error(post_import_flush_error, context="Step 2 Checkpoint Flush")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"🚨 開発者報告用のサニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        sys.exit(1)

    step2_end = datetime.now(resolved_tz)
    step2_elapsed = (step2_end - step2_start).total_seconds()
    print(f"✓ Step 2 完了: 所要時間 {step2_elapsed:.2f} 秒\n")

    # 追加修復PASS: Notion API によるページ内アンカー欠落リンク修復
    if args.repair_inpage_links:
        if not args.notion_token:
            print("⚠️ 警告: --repair-inpage-links が指定されましたが、--notion-token または環境変数 NOTION_TOKEN が未設定です。修復処理をスキップします。")
        else:
            from src.inpage_link_repairer import InPageLinkRepairer
            repairer = InPageLinkRepairer(
                confluence_client=importer.client,
                notion_token=args.notion_token,
            )
            repairer.repair_pages(root_node=tree, converter=importer.converter)

    # Step 3: テキストベース整合性検証
    step3_elapsed = 0.0
    verify_results = []
    if not args.no_verify:
        verify_results, step3_elapsed = _run_step3_verification(importer.client, mapping)

    end_time = datetime.now(resolved_tz)
    total_elapsed = (end_time - start_time).total_seconds()

    # テレメトリとチェックポイントの完了状態更新 (Task B.5, B.6)
    telemetry.record_timing("step1_extract_seconds", step1_elapsed)
    telemetry.record_timing("step2_import_seconds", step2_elapsed)
    telemetry.record_timing("step3_verify_seconds", step3_elapsed)
    telemetry.record_timing("total_seconds", total_elapsed)
    try:
        checkpoint.mark_completed()
    except CheckpointWriteError as e:
        logging.error(f"移行完了時のチェックポイント保存に失敗したためエラー終了します: {e}")
        telemetry.record_error(e, context="Step 3 Checkpoint Complete")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
        sys.exit(1)

    if args.generate_debug_report:
        json_p = os.path.join(args.debug_report_dir, "debug_report.json")
        md_p = os.path.join(args.debug_report_dir, "debug_report.md")
        telemetry.save_reports(json_path=json_p, md_path=md_p)
        print(f"\n📋 開発者報告用のサニタイズド診断レポートを出力しました:")
        print(f" - JSON形式: {json_p}")
        print(f" - Markdown形式: {md_p}")

    # 結果の保存 (詳細なタイミング分析データ付き)
    result_path = "migration_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "migrated_at": end_time.isoformat(),
                "space_key": conf_space,
                "pages_count": len(mapping),
                "timing": {
                    "started_at": start_time.isoformat(),
                    "finished_at": end_time.isoformat(),
                    "total_elapsed_seconds": round(total_elapsed, 2),
                    "step1_extract_seconds": round(step1_elapsed, 2),
                    "step2_import_seconds": round(step2_elapsed, 2),
                    "step3_verify_seconds": round(step3_elapsed, 2),
                },
                "mapping": mapping,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    try:
        os.chmod(result_path, 0o600)
    except Exception:
        pass

    raw_p2_errors = getattr(importer, "pass2_errors", None)
    pass2_errors = raw_p2_errors if isinstance(raw_p2_errors, list) else []
    raw_tel_errors = getattr(telemetry, "errors", None)
    telemetry_errors = raw_tel_errors if isinstance(raw_tel_errors, list) else []
    has_import_errors = bool(pass2_errors or telemetry_errors)

    print("\n==================================================")
    if has_import_errors:
        _safe_mark_interrupted()
        total_err_count = max(len(pass2_errors), len(telemetry_errors))
        print(f"⚠️ 移行処理中にエラーが発生しました（失敗: {total_err_count} 件）。")
        for pe in pass2_errors[:5]:
            print(f"  - [{pe.get('title')}] (ID: {pe.get('confluence_id')}): {pe.get('error')}")
        if len(pass2_errors) > 5:
            print(f"  ... 他 {len(pass2_errors) - 5} 件")
        if not getattr(checkpoint, "write_failed", False):
            print(f"現在の進捗状況はチェックポイント '{args.checkpoint}' に保存されています。")
            print("未完了ページの更新を再開するには、以下のコマンドを実行してください:")
            print(f"  python migrate.py --resume" + (f" --checkpoint {args.checkpoint}" if args.checkpoint != "migration_checkpoint.json" else ""))
        print("==================================================")
        sys.exit(1)

    print("🎉 移行が正常に完了しました！")
    print(f"開始時刻: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"終了時刻: {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"総所要時間: {total_elapsed:.2f} 秒 (抽出: {step1_elapsed:.2f}s | 移行: {step2_elapsed:.2f}s | 検証: {step3_elapsed:.2f}s)")
    print(f"移行済みページ数: {len(mapping)} 件")
    print(f"結果ログファイル: {result_path}")
    print_result_summary(mapping, result_path, show_all=args.show_all_pages, verbose=args.verbose)

    raw_tel_warnings = getattr(telemetry, "warnings", None)
    tel_warnings = raw_tel_warnings if isinstance(raw_tel_warnings, list) else []
    if tel_warnings:
        print(f"\n⚠️ 移行中の警告 ({len(tel_warnings)} 件):")
        for tw in tel_warnings[:10]:
            print(f"  - {tw}")
        if len(tel_warnings) > 10:
            print(f"  ... 他 {len(tel_warnings) - 10} 件（詳細はログまたは --generate-debug-report を参照）")

    pending_dbs = [item for item in mapping.values() if item.get("node_type") == "database" and item.get("pending_row_views")]
    if pending_dbs:
        print("\nℹ️ [Approach B / 非同期インデックス情報]")
        for pdb in pending_dbs:
            print(f" - データベース [{pdb['title']}]: {len(pdb['pending_row_views'])} 件の子ページの行ビューがバックエンドでインデックス処理中です。")
        print("   Confluence側でのインデックス完了後、以下のコマンドで残りの行ビューを安全に完全同期できます:")
        print(f"   python migrate.py --resume" + (f" --checkpoint {args.checkpoint}" if args.checkpoint != "migration_checkpoint.json" else ""))
    print("==================================================")


if __name__ == "__main__":
    main()

