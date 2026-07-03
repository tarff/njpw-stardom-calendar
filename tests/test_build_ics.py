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

        self.builder.load_njpw_series_ids = lambda: ["999"]
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
