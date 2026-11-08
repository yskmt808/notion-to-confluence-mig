#!/usr/bin/env python3
"""
Confluence ページの自動レンダリング＆スクリーンショット撮影・ビジュアル検証ツール。
ユーザーが手動でキャプチャを撮る必要なく、Confluence上の実際のレンダリング結果を自動撮影し、
マクロエラーやレイアウト崩れがないかを自動診断します。
"""

import os
import sys
import json
import logging
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def capture_confluence_pages(output_dir: str = "screenshots") -> int:
    result_path = "migration_result.json"
    if not os.path.exists(result_path):
        print(f"エラー: {result_path} が見つかりません。先に migrate.py を実行してください。")
        return 1

    with open(result_path, "r", encoding="utf-8") as f:
        mig_data = json.load(f)

    mapping = mig_data.get("mapping", {})
    if not mapping:
        print("検証対象ページがありません。")
        return 0

    conf_url = os.getenv("CONFLUENCE_URL").strip().rstrip("/")
    if not conf_url.endswith("/wiki"):
        conf_url = f"{conf_url}/wiki"
    conf_user = os.getenv("CONFLUENCE_USERNAME")
    conf_token = os.getenv("CONFLUENCE_API_TOKEN")
    auth = (conf_user, conf_token)

    os.makedirs(output_dir, exist_ok=True)

    print("==================================================")
    print("📸 Confluence ページの自動スクリーンショット撮影 & エラー診断")
    print(f"対象ページ数: {len(mapping)} 件")
    print(f"保存先ディレクトリ: {output_dir}/")
    print("==================================================\n")

    error_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for idx, (notion_id, item) in enumerate(mapping.items(), 1):
            conf_id = item["confluence_id"]
            title = item["title"]

            print(f"[{idx}/{len(mapping)}] 取得中: [{title}] (ID: {conf_id})")

            # Confluence APIからView HTMLを取得
            api_url = f"{conf_url}/rest/api/content/{conf_id}?expand=body.view,title,version"
            res = requests.get(api_url, auth=auth)
            if res.status_code != 200:
                print(f"  ❌ API取得エラー (HTTP {res.status_code}): {title}")
                error_count += 1
                continue

            data = res.json()
            view_html = data.get("body", {}).get("view", {}).get("value", "")

            # 添付ファイル一覧を取得して、HTML内の画像（data-linked-resource-default-alias 等）をBase64埋め込み化
            import re, base64
            try:
                att_res = requests.get(f"{conf_url}/rest/api/content/{conf_id}/child/attachment", auth=auth)
                if att_res.status_code == 200:
                    for att in att_res.json().get("results", []):
                        fname = att.get("title", "")
                        dl_path = att.get("_links", {}).get("download", "")
                        if dl_path and any(fname.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp3", ".wav")):
                            dl_url = f"{conf_url}{dl_path}" if not dl_path.startswith("http") else dl_path
                            dl_resp = requests.get(dl_url, auth=auth, headers={"X-Atlassian-Token": "nocheck"}, timeout=15)
                            if dl_resp.status_code == 200:
                                mime = dl_resp.headers.get("Content-Type", "image/png")
                                b64_str = base64.b64encode(dl_resp.content).decode("utf-8")
                                b64_uri = f"data:{mime};base64,{b64_str}"
                                # src属性を置き換え
                                escaped_fname = re.escape(fname)
                                view_html = re.sub(
                                    rf'src="[^"]*?{escaped_fname}[^"]*?"',
                                    f'src="{b64_uri}"',
                                    view_html,
                                )
                                # srcset属性があるとブラウザがそちらを優先してしまうため除去
                                view_html = re.sub(r'srcset="[^"]*?"', '', view_html)
            except Exception as e:
                logger.warning(f"Error resolving attachments for screenshot: {e}")

            # エラーキーワードの自動診断
            has_error = False
            error_reasons = []
            if "拡張機能の読み込み中にエラーが発生しました" in view_html or "error-macro" in view_html:
                has_error = True
                error_reasons.append("マクロ/拡張機能エラー検出")
            if "不明なマクロ" in view_html or "Unknown macro" in view_html:
                has_error = True
                error_reasons.append("未定義マクロ検出")

            # カバー画像（バナー）があればタイトルの上部に配置し、Notion本来のカバー画像レイアウトを再現
            cover_banner_html = ""
            main_content_html = view_html
            cover_match = re.search(r'(<div style="[^"]*?max-height:[^"]*?">.*?</div>)', view_html, re.DOTALL)
            if cover_match:
                cover_banner_html = cover_match.group(1)
                main_content_html = view_html.replace(cover_banner_html, "", 1)

            # Confluence (AUI) スタイル付きの完全なHTMLを構築
            safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
            html_filename = f"{output_dir}/page_{conf_id}_{safe_title}.html"
            png_filename = f"{output_dir}/page_{conf_id}_{safe_title}.png"

            full_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/aui/9.5.1/aui/aui-prototyping.min.css" />
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      padding: 32px 48px;
      max-width: 960px;
      margin: 0 auto;
      color: #172B4D;
      line-height: 1.6;
      background: #FFFFFF;
    }}
    h1 {{ font-size: 28px; font-weight: 600; border-bottom: 2px solid #EBECF0; padding-bottom: 8px; margin-top: 16px; }}
    h2 {{ font-size: 22px; font-weight: 600; margin-top: 24px; }}
    h3 {{ font-size: 18px; font-weight: 600; margin-top: 16px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
    th, td {{ border: 1px solid #DFE1E6; padding: 8px 12px; text-align: left; font-size: 14px; }}
    th {{ background: #F4F5F7; font-weight: 600; }}
    pre {{ background: #F4F5F7; padding: 12px; border-radius: 4px; font-family: monospace; font-size: 13px; }}
    .expand-container {{ border: 1px solid #DFE1E6; border-radius: 4px; padding: 12px; margin: 12px 0; background: #FAFBFC; }}
    .confluence-information-macro {{ padding: 12px 16px; border-radius: 4px; margin: 12px 0; border-left: 4px solid #0052CC; background: #DEEBFF; }}
    .confluence-information-macro-information {{ border-left-color: #0052CC !important; background: #DEEBFF !important; }}
    .confluence-information-macro-warning {{ border-left-color: #FF5630 !important; background: #FFEBE6 !important; }}
    .confluence-information-macro-note {{ border-left-color: #FFAB00 !important; background: #FFFAE6 !important; }}
    .confluence-information-macro-tip {{ border-left-color: #36B37E !important; background: #E3FCEF !important; }}
    .wiki-content img {{ max-width: 100%; height: auto; }}
    div[style*="max-height"], div[style*="overflow: hidden"] {{
      width: 100% !important;
      height: 180px !important;
      max-height: 180px !important;
      overflow: hidden !important;
      border-radius: 6px !important;
      margin-bottom: 20px !important;
    }}
    div[style*="max-height"] p, div[style*="max-height"] span, div[style*="max-height"] img,
    div[style*="overflow: hidden"] p, div[style*="overflow: hidden"] span, div[style*="overflow: hidden"] img {{
      width: 100% !important;
      height: 100% !important;
      display: block !important;
      object-fit: cover !important;
      object-position: center 50% !important;
      margin: 0 !important;
    }}
    audio {{ width: 100%; max-width: 500px; margin: 8px 0; }}
    .confluence-embedded-file-wrapper {{ display: inline-block; margin: 6px 0; }}
  </style>
</head>
<body>
  {cover_banner_html}
  <h1>{title}</h1>
  <div class="wiki-content">
    {main_content_html}
  </div>
</body>
</html>"""

            with open(html_filename, "w", encoding="utf-8") as hf:
                hf.write(full_html)

            # Playwrightでレンダリング＆撮影
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.goto(f"file://{os.path.abspath(html_filename)}")
            page.screenshot(path=png_filename, full_page=True)
            page.close()

            status_mark = "⚠️ 警告あり" if has_error else "✅ 正常"
            print(f"  {status_mark} -> 保存: {png_filename}")
            if has_error:
                print(f"     理由: {', '.join(error_reasons)}")
                error_count += 1

        browser.close()

    print("\n==================================================")
    print(f"📸 撮影完了: {len(mapping)} ページ中 {len(mapping) - error_count} 正常, {error_count} 件に警告あり")
    print(f"スクリーンショット確認パス: {output_dir}/")
    print("==================================================")
    return error_count


if __name__ == "__main__":
    sys.exit(capture_confluence_pages())
