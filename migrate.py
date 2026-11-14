#!/usr/bin/env python3
"""
Notion to Confluence 移行メインスクリプト。
Notion からページツリー・DBを取得し、Confluence 上へ同一階層で移行します。
"""

import os
import sys
import json
import logging
import argparse
import warnings
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", category=UserWarning)

import glob
from src.extractor import NotionExtractor, PageNode
from src.html_extractor import HtmlZipExtractor, MultiZipExtractor
from src.importer import ConfluenceImporter
from src.verifier import MigrationVerifier
from src.checkpoint import MigrationCheckpoint
from src.cancellation import CancellationManager, MigrationCancelledException
from src.telemetry import SanitizedTelemetry
from src.api_enricher import NotionApiEnricher
from src.worksheet import WorksheetManager
from src.dashboard import MigrationDashboard
from src.user_mapper import UserMappingManager
from src.logger_util import setup_notice_log_level, setup_uncaught_exception_handler

setup_notice_log_level()
setup_uncaught_exception_handler()

load_dotenv()


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


def collect_tree_stats(node: PageNode) -> Dict[str, Any]:
    stats = {
        "total": 0,
        "pages": 0,
        "databases": 0,
        "database_rows": 0,
        "max_depth": 0,
    }

    def _traverse(n: PageNode, depth: int):
        stats["total"] += 1
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
    return stats


def print_tree_summary(root: PageNode, verbose: bool = False, show_all: bool = False):
    """
    Notionツリー構造の標準出力を表示する。
    ページ数が多数（25件超）の場合は、端末画面の圧迫を防ぐため
    主要セクションとノード種別内訳をサマライズして表示する。
    """
    stats = collect_tree_stats(root)
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
        print(f"  • 📋 データベース行  : {stats['database_rows']} 件")
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

        c_stats = collect_tree_stats(child)
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


def print_verification_results(verify_results: List[Dict[str, Any]], verbose: bool = False, show_all: bool = False):
    """整合性検証結果をサマライズ表示する。"""
    pass_count = sum(1 for r in verify_results if r["status"] == "PASS")
    warn_count = sum(1 for r in verify_results if r["status"] == "WARN")
    fail_count = sum(1 for r in verify_results if r["status"] == "FAIL")

    print("--------------------------------------------------")
    print(f"検証サマリー: 合計 {len(verify_results)} ページ | 正常: {pass_count} | 警告: {warn_count} | 失敗: {fail_count}")

    threshold = 25
    if len(verify_results) <= threshold or verbose or show_all:
        for r in verify_results:
            icon = "✅" if r["status"] == "PASS" else ("⚠️" if r["status"] == "WARN" else "❌")
            print(f"{icon} [{r['status']}] {r['title']} (ID: {r['page_id']})")
            for chk in r["checks_passed"]:
                if verbose:
                    print(f"     ✓ {chk}")
            for w in r["warnings"]:
                print(f"     ⚠️ {w}")
            for iss in r["issues"]:
                print(f"     ❌ {iss}")
    else:
        # 大規模時は問題（警告・失敗）のあるページのみを抽出して報告
        abnormal_results = [r for r in verify_results if r["status"] != "PASS"]
        if abnormal_results:
            print(f"\n⚠️ 確認・対応が必要なページ ({len(abnormal_results)} 件):")
            for r in abnormal_results:
                icon = "⚠️" if r["status"] == "WARN" else "❌"
                print(f"{icon} [{r['status']}] {r['title']} (ID: {r['page_id']})")
                for w in r["warnings"]:
                    print(f"     ⚠️ {w}")
                for iss in r["issues"]:
                    print(f"     ❌ {iss}")
        else:
            print("✅ 全ページが整合性検証に完全合格しました（警告・エラーなし）。")
        print("💡 全ページの個別検証項目を表示するには `--verbose` を指定してください。")


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
        "--no-verify",
        action="store_true",
        help="移行後のテキストベース自動整合性検証をスキップする",
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
        default="space",
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
        "-v", "--verbose",
        action="store_true",
        help="詳細ログを出力する",
    )

    args = parser.parse_args()
    setup_logger(args.verbose)

    # 診断テレメトリとチェックポイント・シグナルマネージャーの初期化 (Task B.5, B.6, B.7)
    telemetry = SanitizedTelemetry()
    checkpoint = MigrationCheckpoint(checkpoint_path=args.checkpoint, save_interval_sec=2.0, max_pending_changes=50)

    if args.resume:
        loaded = checkpoint.load()
        if not loaded:
            print(f"❌ エラー: チェックポイントファイル '{args.checkpoint}' が見つからないか読み込めません。")
            sys.exit(1)
        print(f"🔄 チェックポイントから移行を再開します: {len(checkpoint.nodes)} 件のノードが記録済み (前回の状態: {checkpoint.status})")

        # 前回の実行パラメータをチェックポイントから自動復元
        saved_source = checkpoint.source_info or {}
        if not args.zip_dir and not args.zip_paths:
            if saved_source.get("zip_dir"):
                args.zip_dir = saved_source["zip_dir"]
                logging.info(f"[Resume] チェックポイントから --zip-dir '{args.zip_dir}' を自動復元しました。")
            elif saved_source.get("zip_paths"):
                args.zip_paths = saved_source["zip_paths"]
                logging.info(f"[Resume] チェックポイントから --zip '{args.zip_paths}' を自動復元しました。")
        if args.title_prefix is None and saved_source.get("title_prefix") is not None:
            args.title_prefix = saved_source["title_prefix"]
            logging.info(f"[Resume] チェックポイントから --title-prefix '{args.title_prefix}' を自動復元しました。")
        if not args.notion_page_id and saved_source.get("notion_page_id"):
            args.notion_page_id = saved_source["notion_page_id"]
        if not args.confluence_parent_id and saved_source.get("confluence_parent_id"):
            args.confluence_parent_id = saved_source["confluence_parent_id"]
        if saved_source.get("page_workers") and args.page_workers == 8:
            args.page_workers = saved_source["page_workers"]

    # SIGINT/SIGTERM 安全中断ハンドラーの登録 (Task B.7)
    cancel_manager = CancellationManager(on_cancel_callback=lambda: checkpoint.mark_interrupted())
    cancel_manager.install()

    # ZIPファイルの収集 (--zip / --zip-dir)
    zip_files = []
    if args.zip_paths:
        for p in args.zip_paths:
            matched = glob.glob(p)
            if matched:
                zip_files.extend(matched)
            elif os.path.exists(p):
                zip_files.append(p)
    if args.zip_dir and os.path.isdir(args.zip_dir):
        zip_files.extend(sorted(glob.glob(os.path.join(args.zip_dir, "*.zip"))))
    zip_files = sorted(list(dict.fromkeys(zip_files)))

    use_zip = bool(zip_files)

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

    # タイトルプレフィックスの決定
    title_prefix = args.title_prefix
    if title_prefix is None and (use_zip or args.new_tree):
        title_prefix = "[HTML] "

    # タイムゾーンの解決（CLI引数指定 or ローカル環境自動判定）
    from src.timezone_util import resolve_timezone, get_local_timezone_name
    resolved_tz = resolve_timezone(args.timezone)
    tz_display = args.timezone if args.timezone else f"{get_local_timezone_name()} (ローカル実行環境から自動判定)"

    print("==================================================")
    print("🚀 Notion to Confluence Migration Start")
    start_time = datetime.now(timezone.utc)
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
    step1_start = datetime.now(timezone.utc)
    if use_zip:
        print(f"📥 [Step 1/3] Notion HTMLエクスポートZIP（{len(zip_files)}ファイル）から並行解析中... (開始: {step1_start.strftime('%H:%M:%S')})")
        extractor = MultiZipExtractor(zip_paths=zip_files)
        try:
            tree = extractor.extract_tree()
        except Exception as e:
            logging.error(f"HTMLエクスポート解析エラー: {e}", exc_info=args.verbose)
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

    step1_end = datetime.now(timezone.utc)
    step1_elapsed = (step1_end - step1_start).total_seconds()
    print(f"✓ Step 1 完了: 所要時間 {step1_elapsed:.2f} 秒\n")

    # Task B.3: Notion APIによるハイブリッド補完モード
    workspace_users: Optional[List[Dict[str, Any]]] = None
    if args.enrich_via_api:
        print("⚡ [Task B.3] Notion APIからDBプロパティスキーマ・ビュー属性をピンポイント取得中...")
        enricher = NotionApiEnricher(token=notion_token)
        enrich_summary = enricher.enrich_tree(tree)
        ws_u = getattr(enricher, "workspace_users", None)
        if isinstance(ws_u, list):
            workspace_users = ws_u
        user_msg = ""
        if enrich_summary.get("users_fetched") or enrich_summary.get("users_enriched"):
            user_msg = f", {enrich_summary.get('users_fetched', 0)} 名のユーザーキャッシュ ({enrich_summary.get('users_enriched', 0)} 箇所補完)"
        print(f"✓ API補完完了: {enrich_summary['databases_enriched']} 件のデータベース, {enrich_summary['properties_mapped']} 件の正確なカラム型をマージしました{user_msg}\n")

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

    # テレメトリとチェックポイントにツリー情報を記録
    telemetry.record_tree(tree)
    checkpoint.source_info = {
        "use_zip": use_zip,
        "zip_paths": zip_files,
        "zip_dir": args.zip_dir,
        "title_prefix": title_prefix,
        "notion_page_id": notion_page_id,
        "confluence_parent_id": conf_parent_id,
        "space_key": conf_space,
        "page_workers": args.page_workers,
    }
    checkpoint.space_key = conf_space or ""
    checkpoint.parent_id = conf_parent_id
    checkpoint.save()

    print_tree_summary(tree, verbose=args.verbose, show_all=args.show_all_pages)

    if args.dry_run:
        if homonym_warnings:
            print_homonym_warnings(homonym_warnings)
        print("💡 DRY RUN モードのため、Confluence への移行はスキップされました。")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"📋 サニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        return

    # Step 2: Confluenceへインポート
    step2_start = datetime.now(timezone.utc)
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
        private_mode=(args.private or args.visibility == "private"),
        prune_blocks_on_pass2=(args.no_verify and not args.repair_inpage_links),
    )

    setup_logger(args.verbose, dashboard_mode=dashboard.enabled)
    try:
        mapping = importer.import_tree(
            root_node=tree,
            parent_id=conf_parent_id,
            overwrite=not args.no_overwrite,
        )
    except MigrationCancelledException:
        if cancel_manager.is_error_halt:
            logging.error(f"Migration aborted due to fatal error: {cancel_manager.halt_reason}")
            checkpoint.mark_interrupted()
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
            print(f"現在の進捗状況はチェックポイント '{args.checkpoint}' に保存されています。")
            print("次回再開するには、以下のコマンドを実行してください:")
            print(f"  python migrate.py --resume" + (f" --checkpoint {args.checkpoint}" if args.checkpoint != "migration_checkpoint.json" else ""))
            print("=" * 50)
            checkpoint.mark_interrupted()
            if args.generate_debug_report:
                json_p = os.path.join(args.debug_report_dir, "debug_report.json")
                md_p = os.path.join(args.debug_report_dir, "debug_report.md")
                telemetry.save_reports(json_path=json_p, md_path=md_p)
                print(f"📋 サニタイズド診断レポートを出力しました: {json_p}, {md_p}")
            sys.exit(0)
    except Exception as e:
        logging.error(f"Confluence移行エラー: {e}", exc_info=args.verbose)
        checkpoint.mark_interrupted()
        telemetry.record_error(e, context="Step 2 Import Tree")
        if args.generate_debug_report:
            json_p = os.path.join(args.debug_report_dir, "debug_report.json")
            md_p = os.path.join(args.debug_report_dir, "debug_report.md")
            telemetry.save_reports(json_path=json_p, md_path=md_p)
            print(f"🚨 開発者報告用のサニタイズド診断レポートを出力しました: {json_p}, {md_p}")
        sys.exit(1)
    finally:
        checkpoint.flush()
        setup_logger(args.verbose, dashboard_mode=False)

    step2_end = datetime.now(timezone.utc)
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
        step3_start = datetime.now(timezone.utc)
        print(f"🔍 [Step 3/3] テキストベース移行整合性を自動検証中... (開始: {step3_start.strftime('%H:%M:%S')})")
        verifier = MigrationVerifier(importer.client)
        page_id_map = {nid: item["confluence_id"] for nid, item in mapping.items()}
        verify_results = verifier.verify_tree(tree, page_id_map, conf_parent_id)

        step3_end = datetime.now(timezone.utc)
        step3_elapsed = (step3_end - step3_start).total_seconds()
        print(f"✓ Step 3 完了: 所要時間 {step3_elapsed:.2f} 秒\n")

        print_verification_results(verify_results, verbose=args.verbose, show_all=args.show_all_pages)

    end_time = datetime.now(timezone.utc)
    total_elapsed = (end_time - start_time).total_seconds()

    # テレメトリとチェックポイントの完了状態更新 (Task B.5, B.6)
    telemetry.record_timing("step1_extract_seconds", step1_elapsed)
    telemetry.record_timing("step2_import_seconds", step2_elapsed)
    telemetry.record_timing("step3_verify_seconds", step3_elapsed)
    telemetry.record_timing("total_seconds", total_elapsed)
    checkpoint.mark_completed()

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

    print("\n==================================================")
    print("🎉 移行が正常に完了しました！")
    print(f"開始時刻: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"終了時刻: {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"総所要時間: {total_elapsed:.2f} 秒 (抽出: {step1_elapsed:.2f}s | 移行: {step2_elapsed:.2f}s | 検証: {step3_elapsed:.2f}s)")
    print(f"移行済みページ数: {len(mapping)} 件")
    print(f"結果ログファイル: {result_path}")
    print_result_summary(mapping, result_path, show_all=args.show_all_pages, verbose=args.verbose)

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

