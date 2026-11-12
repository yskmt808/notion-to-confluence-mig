#!/usr/bin/env python3
"""
深さ優先並行実行アルゴリズム検証用の5階層Notion HTMLエクスポートZIP生成スクリプト。
"""

import os
import zipfile
import uuid

def generate_page_html(title: str, page_id: str, content: str = "") -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
    .page-title {{ font-size: 2.25rem; font-weight: 700; margin-bottom: 0.5rem; }}
    .page-body {{ margin-top: 1rem; }}
  </style>
</head>
<body>
  <article id="{page_id}" class="page">
    <header>
      <h1 class="page-title">{title}</h1>
    </header>
    <div class="page-body">
      <p>{content or f"{title} のドキュメント本文です。"}</p>
    </div>
  </article>
</body>
</html>
"""

def create_5level_zip(output_zip_path: str):
    os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
    
    # ノード定義（階層構造）
    nodes = {
        # Level 1 (Root)
        "root": {
            "title": "Level1_総合開発ポータル",
            "id": "11111111111111111111111111111111",
            "path": "Level1_総合開発ポータル 11111111111111111111111111111111.html",
            "dir": "Level1_総合開発ポータル/",
        },
        # Level 2
        "l2_a": {
            "title": "Level2_プロダクト基盤統括部",
            "id": "2222222222222222222222222222222a",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部 2222222222222222222222222222222a.html",
            "dir": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/",
        },
        "l2_b": {
            "title": "Level2_プラットフォームSRE統括部",
            "id": "2222222222222222222222222222222b",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部 2222222222222222222222222222222b.html",
            "dir": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/",
        },
        # Level 3 (Branch A)
        "l3_a1": {
            "title": "Level3_フロントエンド開発グループ",
            "id": "333333333333333333333333333333a1",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ 333333333333333333333333333333a1.html",
            "dir": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ/",
        },
        "l3_a2": {
            "title": "Level3_バックエンド開発グループ",
            "id": "333333333333333333333333333333a2",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ 333333333333333333333333333333a2.html",
            "dir": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ/",
        },
        # Level 3 (Branch B)
        "l3_b1": {
            "title": "Level3_クラウドインフラ基盤グループ",
            "id": "333333333333333333333333333333b1",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ 333333333333333333333333333333b1.html",
            "dir": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ/",
        },
        "l3_b2": {
            "title": "Level3_Observability監視グループ",
            "id": "333333333333333333333333333333b2",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ 333333333333333333333333333333b2.html",
            "dir": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ/",
        },
        # Level 4
        "l4_a1": {
            "title": "Level4_Webコンポーネント刷新PJ",
            "id": "444444444444444444444444444444a1",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ/Level4_Webコンポーネント刷新PJ 444444444444444444444444444444a1.html",
            "dir": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ/Level4_Webコンポーネント刷新PJ/",
        },
        "l4_a2": {
            "title": "Level4_GraphQL_Gateway統合PJ",
            "id": "444444444444444444444444444444a2",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ/Level4_GraphQL_Gateway統合PJ 444444444444444444444444444444a2.html",
            "dir": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ/Level4_GraphQL_Gateway統合PJ/",
        },
        "l4_b1": {
            "title": "Level4_マルチリージョンk8s移行PJ",
            "id": "444444444444444444444444444444b1",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ/Level4_マルチリージョンk8s移行PJ 444444444444444444444444444444b1.html",
            "dir": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ/Level4_マルチリージョンk8s移行PJ/",
        },
        "l4_b2": {
            "title": "Level4_OpenTelemetry標準化PJ",
            "id": "444444444444444444444444444444b2",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ/Level4_OpenTelemetry標準化PJ 444444444444444444444444444444b2.html",
            "dir": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ/Level4_OpenTelemetry標準化PJ/",
        },
        # Level 5
        "l5_a1_1": {
            "title": "Level5_UI設計・DesignToken仕様書",
            "id": "555555555555555555555555555555a1",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ/Level4_Webコンポーネント刷新PJ/Level5_UI設計・DesignToken仕様書 555555555555555555555555555555a1.html",
        },
        "l5_a1_2": {
            "title": "Level5_Storybookカタログ運用基準",
            "id": "555555555555555555555555555555a2",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_フロントエンド開発グループ/Level4_Webコンポーネント刷新PJ/Level5_Storybookカタログ運用基準 555555555555555555555555555555a2.html",
        },
        "l5_a2_1": {
            "title": "Level5_Federationスキーマ整合性設計",
            "id": "555555555555555555555555555555a3",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ/Level4_GraphQL_Gateway統合PJ/Level5_Federationスキーマ整合性設計 555555555555555555555555555555a3.html",
        },
        "l5_a2_2": {
            "title": "Level5_認可ポリシー・JWT伝播仕様",
            "id": "555555555555555555555555555555a4",
            "path": "Level1_総合開発ポータル/Level2_プロダクト基盤統括部/Level3_バックエンド開発グループ/Level4_GraphQL_Gateway統合PJ/Level5_認可ポリシー・JWT伝播仕様 555555555555555555555555555555a4.html",
        },
        "l5_b1_1": {
            "title": "Level5_ArgoCD同期ポリシー",
            "id": "555555555555555555555555555555b1",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ/Level4_マルチリージョンk8s移行PJ/Level5_ArgoCD同期ポリシー 555555555555555555555555555555b1.html",
        },
        "l5_b1_2": {
            "title": "Level5_Ciliumネットワークポリシー定義",
            "id": "555555555555555555555555555555b2",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_クラウドインフラ基盤グループ/Level4_マルチリージョンk8s移行PJ/Level5_Ciliumネットワークポリシー定義 555555555555555555555555555555b2.html",
        },
        "l5_b2_1": {
            "title": "Level5_分散トレーシングサンプリング規則",
            "id": "555555555555555555555555555555b3",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ/Level4_OpenTelemetry標準化PJ/Level5_分散トレーシングサンプリング規則 555555555555555555555555555555b3.html",
        },
        "l5_b2_2": {
            "title": "Level5_SLO・アラートメトリクス定義",
            "id": "555555555555555555555555555555b4",
            "path": "Level1_総合開発ポータル/Level2_プラットフォームSRE統括部/Level3_Observability監視グループ/Level4_OpenTelemetry標準化PJ/Level5_SLO・アラートメトリクス定義 555555555555555555555555555555b4.html",
        },
    }

    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key, item in nodes.items():
            content = generate_page_html(item["title"], item["id"])
            zf.writestr(item["path"], content.encode("utf-8"))
    
    print(f"✅ 5階層テスト用ZIP生成完了: {output_zip_path} (合計 {len(nodes)} ページ)")

if __name__ == "__main__":
    target_path = "test_5levels/notion_5levels_export.zip"
    create_5level_zip(target_path)

