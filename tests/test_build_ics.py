import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_ics", Path(__file__).resolve().parents[1] / "scripts" / "build_ics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_map(mapping):
    """fetch() stub: first URL substring that matches wins. Exception values are raised."""
    def _fetch(url, binary=False):
        for key, payload in mapping.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return __import__("json").dumps(payload)
        raise OSError(f"unexpected URL: {url}")
    return _fetch


class BuildIcsTests(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()

    def test_parse_bell_rejects_invalid_24_hour_times(self):
        self.assertEqual((17, 0), self.builder.parse_bell("17:00"))
        self.assertEqual((18, 30), self.builder.parse_bell("6:30 PM"))
        self.assertIsNone(self.builder.parse_bell("24:00"))
        self.assertIsNone(self.builder.parse_bell("99:99"))
        self.assertIsNone(self.builder.parse_bell("12:60"))

    def test_main_refuses_to_write_when_required_njpw_api_fetch_fails(self):
        events = [
            self.builder.Event(
                uid=f"stardom-{i}@test",
                summary="Stardom",
                location="",
                desc="",
                date=date(2026, 1, 1),
            )
            for i in range(20)
        ]

        def fail_fetch(url, binary=False):
            raise OSError("simulated API outage")

        self.builder.fetch = fail_fetch
        self.builder.stardom_events = lambda: events

        with tempfile.TemporaryDirectory() as tmp:
            self.builder.OUT = Path(tmp) / "calendar.ics"
            with self.assertRaises(SystemExit) as cm:
                self.builder.main()

            self.assertNotEqual(0, cm.exception.code)
            self.assertFalse(self.builder.OUT.exists())

    def test_main_refuses_to_write_when_required_njpw_api_payload_has_no_shows(self):
        events = [
            self.builder.Event(
                uid=f"stardom-{i}@test",
                summary="Stardom",
                location="",
                desc="",
                date=date(2026, 1, 1),
            )
            for i in range(20)
        ]

        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = lambda url, binary=False: __import__("json").dumps({
            "twitter_hash_tags": "G1CLIMAX36",
            "tournaments": [],
        })
        self.builder.njpw_from_gcal = lambda covered, spans: []
        self.builder.stardom_events = lambda: events

        with tempfile.TemporaryDirectory() as tmp:
            self.builder.OUT = Path(tmp) / "calendar.ics"
            with self.assertRaises(SystemExit) as cm:
                self.builder.main()

            self.assertNotEqual(0, cm.exception.code)
            self.assertFalse(self.builder.OUT.exists())

    def test_njpw_api_uids_use_per_show_post_ids_for_same_day_events(self):
        payload = {
            "twitter_hash_tags": "TEST",
            "tournaments": [
                {
                    "post_id": 111,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "12:00",
                    "venue": {"stadium_name": "Hall A", "prefecture": "Tokyo"},
                },
                {
                    "post_id": 222,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "18:00",
                    "venue": {"stadium_name": "Hall B", "prefecture": "Tokyo"},
                },
            ],
        }

        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = lambda url, binary=False: __import__("json").dumps(payload)

        events, covered, spans, failures = self.builder.njpw_from_api()

        self.assertEqual([], failures)
        self.assertEqual(2, len(events))
        self.assertEqual(2, len({e.uid for e in events}))
        self.assertIn("njpw-111@njpw-stardom-cal", {e.uid for e in events})
        self.assertIn("njpw-222@njpw-stardom-cal", {e.uid for e in events})

    def test_google_calendar_keeps_same_day_events_with_source_uids(self):
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260102T030000Z
UID:first@google.com
SUMMARY:Morning Show（Tokyo）
END:VEVENT
BEGIN:VEVENT
DTSTART:20260102T090000Z
UID:second@google.com
SUMMARY:Evening Show（Osaka）
END:VEVENT
END:VCALENDAR
"""

        self.builder.fetch = lambda url, binary=False: raw
        self.builder.datetime = FixedDatetime

        events = self.builder.njpw_from_gcal(set(), [])

        self.assertEqual(2, len(events))
        self.assertEqual(
            {"njpw-gcal-first@google.com", "njpw-gcal-second@google.com"},
            {e.uid for e in events},
        )

    def test_google_calendar_keeps_uncovered_dates_inside_api_tour_span(self):
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260112T090000Z
UID:one-off@google.com
SUMMARY:Special One-off（Tokyo）
END:VEVENT
END:VCALENDAR
"""

        self.builder.fetch = lambda url, binary=False: raw
        self.builder.datetime = FixedDatetime

        events = self.builder.njpw_from_gcal(
            {date(2026, 1, 10), date(2026, 1, 14)},
            [(date(2026, 1, 9), date(2026, 1, 15))],
        )

        self.assertEqual(1, len(events))
        self.assertEqual("njpw-gcal-one-off@google.com", events[0].uid)

    def test_google_calendar_keeps_adjacent_uncovered_dates(self):
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260111T090000Z
UID:legit-one-off@google.com
SUMMARY:Legitimate One-off（Tokyo）
END:VEVENT
END:VCALENDAR
"""

        self.builder.fetch = lambda url, binary=False: raw
        self.builder.datetime = FixedDatetime

        events = self.builder.njpw_from_gcal(
            {date(2026, 1, 10), date(2026, 1, 14)},
            [(date(2026, 1, 9), date(2026, 1, 15))],
        )

        self.assertEqual(1, len(events))
        self.assertEqual("njpw-gcal-legit-one-off@google.com", events[0].uid)

    def test_google_calendar_skips_adjacent_date_shift_from_api_event(self):
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260112T090000Z
UID:timezone-shift@google.com
SUMMARY:G1 CLIMAX 36（Chicago）
END:VEVENT
END:VCALENDAR
"""

        self.builder.fetch = lambda url, binary=False: raw
        self.builder.datetime = FixedDatetime

        events = self.builder.njpw_from_gcal({date(2026, 1, 11), date(2026, 1, 12)}, [])

        self.assertEqual([], events)

    def test_google_calendar_skips_covered_dates_from_api(self):
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260110T090000Z
UID:duplicate@google.com
SUMMARY:Duplicate Show（Tokyo）
END:VEVENT
END:VCALENDAR
"""

        self.builder.fetch = lambda url, binary=False: raw
        self.builder.datetime = FixedDatetime

        events = self.builder.njpw_from_gcal({date(2026, 1, 10)}, [])

        self.assertEqual([], events)

    def test_njpw_api_covers_foreign_show_jst_date_for_gcal_dedupe(self):
        payload = {
            "twitter_hash_tags": "TEST",
            "tournaments": [
                {
                    "post_id": 444,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "19:00",
                    "venue": {"stadium_name": "Madison Square Garden", "prefecture": "USA"},
                },
            ],
        }

        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = lambda url, binary=False: __import__("json").dumps(payload)

        events, covered, spans, failures = self.builder.njpw_from_api()

        self.assertEqual([], failures)
        self.assertIn(date(2026, 1, 2), covered)
        self.assertIn(date(2026, 1, 3), covered)

    def test_njpw_api_uses_known_us_venue_timezone(self):
        payload = {
            "twitter_hash_tags": "TEST",
            "tournaments": [
                {
                    "post_id": 333,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "19:00",
                    "venue": {"stadium_name": "Madison Square Garden", "prefecture": "USA"},
                },
            ],
        }

        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = lambda url, binary=False: __import__("json").dumps(payload)

        events, covered, spans, failures = self.builder.njpw_from_api()

        self.assertEqual([], failures)
        self.assertEqual("America/New_York", events[0].tz)

    def test_series_ids_come_from_the_schedule_index(self):
        self.builder.fetch = fetch_map({
            "/pagination/1.json": {"AllCount": 2, "posts": [{"post_id": "111"}, {"post_id": 222}]},
            "/pagination/2.json": {"AllCount": 2, "posts": [{"post_id": 222}, {"post_id": "333"}]},
        })

        ids, failures = self.builder.discover_njpw_series_ids()

        self.assertEqual([], failures)
        self.assertEqual(["111", "222", "333"], ids)

    def test_series_index_fetch_failure_is_fatal(self):
        self.builder.fetch = fetch_map({"/pagination/1.json": OSError("simulated outage")})

        ids, failures = self.builder.discover_njpw_series_ids()

        self.assertEqual([], ids)
        self.assertEqual(1, len(failures))

    def test_series_index_with_no_series_is_fatal(self):
        self.builder.fetch = fetch_map({"/pagination/1.json": {"AllCount": 1, "posts": []}})

        ids, failures = self.builder.discover_njpw_series_ids()

        self.assertEqual([], ids)
        self.assertEqual(1, len(failures))

    def test_series_index_without_a_page_count_is_fatal(self):
        # Trusting a missing AllCount would silently publish only page 1 of the schedule.
        self.builder.fetch = fetch_map({"/pagination/1.json": {"posts": [{"post_id": "111"}]}})

        ids, failures = self.builder.discover_njpw_series_ids()

        self.assertEqual(1, len(failures))

    def test_summary_uses_series_title_from_the_api(self):
        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = fetch_map({
            "/series/posts/999.json": {"series_title": "  SUPER Jr. TAG LEAGUE  2026  "},
            "/schedule/list/999.json": {
                "twitter_hash_tags": "sjtl",
                "tournaments": [{
                    "post_id": 555,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "18:30",
                    "venue": {"stadium_name": "Korakuen Hall", "prefecture": "Tokyo"},
                }],
            },
        })

        events, covered, spans, failures = self.builder.njpw_from_api()

        self.assertEqual([], failures)
        self.assertEqual("NJPW — SUPER Jr. TAG LEAGUE 2026 · Tokyo", events[0].summary)

    def test_missing_series_title_falls_back_without_failing_the_build(self):
        self.builder.discover_njpw_series_ids = lambda: (["999"], [])
        self.builder.fetch = fetch_map({
            "/series/posts/999.json": OSError("title unavailable"),
            "/schedule/list/999.json": {
                "twitter_hash_tags": "sjtl",
                "tournaments": [{
                    "post_id": 555,
                    "event_start_date": "2026-01-02T00:00:00+09:00",
                    "start_time": "18:30",
                    "venue": {"stadium_name": "Korakuen Hall", "prefecture": "Tokyo"},
                }],
            },
        })

        events, covered, spans, failures = self.builder.njpw_from_api()

        self.assertEqual([], failures)
        self.assertEqual("NJPW — sjtl · Tokyo", events[0].summary)

    def test_calendar_description_does_not_embed_changing_stamp(self):
        event = self.builder.Event(
            uid="event@test",
            summary="Event",
            location="",
            desc="",
            date=date(2026, 1, 1),
        )

        out = self.builder.emit([event], "20990101T000000Z")

        self.assertNotIn("Last build 20990101T000000Z", out)


class FixedDatetime:
    @classmethod
    def now(cls, tz=None):
        return __import__("datetime").datetime(2026, 1, 1, tzinfo=tz)

    @classmethod
    def strptime(cls, *args, **kwargs):
        return __import__("datetime").datetime.strptime(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
