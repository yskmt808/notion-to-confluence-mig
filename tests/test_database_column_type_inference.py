"""
tests/test_database_column_type_inference.py
TCK_00013: DBテーブルのカラム型判別適正化（過剰tag化是正・日付型判定・Confluence checkbox型対応）の網羅テスト。
"""

import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from src.api_enricher import NotionApiEnricher
from src.converter import NotionToConfluenceConverter
from src.extractor import PageNode
from src.html_extractor import HtmlZipExtractor
from src.verifier import MigrationVerifier
try:
    from storage_xml_helper import assert_valid_storage_xml
except ImportError:
    from tests.storage_xml_helper import assert_valid_storage_xml


def _make_zip(entries):
    tf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    path = tf.name
    tf.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, data)
    return path


class TestDatabaseColumnTypeInference(unittest.TestCase):
    def test_html_plus_csv_prioritizes_all_html_property_types_and_schema(self):
        """個別行HTMLが存在する場合、全property-row-*型がCSVフォールバックより優先され、HTML未保持行・schemaにも反映されること"""
        csv_content = (
            "\ufeff名前,ステータス,選択,タグ,期日,作成日時,最終更新日時,完了,金額,参照URL,メモ,関連\n"
            'Row 1,進行中,OptionA,"Core, API",2026年11月10日 → 2026年11月15日,2026年11月11日 21:30,2026年11月12日 08:45,Yes,"1,250",https://www.notion.so/11111111111111111111111111111111,"1行目\n2行目","TargetA (Sub/TargetA 22222222222222222222222222222222.html), TargetB (Sub/TargetB 33333333333333333333333333333333.html)"\n'
            'Row 2,完了,OptionB,UI,2026年12月01日,2026年12月01日 10:00,2026年12月02日 11:00,No,500,https://example.com/docs,通常テキスト,\n'
        )
        db_html = "<html><head><title>Project DB</title></head><body><h1>Project DB</h1></body></html>"
        row1_html = """
        <html>
        <head><title>Row 1</title></head>
        <body>
          <header>
            <h1 class="page-title">Row 1</h1>
            <table class="properties"><tbody>
              <tr class="property-row property-row-status"><th>ステータス</th><td><span class="selected-value">進行中</span></td></tr>
              <tr class="property-row property-row-select"><th>選択</th><td><span class="selected-value">OptionA</span></td></tr>
              <tr class="property-row property-row-multi_select"><th>タグ</th><td><span class="selected-value">Core</span><span class="selected-value">API</span></td></tr>
              <tr class="property-row property-row-date"><th>期日</th><td><time datetime="2026-11-10">2026年11月10日</time> → <time datetime="2026-11-15">2026年11月15日</time></td></tr>
              <tr class="property-row property-row-created_time"><th>作成日時</th><td><time datetime="2026-11-11T21:30">2026年11月11日 21:30</time></td></tr>
              <tr class="property-row property-row-last_edited_time"><th>最終更新日時</th><td><time datetime="2026-11-12T08:45">2026年11月12日 08:45</time></td></tr>
              <tr class="property-row property-row-checkbox"><th>完了</th><td><input type="checkbox" class="checkbox checkbox-on" disabled="" checked=""/></td></tr>
              <tr class="property-row property-row-number"><th>金額</th><td>1,250</td></tr>
              <tr class="property-row property-row-url"><th>参照URL</th><td><a href="https://www.notion.so/11111111111111111111111111111111">Notion Link</a></td></tr>
              <tr class="property-row property-row-text"><th>メモ</th><td>1行目<br/>2行目</td></tr>
              <tr class="property-row property-row-relation"><th>関連</th><td><a href="Sub/TargetA 22222222222222222222222222222222.html">TargetA</a></td></tr>
            </tbody></table>
          </header>
          <div class="page-body"><p>Body</p></div>
        </body>
        </html>
        """
        zip_path = _make_zip({
            "Project DB 00000000000000000000000000000001.csv": csv_content,
            "Project DB 00000000000000000000000000000001.html": db_html,
            "Project DB/Row 1 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.html": row1_html,
        })
        try:
            extractor = HtmlZipExtractor(zip_path)
            db_node = extractor.extract_tree()
            self.assertEqual(db_node.node_type, "database")
            self.assertEqual(db_node.properties.get("title_column"), "名前")

            schema = db_node.properties.get("schema", {})
            expected_schema_types = {
                "名前": "title",
                "ステータス": "status",
                "選択": "select",
                "タグ": "multi_select",
                "期日": "date",
                "作成日時": "created_time",
                "最終更新日時": "last_edited_time",
                "完了": "checkbox",
                "金額": "number",
                "参照URL": "url",
                "メモ": "text",
                "関連": "relation",
            }
            for col, exp_t in expected_schema_types.items():
                self.assertEqual(schema[col]["type"], exp_t, f"schema mismatch for {col}")

            row1, row2 = db_node.children[0], db_node.children[1]
            self.assertEqual(row1.properties["ステータス"]["type"], "status")
            self.assertEqual(row1.properties["選択"]["type"], "select")
            self.assertEqual(row1.properties["タグ"]["type"], "multi_select")
            self.assertEqual(row1.properties["期日"]["type"], "date")
            self.assertEqual(row1.properties["期日"]["start"], "2026-11-10")
            self.assertEqual(row1.properties["期日"]["end"], "2026-11-15")
            self.assertEqual(row1.properties["作成日時"]["type"], "created_time")
            self.assertEqual(row1.properties["作成日時"]["start"], "2026-11-11T21:30")
            self.assertEqual(row1.properties["最終更新日時"]["type"], "last_edited_time")
            self.assertEqual(row1.properties["最終更新日時"]["start"], "2026-11-12T08:45")
            self.assertEqual(row1.properties["完了"]["type"], "checkbox")
            self.assertTrue(row1.properties["完了"]["value"])
            self.assertEqual(row1.properties["金額"]["type"], "number")
            self.assertEqual(row1.properties["金額"]["value"], 1250)
            # URL列内の notion.so リンクが relation に誤認されないこと
            self.assertEqual(row1.properties["参照URL"]["type"], "url")
            self.assertEqual(row1.properties["参照URL"]["value"], "https://www.notion.so/11111111111111111111111111111111")
            # HTML の <br/> が改行テキストとして保持されること
            self.assertEqual(row1.properties["メモ"]["type"], "text")
            self.assertEqual(row1.properties["メモ"]["value"], "1行目\n2行目")
            # CSV 側の複数リレーション補完が維持されること (2件)
            self.assertEqual(row1.properties["関連"]["type"], "relation")
            self.assertEqual(len(row1.properties["関連"]["relations"]), 2)

            # HTMLが無い Row 2 も HTML 確定列型を引き継ぐこと
            self.assertEqual(row2.properties["ステータス"]["type"], "status")
            self.assertEqual(row2.properties["選択"]["type"], "select")
            self.assertEqual(row2.properties["完了"]["type"], "checkbox")
            self.assertFalse(row2.properties["完了"]["value"])
            self.assertEqual(row2.properties["作成日時"]["type"], "created_time")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    def test_standalone_csv_column_wide_inference_and_no_over_tagging(self):
        """行HTMLが存在しない単独CSVで、1行テキストがselect/tag化されずtextになり、日付/数値/URL/チェックボックス/混在列が安全に判別されること"""
        csv_content = (
            "\ufeff名前,一行メモ,複数行メモ,期日,数値,コード番号,電話番号,リンクURL,混在URL,完了フラグ,チェック記号,Tags,混在数値\n"
            'Item 1,短いメモA,"1行目\n2行目",2026年11月10日,"1,000",00123,03-1234-5678,https://example.com/1,https://example.com/a,Yes,✅,"Core, API",100\n'
            'Item 2,短いメモB,単一行のみ,2026-11-15 → 2026-11-20,-25.5,00456,090-1111-2222,http://localhost:8080/x,社内Wiki参照,No,⬜,UI,未定\n'
            'Item 3,短いメモC,,"November 1, 2026 9:30 PM",0,01000,,https://sub.example.co.jp/p?a=1,,true,☑,,200\n'
        )
        parent_html = "<html><head><title>Root</title></head><body><div class='page-body'></div></body></html>"
        zip_path = _make_zip({
            "Root 00000000000000000000000000000001.html": parent_html,
            "Root/StandaloneDB 00000000000000000000000000000002.csv": csv_content,
        })
        try:
            extractor = HtmlZipExtractor(zip_path)
            root = extractor.extract_tree()
            db_node = root.children[0]
            self.assertEqual(db_node.node_type, "database")
            schema = db_node.properties["schema"]

            # 過剰な select/tag 化が防止され text になること
            self.assertEqual(schema["一行メモ"]["type"], "text")
            self.assertEqual(schema["複数行メモ"]["type"], "text")
            # 先行ゼロのコード番号・電話番号・混在列は text にフォールバックすること
            self.assertEqual(schema["コード番号"]["type"], "text")
            self.assertEqual(schema["電話番号"]["type"], "text")
            self.assertEqual(schema["混在URL"]["type"], "text")
            self.assertEqual(schema["混在数値"]["type"], "text")

            # 一貫して型適合する列のみそれぞれの型になること
            self.assertEqual(schema["期日"]["type"], "date")
            self.assertEqual(schema["数値"]["type"], "number")
            self.assertEqual(schema["リンクURL"]["type"], "url")
            self.assertEqual(schema["完了フラグ"]["type"], "checkbox")
            self.assertEqual(schema["チェック記号"]["type"], "checkbox")
            self.assertEqual(schema["Tags"]["type"], "multi_select")

            # 値の検証
            r1, r2, r3 = db_node.children
            self.assertEqual(r1.properties["一行メモ"]["type"], "text")
            self.assertEqual(r1.properties["数値"]["value"], 1000)
            self.assertEqual(r2.properties["数値"]["value"], -25.5)
            self.assertEqual(r3.properties["数値"]["value"], 0)
            self.assertEqual(r2.properties["期日"]["start"], "2026-11-15")
            self.assertEqual(r2.properties["期日"]["end"], "2026-11-20")
            self.assertEqual(r3.properties["期日"]["start"], "2026-11-01T21:30")
            self.assertTrue(r1.properties["チェック記号"]["value"])
            self.assertFalse(r2.properties["チェック記号"]["value"])
            self.assertTrue(r3.properties["チェック記号"]["value"])
            self.assertEqual(r1.properties["コード番号"]["type"], "text")
            self.assertEqual(r1.properties["コード番号"]["value"], "00123")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    def test_converter_csv_checkbox_date_timestamps_and_select_config(self):
        """generate_database_csv / _format_prop_for_csv / get_select_field_configurations / format_property_for_confluence の型マッピング検証"""
        conv = NotionToConfluenceConverter(timezone="JST")
        db_node = PageNode(
            id="db-conv",
            title="型マッピングDB",
            node_type="database",
            properties={
                "title_column": "名前",
                "schema": {
                    "名前": {"type": "title"},
                    "完了": {"type": "checkbox"},
                    "期日": {"type": "date"},
                    "作成日時": {"type": "created_time"},
                    "更新日時": {"type": "last_edited_time"},
                    "ステータス": {"type": "status"},
                    "選択": {"type": "select"},
                    "タグ": {"type": "multi_select"},
                    "数値": {"type": "number"},
                    "URL": {"type": "url"},
                    "メモ": {"type": "text"},
                },
            },
            children=[
                PageNode(
                    id="r1",
                    title="Row 1",
                    node_type="database_row",
                    properties={
                        "名前": {"type": "title", "value": "Row 1"},
                        "完了": {"type": "checkbox", "value": True},
                        "期日": {"type": "date", "start": "2026-11-10"},
                        # UTC 23:30 -> JST 翌 08:30 (日付繰り上がり)
                        "作成日時": {"type": "created_time", "value": "2026-11-07T23:30:00.000Z"},
                        # HTML由来のnaiveローカル時刻 (シフトしない)
                        "更新日時": {"type": "last_edited_time", "start": "2026-11-11T21:30", "value": "2026-11-11T21:30"},
                        "ステータス": {"type": "status", "status": {"name": "進行中"}, "value": "進行中"},
                        "選択": {"type": "select", "select": {"name": "AAA"}, "value": "AAA"},
                        "タグ": {"type": "multi_select", "items": [{"name": "A"}, {"name": "B"}]},
                        "数値": {"type": "number", "value": "1,234.5"},
                        "URL": {"type": "url", "value": "https://example.com"},
                        "メモ": {"type": "text", "value": "通常テキスト"},
                    },
                ),
                PageNode(
                    id="r2",
                    title="Row 2",
                    node_type="database_row",
                    properties={
                        "名前": {"type": "title", "value": "Row 2"},
                        "完了": {"type": "checkbox", "value": False},
                    },
                ),
                PageNode(
                    id="r3",
                    title="Row 3",
                    node_type="database_row",
                    properties={
                        "名前": {"type": "title", "value": "Row 3"},
                        "完了": {"type": "checkbox", "value": None},
                    },
                ),
            ],
        )

        # 1. get_select_field_configurations から checkbox が除外されていること
        single_sel, multi_sel = conv.get_select_field_configurations(db_node)
        self.assertEqual(single_sel, ["ステータス", "選択"])
        self.assertEqual(multi_sel, ["タグ"])
        self.assertNotIn("完了", single_sel)

        # 2. generate_database_csv の <fields> と <data>
        csv_out = conv.generate_database_csv(
            db_node,
            {
                "r1": "https://example.atlassian.net/wiki/spaces/S/pages/1",
                "r2": "https://example.atlassian.net/wiki/spaces/S/pages/2",
                "r3": "https://example.atlassian.net/wiki/spaces/S/pages/3",
            },
        )
        self.assertIn("名前,confluence-page-link,", csv_out)
        self.assertIn("完了,checkbox,", csv_out)
        self.assertIn("期日,date,", csv_out)
        self.assertIn("作成日時,date,", csv_out)
        self.assertIn("更新日時,date,", csv_out)
        self.assertIn("ステータス,tag,", csv_out)
        self.assertIn("選択,tag,", csv_out)
        self.assertIn("タグ,tag,", csv_out)
        self.assertIn("数値,number,number_format:number", csv_out)
        self.assertIn("URL,hyperlink,", csv_out)
        self.assertIn("メモ,text,", csv_out)

        lines = csv_out.splitlines()
        data_idx = lines.index("<data>")
        row1_csv = lines[data_idx + 2]
        row2_csv = lines[data_idx + 3]
        row3_csv = lines[data_idx + 4]

        self.assertEqual(
            row1_csv,
            "https://example.atlassian.net/wiki/spaces/S/pages/1,true,2026-11-10,2026-11-08,2026-11-11,進行中,AAA,A|B,1234.5,https://example.com,通常テキスト",
        )
        self.assertIn(",false,", row2_csv)
        self.assertNotIn("完了", row1_csv.split(",")[1])
        self.assertNotIn("未完了", row2_csv)
        self.assertTrue(row3_csv.startswith('https://example.atlassian.net/wiki/spaces/S/pages/3,"",'))

        # 3. format_property_for_confluence の created_time / last_edited_time 表示
        ct_html = conv.format_property_for_confluence({"type": "created_time", "value": "2026-11-07T23:30:00.000Z"})
        self.assertIn('<time datetime="2026-11-08" />', ct_html)
        self.assertIn("08:30 JST", ct_html)
        assert_valid_storage_xml(self, f"<p>{ct_html}</p>")

        lt_html = conv.format_property_for_confluence({"type": "last_edited_time", "start": "2026-11-11T21:30"})
        self.assertIn('<time datetime="2026-11-11" />', lt_html)
        self.assertIn("21:30", lt_html)
        assert_valid_storage_xml(self, f"<p>{lt_html}</p>")

    def test_api_enricher_preserves_created_time_last_edited_time_and_date_end(self):
        """NotionApiEnricher._align_row_properties が created_time / last_edited_time / date範囲 / url を欠落させず整列すること"""
        enricher = NotionApiEnricher(token="fake")
        schema = {
            "名前": {"type": "title"},
            "作成日時": {"type": "created_time"},
            "最終更新日時": {"type": "last_edited_time"},
            "期日": {"type": "date"},
            "URL": {"type": "url"},
            "ステータス": {"type": "status"},
            "完了": {"type": "checkbox"},
        }
        row1 = PageNode(
            id="r1",
            title="Row 1",
            node_type="database_row",
            created_time="2026-11-01T10:00:00.000Z",
            last_edited_time="2026-11-02T12:00:00.000Z",
            properties={
                "作成日時": {"type": "created_time", "start": "2026-11-01T10:00", "value": "2026-11-01T10:00"},
                # 最終更新日時は row.properties に無くても row.last_edited_time から補完されること
                "期日": {"type": "date", "start": "2026-11-10", "end": "2026-11-15", "value": "2026-11-10"},
                "URL": {"type": "url", "url": "https://example.com", "value": "https://example.com"},
                "ステータス": {"type": "status", "value": "進行中"},
                "完了": {"type": "text", "value": "✅"},
            },
        )
        enricher._align_row_properties([row1], schema)

        self.assertEqual(row1.properties["作成日時"]["type"], "created_time")
        self.assertEqual(row1.properties["作成日時"]["value"], "2026-11-01T10:00")
        self.assertEqual(row1.properties["最終更新日時"]["type"], "last_edited_time")
        self.assertEqual(row1.properties["最終更新日時"]["value"], "2026-11-02T12:00:00.000Z")
        self.assertEqual(row1.properties["期日"]["start"], "2026-11-10")
        self.assertEqual(row1.properties["期日"]["end"], "2026-11-15")
        self.assertEqual(row1.properties["URL"]["type"], "url")
        self.assertEqual(row1.properties["URL"]["value"], "https://example.com")
        self.assertEqual(row1.properties["ステータス"]["type"], "status")
        self.assertTrue(row1.properties["完了"]["value"])

    def test_verifier_validates_database_field_types(self):
        """MigrationVerifier.verify_database_node が Confluence Databases /fields API の型整合性を検証すること"""
        mock_client = MagicMock()
        mock_client.base_url = "https://example.atlassian.net/wiki"
        mock_client.auth = ("u", "t")
        mock_client.get_database_views.return_value = [{"id": "v1", "name": "すべてのデータ", "type": "table"}]
        mock_client.get_database_entries.return_value = [{"id": "e1"}]

        db_node_ok = PageNode(
            id="db-1",
            title="Fields Verify DB",
            node_type="database",
            properties={
                "schema": {
                    "名前": {"type": "title"},
                    "完了": {"type": "checkbox"},
                    "作成日時": {"type": "created_time"},
                    "メモ": {"type": "text"},
                }
            },
            children=[PageNode(id="r1", title="Row 1", node_type="database_row")],
        )

        mock_db_res = MagicMock(ok=True)
        mock_db_res.json.return_value = {"id": "conf-db-1", "title": "Fields Verify DB", "parentId": "p-1"}

        # 1. 型一致時は PASS（Confluence API v2 実測値 pageLink も含む）
        mock_client.get_database_fields.return_value = [
            {"name": "名前", "type": "pageLink"},
            {"name": "完了", "type": "checkbox"},
            {"name": "作成日時", "type": "date"},
            {"name": "メモ", "type": "text"},
        ]
        verifier = MigrationVerifier(mock_client)
        with patch("requests.get", return_value=mock_db_res):
            res_ok = verifier.verify_database_node(db_node_ok, "conf-db-1", expected_parent_id="p-1")
        self.assertEqual(res_ok["status"], "PASS")
        self.assertTrue(any("Database field types verified" in c for c in res_ok["checks_passed"]))

        # 2. checkbox が tag になっているなど型不一致時は FAIL
        db_node_bad = PageNode(
            id="db-2",
            title="Fields Verify DB",
            node_type="database",
            properties={"schema": {"完了": {"type": "checkbox"}}},
            children=[PageNode(id="r1", title="Row 1", node_type="database_row")],
        )
        mock_client.get_database_fields.return_value = [{"name": "完了", "type": "tag"}]
        with patch("requests.get", return_value=mock_db_res):
            res_bad = verifier.verify_database_node(db_node_bad, "conf-db-1", expected_parent_id="p-1")
        self.assertEqual(res_bad["status"], "FAIL")
        self.assertTrue(any("type mismatch" in iss for iss in res_bad["issues"]))

    def test_error_pattern_export_block_zip_schema_and_csv(self):
        """error_pattern_ExportBlock.zip 内のDBが過剰tag化せず checkbox/created_time/date/select/title を正しく判別すること"""
        zip_path = "/home/antigravity/yskmt808/notion-to-confluence-mig/error_pattern_ExportBlock.zip"
        if not os.path.exists(zip_path):
            self.skipTest(f"Sample ZIP not found: {zip_path}")

        root = HtmlZipExtractor(zip_path).extract_tree()
        target_db = None

        def _find_db(node):
            nonlocal target_db
            if node.node_type == "database" and node.id == "4157be77-284d-8021-b09e-f9e9a1a7475d":
                target_db = node
                return
            for ch in node.children:
                _find_db(ch)

        _find_db(root)
        self.assertIsNotNone(target_db)
        schema = target_db.properties["schema"]
        self.assertEqual(schema["名前"]["type"], "title")
        self.assertEqual(schema["チェックボックス"]["type"], "checkbox")
        self.assertEqual(schema["作成日時"]["type"], "created_time")
        self.assertEqual(schema["日付"]["type"], "date")
        self.assertEqual(schema["選択"]["type"], "select")

        conv = NotionToConfluenceConverter(timezone="Asia/Tokyo")
        single_sel, multi_sel = conv.get_select_field_configurations(target_db)
        self.assertEqual(single_sel, ["選択"])
        self.assertEqual(multi_sel, [])

        csv_str = conv.generate_database_csv(target_db, {})
        self.assertIn("名前,confluence-page-link,", csv_str)
        self.assertIn("チェックボックス,checkbox,", csv_str)
        self.assertIn("作成日時,date,", csv_str)
        self.assertIn("日付,date,", csv_str)
        self.assertIn("選択,tag,", csv_str)
        self.assertIn(",true,2026-11-11,2026-11-10,AAA", csv_str)
        self.assertIn(",false,2026-11-11,2026-12-09,BBB", csv_str)
        self.assertIn(',false,2026-11-11,2026-11-30,""', csv_str)


if __name__ == "__main__":
    unittest.main()
