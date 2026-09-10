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


# Real markup shape from wwr-stardom.com/en/schedule/ (captured 2026-09-08): the slug
# separator is a hyphen, and Stardom's own shows sit under /en/event/ as well as
# /en/schedule/. The pre-2026-09 parser required "/schedule/" + an underscore and so
# silently matched nothing.
STARDOM_GRID = """
<li><span class="date">5</span>
<a href="https://wwr-stardom.com/en/event/20260905-irgevent/" class="pc_only"><div class="box_event">Ito Respect Army Summit 2026</div></a>
</li>
<li><span class="date">6</span>
<a href="https://wwr-stardom.com/en/event/20260906-korakuen/" class="pc_only"><div class="box_game">STARDOM in KORAKUEN 2026 Sep.</div></a>
</li>
<li><span class="date">10</span>
<a href="https://wwr-stardom.com/en/schedule/20260910-itodojo/" class="pc_only"><div class="box_other">[Participating from another organization] Ito Dojo</div></a>
</li>
<li><span class="date">12</span>
<a href="https://wwr-stardom.com/en/schedule/20260912-yokohama/" class="pc_only"><div class="box_game">STARDOM TO THE WORLD 2026</div></a>
</li>
"""


class StardomGridTests(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()

    def test_month_grid_finds_own_shows_under_both_event_and_schedule_paths(self):
        shows = self.builder.stardom_month_shows(STARDOM_GRID)

        self.assertEqual(
            [("20260906", "event", "20260906-korakuen", "STARDOM in KORAKUEN 2026 Sep."),
             ("20260912", "schedule", "20260912-yokohama", "STARDOM TO THE WORLD 2026")],
            shows,
        )

    def test_month_grid_still_excludes_other_promotion_and_press_entries(self):
        names = [s[3] for s in self.builder.stardom_month_shows(STARDOM_GRID)]

        self.assertNotIn("Ito Respect Army Summit 2026", names)
        self.assertTrue(all("Ito Dojo" not in n for n in names))

    def test_scrape_fails_loudly_when_every_grid_parses_to_nothing(self):
        """A silent structural change (the 2026-09 URL rewrite) must not look like
        'Stardom announced no shows' -- that published a month of empty calendar."""
        moved = '<a href="/en/whatever/sep6/"><div class="box_game">STARDOM</div></a>'
        self.builder.fetch = lambda url, binary=False: moved

        with self.assertRaises(self.builder.StardomGridChanged):
            self.builder.scrape_stardom()

    def test_scrape_tolerates_a_genuinely_empty_month(self):
        def _fetch(url, binary=False):
            return STARDOM_GRID if "ym=" in url and url.endswith("09") else "<html></html>"

        self.builder.fetch = _fetch
        self.builder.STARDOM_DETAIL_LOOKAHEAD = -1  # no detail fetches in this test

        out = self.builder.scrape_stardom()

        self.assertIn("2026-09-06", out)
        self.assertEqual("Korakuen Hall, Tokyo", out["2026-09-06"]["venue"])

    def test_detail_url_uses_the_path_the_grid_linked_to(self):
        seen = []

        def _fetch(url, binary=False):
            seen.append(url)
            return STARDOM_GRID if "ym=" in url else "The start time for the main event 16:00"

        self.builder.fetch = _fetch
        self.builder.datetime = FixedStardomDatetime
        self.builder.scrape_stardom()

        self.assertIn("https://wwr-stardom.com/en/event/20260906-korakuen/", seen)
        self.assertIn("https://wwr-stardom.com/en/schedule/20260912-yokohama/", seen)


class FixedStardomDatetime:
    @classmethod
    def now(cls, tz=None):
        return __import__("datetime").datetime(2026, 9, 1, tzinfo=tz)

    @classmethod
    def strptime(cls, *args, **kwargs):
        return __import__("datetime").datetime.strptime(*args, **kwargs)


# The grid tile no longer separates show name from venue, and a slug's date prefix belongs
# to the series, not the show: SAKAE ~Day2~ is slugged 20261002 but runs on the 3rd. The
# page's INFORMATION list carries the authoritative date, a clean title and a clean venue.
STARDOM_INFO = """
<ul class="schedule_list">
<li class="info_box"><div class="info_text">
<p class="date">2026.10.02 Fri</p>
<h2 class="title">STARDOM in SAKAE 2026 Oct. ~Day1~</h2>
<p class="place">Aichi Chunichi Hall</p>
</div><a href="https://wwr-stardom.com/en/schedule/20261002-chu-nichi-day1/">Ticket details</a></li>
<li class="info_box"><div class="info_text">
<p class="date">2026.10.03 Sat</p>
<h2 class="title">STARDOM in SAKAE 2026 Oct. ~Day2~</h2>
<p class="place">Aichi Chunichi Hall</p>
</div><a href="https://wwr-stardom.com/en/schedule/20261002-chu-nichi-day2/">Ticket details</a></li>
</ul>
"""

STARDOM_TWO_DAY_GRID = """
<a href="https://wwr-stardom.com/en/schedule/20261002-chu-nichi-day1/"><div class="box_game">STARDOM in SAKAE 2026 Oct. ~Day 1~ Aichi, Chunichi Hall</div></a>
<a href="https://wwr-stardom.com/en/schedule/20261002-chu-nichi-day2/"><div class="box_game">STARDOM in SAKAE 2026 Oct. ~Day 2~ Aichi, Chunichi Hall</div></a>
""" + STARDOM_INFO


class StardomInfoBoxTests(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()
        self.builder.STARDOM_DETAIL_LOOKAHEAD = -1  # never fetch detail pages here

    def test_info_boxes_give_date_title_and_venue_keyed_by_slug(self):
        info = self.builder.stardom_info_boxes(STARDOM_INFO)

        self.assertEqual(
            ("2026-10-03", "STARDOM in SAKAE 2026 Oct. ~Day2~", "Aichi Chunichi Hall"),
            info["20261002-chu-nichi-day2"],
        )

    def test_two_shows_sharing_a_slug_date_keep_their_own_days(self):
        self.builder.fetch = lambda url, binary=False: (
            STARDOM_TWO_DAY_GRID if "ym=202610" in url else "<html></html>")
        self.builder.datetime = FixedStardomDatetime

        out = self.builder.scrape_stardom()

        self.assertEqual("STARDOM in SAKAE 2026 Oct. ~Day1~", out["2026-10-02"]["name"])
        self.assertEqual("STARDOM in SAKAE 2026 Oct. ~Day2~", out["2026-10-03"]["name"])
        self.assertEqual("Aichi Chunichi Hall", out["2026-10-03"]["venue"])

    def test_a_show_the_information_list_omits_still_falls_back_to_the_grid(self):
        grid = ('<a href="https://wwr-stardom.com/en/event/20261210-korakuen/">'
                '<div class="box_game">Korakuen Hall (Evening)</div></a>')
        self.builder.fetch = lambda url, binary=False: (
            grid if "ym=202612" in url else "<html></html>")
        self.builder.datetime = FixedStardomDatetime

        out = self.builder.scrape_stardom()

        self.assertEqual("Korakuen Hall (Evening)", out["2026-12-10"]["name"])
        self.assertEqual("Korakuen Hall, Tokyo", out["2026-12-10"]["venue"])

    def test_other_promotion_entries_in_the_information_list_are_not_added(self):
        info = ('<li class="info_box"><div class="info_text">'
                '<p class="date">2026.10.19 Mon</p>'
                '<h2 class="title">[Participation] Pro Wrestling Judo</h2>'
                '<p class="place">Shinkiba 1st RING</p></div>'
                '<a href="https://wwr-stardom.com/en/schedule/20261019-pw-judo/">t</a></li>')
        self.builder.fetch = lambda url, binary=False: (
            STARDOM_TWO_DAY_GRID + info if "ym=202610" in url else "<html></html>")
        self.builder.datetime = FixedStardomDatetime

        out = self.builder.scrape_stardom()

        self.assertNotIn("2026-10-19", out)


def vevent(uid, day):
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260101T000000Z",
        f"DTSTART;TZID=Asia/Tokyo:{day:%Y%m%d}T160000",
        f"DTEND;TZID=Asia/Tokyo:{day:%Y%m%d}T190000",
        "SUMMARY:NJPW — Road to DESTRUCTION · Tochigi", "END:VEVENT",
    ])


class CarryOverTests(unittest.TestCase):
    """NJPW's API drops a show the moment it has aired, so yesterday's calendar is the
    only record of it. Shows from the last week are carried over from the previous
    build's output until they roll off."""

    TODAY = date(2026, 9, 11)

    def setUp(self):
        self.builder = load_builder()

    def previous(self, *blocks):
        return "BEGIN:VCALENDAR\r\n" + "\r\n".join(blocks) + "\r\nEND:VCALENDAR\r\n"

    def test_a_show_that_aired_this_week_is_carried_over_verbatim(self):
        block = vevent("njpw-656886@njpw-stardom-cal", date(2026, 9, 9))
        kept = self.builder.carry_over(self.previous(block), set(), self.TODAY)
        self.assertEqual([block], kept)

    def test_a_show_older_than_the_retention_window_rolls_off(self):
        block = vevent("njpw-1@njpw-stardom-cal", self.TODAY - __import__("datetime").timedelta(days=8))
        self.assertEqual([], self.builder.carry_over(self.previous(block), set(), self.TODAY))

    def test_a_show_the_fresh_fetch_still_lists_is_not_duplicated(self):
        block = vevent("njpw-1@njpw-stardom-cal", date(2026, 9, 9))
        kept = self.builder.carry_over(self.previous(block), {"njpw-1@njpw-stardom-cal"}, self.TODAY)
        self.assertEqual([], kept)

    def test_a_future_show_missing_from_the_fetch_is_dropped_as_cancelled(self):
        block = vevent("njpw-1@njpw-stardom-cal", date(2026, 9, 12))
        self.assertEqual([], self.builder.carry_over(self.previous(block), set(), self.TODAY))

    def test_emit_appends_carried_blocks_after_the_fresh_events(self):
        block = vevent("njpw-old@njpw-stardom-cal", date(2026, 9, 9))
        fresh = self.builder.Event(uid="njpw-new@test", summary="New", location="", desc="",
                                   date=date(2026, 9, 13))
        out = self.builder.emit([fresh], "20260101T000000Z", carried=[block])
        self.assertIn(block, out)
        self.assertLess(out.index("UID:njpw-new@test"), out.index("UID:njpw-old@"))
        self.assertTrue(out.endswith("END:VEVENT\r\nEND:VCALENDAR\r\n"))


class FixedDatetime:
    @classmethod
    def now(cls, tz=None):
        return __import__("datetime").datetime(2026, 1, 1, tzinfo=tz)

    @classmethod
    def strptime(cls, *args, **kwargs):
        return __import__("datetime").datetime.strptime(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
