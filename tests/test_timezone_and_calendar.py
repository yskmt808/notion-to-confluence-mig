import unittest
import datetime
from src.timezone_util import (
    get_local_timezone,
    get_local_timezone_name,
    resolve_timezone,
    format_iso_datetime,
)
from src.converter import NotionToConfluenceConverter
from src.worksheet import WorksheetManager
from src.extractor import PageNode


class TestTimezoneAndCalendar(unittest.TestCase):
    def test_local_timezone_detection(self):
        tz = get_local_timezone()
        self.assertIsNotNone(tz)
        name = get_local_timezone_name()
        self.assertTrue(len(name) > 0)

    def test_resolve_timezone(self):
        tz_jst = resolve_timezone("JST")
        self.assertEqual(tz_jst.tzname(None), "JST")

        tz_utc = resolve_timezone("UTC")
        self.assertEqual(tz_utc.utcoffset(None), datetime.timedelta(0))

        # None resolves to local
        tz_none = resolve_timezone(None)
        self.assertIsNotNone(tz_none)

    def test_format_iso_datetime(self):
        # 2026-11-07 23:30:00 UTC == 2026-11-08 08:30:00 JST
        iso_str = "2026-11-07T23:30:00.000Z"
        formatted_jst = format_iso_datetime(iso_str, tz="JST")
        self.assertIn("2026-11-08 08:30", formatted_jst)
        self.assertIn("JST", formatted_jst)

        formatted_utc = format_iso_datetime(iso_str, tz="UTC")
        self.assertIn("2026-11-07 23:30", formatted_utc)
        self.assertIn("UTC", formatted_utc)

    def test_converter_date_property_with_time_and_range(self):
        conv = NotionToConfluenceConverter(timezone="JST")
        prop_range = {
            "type": "date",
            "start": "2026-11-07T23:30:00.000Z",
            "end": "2026-11-08T03:00:00.000Z",
        }
        rendered = conv.format_property_for_confluence(prop_range)
        # Should render start and end with lozenge and JST time
        self.assertIn('<time datetime="2026-11-07" />', rendered)
        self.assertIn("08:30 JST", rendered)
        self.assertIn('〜 <time datetime="2026-11-08" />', rendered)
        self.assertIn("12:00 JST", rendered)

    def test_worksheet_calendar_view_mapping(self):
        db_node = PageNode(
            id="test-db-1",
            title="スケジュール管理DB",
            node_type="database",
            properties={
                "schema": {"日時": {"type": "date"}},
                "views_config": [
                    {"name": "月別カレンダー", "type": "calendar"},
                    {"name": "タイムライン計画", "type": "timeline"},
                    {"name": "進行中ボード", "type": "board"},
                ],
            },
        )
        views = WorksheetManager._extract_db_views(db_node)
        self.assertEqual(len(views), 3)

        v_cal = next(v for v in views if v["view_name"] == "月別カレンダー")
        self.assertEqual(v_cal["inferred_layout"], "table")
        self.assertIn("カレンダー", v_cal["notes"])

        v_tl = next(v for v in views if v["view_name"] == "タイムライン計画")
        self.assertEqual(v_tl["inferred_layout"], "table")
        self.assertIn("timeline", v_tl["notes"])

        v_board = next(v for v in views if v["view_name"] == "進行中ボード")
        self.assertEqual(v_board["inferred_layout"], "board")


if __name__ == "__main__":
    unittest.main()
