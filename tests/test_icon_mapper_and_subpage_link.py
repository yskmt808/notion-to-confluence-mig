import unittest
import zipfile
import io
from src.icon_mapper import resolve_notion_icon_to_emoji, extract_icon_name_from_path, is_unicode_emoji
from src.html_extractor import HtmlZipExtractor
from src.importer import PageNode


class TestIconMapperAndSubpageLink(unittest.TestCase):
    def test_extract_icon_name(self):
        self.assertEqual(extract_icon_name_from_path("/icons/aquarius_gray.svg"), "aquarius")
        self.assertEqual(extract_icon_name_from_path("https://app.notion.com/icons/airplane_purple.svg"), "airplane")
        self.assertEqual(extract_icon_name_from_path("clover_green.svg"), "clover")

    def test_resolve_icon_from_various_inputs(self):
        # 1. 直接Unicode絵文字
        self.assertEqual(resolve_notion_icon_to_emoji("🚀"), "🚀")
        # 2. SVGパス文字列
        self.assertEqual(resolve_notion_icon_to_emoji("/icons/aquarius_gray.svg"), "♒")
        self.assertEqual(resolve_notion_icon_to_emoji("https://app.notion.com/icons/airplane_purple.svg"), "✈️")
        self.assertEqual(resolve_notion_icon_to_emoji("/icons/clover_gray.svg"), "🍀")
        self.assertEqual(resolve_notion_icon_to_emoji("/icons/alien-pixel_red.svg"), "👾")
        self.assertEqual(resolve_notion_icon_to_emoji("/icons/ambulance_blue.svg"), "🚑")
        # 3. Notion API辞書
        self.assertEqual(resolve_notion_icon_to_emoji({"type": "emoji", "emoji": "🎉"}), "🎉")
        self.assertEqual(
            resolve_notion_icon_to_emoji({"type": "icon", "icon": {"name": "aquarius", "color": "gray"}}),
            "♒",
        )
        self.assertEqual(
            resolve_notion_icon_to_emoji({"type": "icon", "icon": {"name": "clover", "color": "gray"}}),
            "🍀",
        )
        # 4. 未知のアイコンや不正な文字列
        self.assertEqual(resolve_notion_icon_to_emoji("/icons/unknown_icon_gray.svg"), "📄")
        self.assertIsNone(resolve_notion_icon_to_emoji("not an emoji or icon"))
        self.assertIsNone(resolve_notion_icon_to_emoji(None))

    def test_html_extractor_resolves_page_icon_and_subpage_link(self):
        """NotionエクスポートHTMLでアイコン付き子ページリンクが画像ブロックに化けず、child_pageとして抽出されること"""
        html_content = """
        <html>
        <head>
          <meta name="data-notion-page-icon" content="/icons/aquarius_gray.svg"/>
          <title>Parent Page</title>
        </head>
        <body>
          <article id="parent-id">
            <header><h1 class="page-title">Parent Page</h1></header>
            <div class="page-body">
              <aside class="callout" data-notion-callout-icon="/icons/clover_gray.svg">
                <div style="font-size:1.5em"><img class="icon" src="/icons/clover_gray.svg"/></div>
                <div><p>コールアウト１</p></div>
              </aside>
              <figure class="link-to-page" id="subpage-id">
                <a data-notion-page-id="subpage-id" href="Subpage%20subpage-id.html">
                  <img class="icon notion-static-icon" src="https://app.notion.com/icons/airplane_purple.svg"/>
                  子ページタイトル
                </a>
              </figure>
            </div>
          </article>
        </body>
        </html>
        """
        subpage_html = """
        <html>
        <head>
          <meta name="data-notion-page-icon" content="/icons/airplane_purple.svg"/>
          <title>子ページタイトル</title>
        </head>
        <body>
          <article id="subpage-id"><header><h1 class="page-title">子ページタイトル</h1></header><div class="page-body"></div></article>
        </body>
        </html>
        """
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".zip") as tf:
            with zipfile.ZipFile(tf.name, "w") as zf:
                zf.writestr("Parent Page parent-id.html", html_content)
                zf.writestr("Parent Page/Subpage subpage-id.html", subpage_html)

            extractor = HtmlZipExtractor(tf.name)
            with zipfile.ZipFile(tf.name, "r") as zf:
                root = extractor._build_page_node(zf, "Parent Page parent-id.html", {})

            # 1. ページアイコンが絵文字に解決されていること
            self.assertEqual(root.icon, "♒")

            # 2. コールアウトのアイコンが絵文字に解決され、本文に余計なパスが混ざらないこと
            callout_block = root.blocks[0]
            self.assertEqual(callout_block["type"], "callout")
            self.assertEqual(callout_block["callout"]["icon"]["emoji"], "🍀")
            rich_texts = callout_block["callout"]["rich_text"]
            plain_texts = [r.get("plain_text", "") for r in rich_texts]
            self.assertFalse(any("/icons/" in t for t in plain_texts))

            # 3. 子ページリンクが画像ブロックにならず、child_pageとして抽出されていること
            link_block = root.blocks[1]
            self.assertEqual(link_block["type"], "child_page")
            self.assertEqual(link_block["child_page"]["title"], "子ページタイトル")


if __name__ == "__main__":
    unittest.main()
