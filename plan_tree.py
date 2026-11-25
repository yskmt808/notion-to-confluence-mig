#!/usr/bin/env python3
"""
移行計画作成用の Notion ページツリー調査ツール。

- API 調査モード (--api): POST /v1/search + レートリミット制御 + キャッシュ再開で最小 API コールでツリーを構築
- ZIP 分析モード (--zip / --zip-dir): Notion HTML エクスポート ZIP から API コールなしでツリーを構築
- 出力 (方式B): <output-dir>/pages.csv (1ページ1行のUTF-8-BOM CSV) と <output-dir>/summary.md
"""

import argparse
import glob
import logging
import os
import sys
import time
from typing import List

from dotenv import load_dotenv

from src.tree_planner import (
    ApiTreeScanner,
    analyze_zips_lightweight,
    build_summary_markdown,
    build_zip_stats_markdown,
    count_unique_db_csvs,
    count_zip_source_files,
    export_pages_csv,
    export_summary_markdown,
    export_zip_stats_csvs,
    flatten_plan_trees,
    load_exclude_ids,
    prune_excluded_subtrees,
    scan_zips_fast_tree,
    scan_zips_to_plan_trees,
)


def parse_args(argv: List[str] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="移行計画作成用のNotionページツリー調査ツール (1ページ1行CSV + Markdownサマリー出力)",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--api",
        action="store_true",
        help="Notion API (POST /v1/search) を使用してツリーを調査するモード",
    )
    mode_group.add_argument(
        "--zip",
        nargs="+",
        dest="zip_paths",
        help="Notion HTMLエクスポートZIPファイルのパス（複数指定可）",
    )
    mode_group.add_argument(
        "--zip-dir",
        help="複数のNotion HTMLエクスポートZIPが格納されたディレクトリ",
    )
    mode_group.add_argument(
        "--from-cache",
        dest="from_cache",
        help="保存済みのAPI検索キャッシュJSON (api_search_cache.json) からAPI呼び出し0回でツリー/サブツリーを抽出するモード",
    )

    parser.add_argument(
        "--notion-token",
        default="",
        help="Notion Integration Token（未指定時は環境変数 NOTION_TOKEN / NOTION_API_KEY を参照）",
    )
    parser.add_argument(
        "--root-page-id",
        default="",
        help="(--api / --from-cache / --zip / --zip-dir 時) 調査対象とするルートページ/DBのUUID（未指定時は全体）",
    )
    parser.add_argument(
        "--subtree-only",
        action="store_true",
        help="(--api + --root-page-id時) POST /v1/search を使わず blocks/{id}/children でサブツリー内のみを直接探索する（DB行は辿らない高速モード）",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=2.8,
        help="(--api時) 1秒あたりの最大APIリクエスト数 (デフォルト: 2.8)",
    )
    parser.add_argument(
        "--cache-file",
        default="",
        help="(--api時) 検索結果の中間キャッシュJSONパス (デフォルト: <output-dir>/api_search_cache.json)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="(--api時) キャッシュファイルから中断箇所を再開する",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/tree_plan",
        help="pages.csv と summary.md の出力先ディレクトリ (デフォルト: reports/tree_plan)",
    )
    parser.add_argument(
        "--exclude-db-rows",
        action="store_true",
        help="pages.csv の明細行から DB行レコード (database_row) を除外する（サマリーとdb_row_countは維持）",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="(--zip / --zip-dir 時) HTMLフルパースを行わず、ZIPメタデータとCSV行数カウントのみで規模感(zip_stats.md / zip_databases.csv / zip_folders.csv)を高速算出する",
    )
    parser.add_argument(
        "--fast-tree",
        action="store_true",
        help="(--zip / --zip-dir 時) HTML本文パースをスキップし、ZIPパス階層・<title>・CSV行数のみから高速にpages.csv / summary.mdを生成する",
    )
    parser.add_argument(
        "--exclude-ids",
        nargs="+",
        default=[],
        help="分析・ツリー出力から除外するページ/DBのNotion UUID（複数指定可、配下のサブツリーも除外）",
    )
    parser.add_argument(
        "--exclude-file",
        default="",
        help="分析対象外のページ/DB UUIDを1行1件で記載したテキスト/CSVファイルパス（#コメント可）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="詳細ログを出力する",
    )
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    t0 = time.monotonic()
    api_calls = 0
    include_db_rows = not args.exclude_db_rows
    exclude_ids = load_exclude_ids(args.exclude_ids, args.exclude_file or None)
    if exclude_ids:
        print(f"🚫 除外リスト読み込み: {len(exclude_ids)} 件のページ/DB（および配下サブツリー）を分析対象外にします")

    if args.api:
        token = args.notion_token or os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY") or ""
        if not token:
            print("エラー: --api モードには --notion-token または環境変数 NOTION_TOKEN が必要です。", file=sys.stderr)
            return 1
        if args.subtree_only and not args.root_page_id:
            print("エラー: --subtree-only を使用する場合は --root-page-id を指定してください。", file=sys.stderr)
            return 1
        cache_path = args.cache_file or os.path.join(args.output_dir, "api_search_cache.json")
        scanner = ApiTreeScanner(
            token=token,
            rate_limit=args.rate_limit,
            cache_file=cache_path,
            resume=args.resume,
        )
        if args.subtree_only:
            print(f"🔍 Notion API (blocks/children) でサブツリー [{args.root_page_id}] 配下を直接探索中...")

            def _on_subtree_progress(page_cnt: int, db_cnt: int, q_len: int) -> None:
                elapsed_s = time.monotonic() - t0
                print(
                    f"  ... ページ {page_cnt}件 / DB {db_cnt}件 (残りキュー: {q_len}件, API呼出: {scanner.api_calls}回, {elapsed_s:.1f}s)",
                    flush=True,
                )

            roots = scanner.scan_subtree(
                root_page_id=args.root_page_id,
                progress_cb=_on_subtree_progress,
                exclude_ids=exclude_ids,
            )
            source_label = f"Notion API (blocks/children subtree) [root={args.root_page_id}]"
        else:
            print("🔍 Notion API (POST /v1/search) でページツリーを探索中...")

            def _on_search_progress(cnt: int) -> None:
                elapsed_s = max(0.01, time.monotonic() - t0)
                st = scanner.last_search_stats
                rate = cnt / elapsed_s
                latest = (st.get("latest_title") or "")[:30]
                print(
                    f"  ... {cnt}件取得 (page:{st.get('page', 0)} / db:{st.get('database', 0)} / row:{st.get('database_row', 0)}) "
                    f"| API:{scanner.api_calls}回 | {elapsed_s:.1f}s ({rate:.1f}件/s) | 最新: {latest}",
                    flush=True,
                )

            def _on_parent_progress(done: int, total: int) -> None:
                if done == total or done % 10 == 0:
                    pct = (done / total * 100.0) if total else 100.0
                    print(f"  ... 親ブロック解決中: {done}/{total} ({pct:.0f}%)", flush=True)

            roots = scanner.scan(
                root_page_id=args.root_page_id or None,
                progress_cb=_on_search_progress,
                parent_progress_cb=_on_parent_progress,
            )
            source_label = "Notion API (POST /v1/search)"
            if args.root_page_id:
                source_label += f" [root={args.root_page_id}]"
        api_calls = scanner.api_calls
    elif args.from_cache:
        print(f"💾 キャッシュ [{args.from_cache}] からAPI呼び出しなしでページツリーを再構築中...")
        roots = ApiTreeScanner.build_trees_from_cache(
            cache_file=args.from_cache,
            root_page_id=args.root_page_id or None,
        )
        source_label = f"API Cache ({os.path.basename(args.from_cache)})"
        if args.root_page_id:
            source_label += f" [root={args.root_page_id}]"
    else:
        zip_files: List[str] = []
        if args.zip_paths:
            zip_files = list(args.zip_paths)
        elif args.zip_dir:
            zip_files = sorted(glob.glob(os.path.join(args.zip_dir, "*.zip")))
            if not zip_files:
                print(f"エラー: ディレクトリ '{args.zip_dir}' にZIPファイルが見つかりません。", file=sys.stderr)
                return 1
        html_cnt, csv_cnt = count_zip_source_files(zip_files)
        unique_db_cnt = count_unique_db_csvs(zip_files)
        file_summary = f"{len(zip_files)}件, HTML:{html_cnt}件 / DB CSV:{unique_db_cnt}件 (全CSV:{csv_cnt}件)"
        if args.stats_only:
            print(f"⚡ エクスポートZIP ({file_summary}) の軽量規模算出を実行中...")
            stats = analyze_zips_lightweight(
                zip_files,
                progress_cb=lambda idx, tot, name: print(f"  ... [{idx}/{tot}] {name}", flush=True),
                exclude_ids=exclude_ids,
            )
            elapsed = time.monotonic() - t0
            os.makedirs(args.output_dir, exist_ok=True)
            stats_md = build_zip_stats_markdown(stats, elapsed_seconds=elapsed)
            md_path = os.path.join(args.output_dir, "zip_stats.md")
            export_summary_markdown(stats_md, md_path)
            db_csv_path, folder_csv_path = export_zip_stats_csvs(stats, args.output_dir)
            print("")
            print(stats_md)
            print("✅ 軽量規模算出完了:")
            print(f"   - サマリー (Markdown): {md_path}")
            print(f"   - DB一覧CSV          : {db_csv_path}")
            print(f"   - フォルダ一覧CSV    : {folder_csv_path}")
            return 0
        if args.fast_tree:
            print(f"⚡ エクスポートZIP ({file_summary}) から超高速ツリー構築中 (--fast-tree)...")
            roots = scan_zips_fast_tree(
                zip_files,
                root_page_id=args.root_page_id or None,
                include_db_rows=include_db_rows,
                exclude_ids=exclude_ids,
                progress_cb=lambda idx, tot, name: print(f"  ... [{idx}/{tot}] {name}", flush=True),
            )
            source_label = f"Export ZIP fast-tree x {len(zip_files)} ({os.path.basename(zip_files[0])})"
        else:
            print(f"📦 エクスポートZIP ({file_summary}) からページツリーを解析中...")
            roots = scan_zips_to_plan_trees(zip_files, root_page_id=args.root_page_id or None)
            source_label = f"Export ZIP x {len(zip_files)} ({os.path.basename(zip_files[0])})"
        if args.root_page_id:
            source_label += f" [root={args.root_page_id}]"

    if exclude_ids:
        roots = prune_excluded_subtrees(roots, exclude_ids)

    rows = flatten_plan_trees(roots, include_db_rows=include_db_rows)
    elapsed = time.monotonic() - t0

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = os.path.join(args.output_dir, "pages.csv")
    md_path = os.path.join(args.output_dir, "summary.md")

    export_pages_csv(rows, csv_path)
    summary_md = build_summary_markdown(
        roots,
        rows,
        source_label=source_label,
        api_calls=api_calls,
        elapsed_seconds=elapsed,
    )
    export_summary_markdown(summary_md, md_path)

    print("")
    print(summary_md)
    print(f"✅ 出力完了:")
    print(f"   - 明細CSV (1ページ1行): {csv_path}")
    print(f"   - サマリー (Markdown)  : {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
