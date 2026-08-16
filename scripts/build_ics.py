#!/usr/bin/env python3
"""
Build a combined NJPW + Stardom iCalendar feed.

Sources
-------
NJPW (reliable):
  * Official schedule API, one static JSON per tour ("series"):
      https://app.njpw1972.com/series/tournaments/schedule/list/<ID>.json
    The IDs are discovered from the site's own paginated schedule index, so tours roll
    over on their own (NJPW deletes a series JSON once the tour has finished, which used
    to break the build the morning after every tour). Gives English venues + confirmed
    bell times, full announced horizon (untruncated).
  * Public "njpwworld Schedule" Google Calendar (auto-enumerates near-term shows incl.
    one-offs). Used only to FILL dates the API series don't already cover, so brand-new
    tours show up ~1 month out before their series ID is added.

Stardom (best-effort):
  * data/stardom.json (maintained by hand; Stardom has no public API). time=null -> all-day.

Times are emitted in venue-local zones (Asia/Tokyo, or venue-specific US timezones) with
VTIMEZONE blocks, so subscribers' devices convert to their own timezone automatically.
No bell time is ever invented: unknown Stardom times become all-day events.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "njpw-stardom.ics"

NJPW_API = "https://app.njpw1972.com/series/tournaments/schedule/list/{id}.json"
NJPW_SERIES_INDEX = "https://app.njpw1972.com/series/tournaments/schedule/pagination/{page}.json"
NJPW_SERIES_META = "https://app.njpw1972.com/series/posts/{id}.json"
GCAL_ICS = ("https://calendar.google.com/calendar/ical/"
            "n6l35ni6rcbffi1m4m5g5ocnh4%40group.calendar.google.com/public/basic.ics")
UA = "Mozilla/5.0 (compatible; njpw-stardom-cal/1.0; +https://github.com/)"
GCAL_WINDOW_DAYS = 60  # how far ahead to trust/borrow from the Google mirror

JST = timezone(timedelta(hours=9))

# NJPW's API currently exposes US shows with prefecture="USA", so country alone is not
# enough to choose the venue-local timezone. Unknown US venues fail the build rather than
# publishing a calendar with the wrong local time.
US_VENUE_TZ = {
    "NOW Arena": "America/Chicago",
    "Madison Square Garden": "America/New_York",
    "Walter Pyramid": "America/Los_Angeles",
    "Long Beach Convention": "America/Los_Angeles",
}


def njpw_timezone(pref, stadium):
    if pref != "USA":
        return "Asia/Tokyo"
    stadium_key = stadium.casefold()
    for venue, tz in US_VENUE_TZ.items():
        if venue.casefold() in stadium_key:
            return tz
    return None


def api_covered_dates(day, hm, tz):
    dates = {day}
    if hm and tz != "Asia/Tokyo":
        local = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=ZoneInfo(tz))
        dates.add(local.astimezone(JST).date())
    return dates

# Stardom scraper (best-effort): official WordPress site, monthly grid + per-show detail page.
STARDOM_MONTH = "https://wwr-stardom.com/en/schedule/?ym={ym}"
STARDOM_DETAIL = "https://wwr-stardom.com/en/schedule/{slug}/"
STARDOM_MONTHS = 4              # months of grid to enumerate from the current JST month
STARDOM_DETAIL_LOOKAHEAD = 40  # only fetch a detail page for a bell time within N days
STARDOM_VENUE_HINT = {         # nicer LOCATION for common slug tokens; else title-cased token
    "korakuen": "Korakuen Hall, Tokyo",
    "otaku": "Ota Ward Gymnasium, Tokyo",
    "ota": "Ota Ward Gymnasium, Tokyo",
}


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def parse_bell(s):
    """'17:00' / '18:30' / '7PM' / '6:30 PM' / '5:30 PM' -> (hour, minute) or None."""
    if not s:
        return None
    t = s.strip().upper().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= minute <= 59:
            return h, minute
        return None
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(AM|PM)", t)
    if m:
        raw_h = int(m.group(1))
        minute = int(m.group(2) or 0)
        if not (1 <= raw_h <= 12 and 0 <= minute <= 59):
            return None
        h = raw_h % 12
        if m.group(3) == "PM":
            h += 12
        return h, minute
    return None


class Event:
    __slots__ = ("uid", "summary", "location", "desc", "date", "hm", "tz")

    def __init__(self, uid, summary, location, desc, date, hm=None, tz="Asia/Tokyo"):
        self.uid = uid
        self.summary = summary
        self.location = location
        self.desc = desc
        self.date = date        # datetime.date
        self.hm = hm            # (h, m) or None for all-day
        self.tz = tz


def discover_njpw_series_ids():
    """Current + upcoming series post_ids from the site's own schedule index.

    Same endpoint njpw1972.com/schedule paginates over: AllCount is the page count and
    each page carries a handful of posts. Anything odd (fetch failure, missing page
    count, empty index) is reported as a failure rather than silently publishing a
    partial schedule. Returns (ids, failures).
    """
    ids, page, pages = [], 1, None
    while pages is None or page <= pages:
        url = NJPW_SERIES_INDEX.format(page=page)
        try:
            d = json.loads(fetch(url))
        except Exception as e:
            print(f"  ! NJPW series index page {page} fetch failed: {e}", file=sys.stderr)
            return [], [("series-index", str(e))]
        if not isinstance(d, dict) or not isinstance(d.get("AllCount"), int) or d["AllCount"] < 1:
            msg = f"index page {page} had no usable page count"
            print(f"  ! NJPW series index failed: {msg}", file=sys.stderr)
            return [], [("series-index", msg)]
        pages = d["AllCount"]
        posts = d.get("posts") or []
        if not posts:
            break
        for p in posts:
            pid = str((p or {}).get("post_id") or "").strip()
            if pid.isdigit() and pid not in ids:
                ids.append(pid)
        page += 1
    if not ids:
        msg = "schedule index listed no series"
        print(f"  ! NJPW series index failed: {msg}", file=sys.stderr)
        return [], [("series-index", msg)]
    return ids, []


def series_title(sid):
    """Human tour name, e.g. 'SUPER Jr. TAG LEAGUE 2026 ～Road to POWER STRUGGLE～'.

    Cosmetic only: the schedule itself comes from the list endpoint, so a failure here
    degrades the summary instead of blocking the build.
    """
    try:
        d = json.loads(fetch(NJPW_SERIES_META.format(id=sid)))
    except Exception as e:
        print(f"  - NJPW series {sid} title fetch failed: {e}", file=sys.stderr)
        return ""
    if not isinstance(d, dict):
        return ""
    return re.sub(r"\s+", " ", d.get("series_title") or "").strip()


def njpw_from_api():
    events = []
    covered = set()   # exact dates an API series already accounts for
    spans = []        # retained for callers that still pass API tour ranges
    failures = []
    series_ids, failures = discover_njpw_series_ids()
    if failures:
        return events, covered, spans, failures
    for sid in series_ids:
        try:
            d = json.loads(fetch(NJPW_API.format(id=sid)))
        except Exception as e:
            print(f"  ! NJPW series {sid} fetch failed: {e}", file=sys.stderr)
            failures.append((sid, str(e)))
            continue
        if not isinstance(d, dict):
            print(f"  ! NJPW series {sid} response was not a JSON object", file=sys.stderr)
            failures.append((sid, "invalid response shape"))
            continue
        name = (series_title(sid) or d.get("twitter_hash_tags")
                or d.get("stadium_name") or "NJPW Event")
        shows = d.get("tournaments")
        if not isinstance(shows, list) or not shows:
            print(f"  ! NJPW series {sid} response had no tournament shows", file=sys.stderr)
            failures.append((sid, "no tournament shows"))
            continue
        span_days = []
        for idx, t in enumerate(shows, 1):
            ds = (t.get("event_start_date") or "")[:10]
            try:
                day = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            v = t.get("venue") or {}
            pref = (v.get("prefecture") or "").strip()
            stadium = (v.get("stadium_name") or "").strip()
            tz = njpw_timezone(pref, stadium)
            if not tz:
                msg = f"unknown timezone for {pref} venue: {stadium or '(missing venue)'}"
                print(f"  ! NJPW series {sid} {msg}", file=sys.stderr)
                failures.append((sid, msg))
                continue
            hm = parse_bell(t.get("start_time"))
            loc = ", ".join(x for x in (stadium, pref) if x)
            if pref and pref != "USA" and pref not in stadium:
                loc = f"{stadium}, {pref}" if stadium else pref
            desc_bits = []
            if hm:
                desc_bits.append(f"Bell {hm[0]:02d}:{hm[1]:02d} {'local' if tz!='Asia/Tokyo' else 'JST'} (NJPW official).")
            if t.get("doors_open"):
                desc_bits.append(f"Doors {t['doors_open']}.")
            region = pref if pref else ""
            source_id = t.get("post_id") or f"{sid}-{ds}-{idx}"
            events.append(Event(
                uid=f"njpw-{source_id}@njpw-stardom-cal",
                summary=f"NJPW — {name}" + (f" · {region}" if region else ""),
                location=loc, desc=" ".join(desc_bits),
                date=day, hm=hm, tz=tz))
            covered.update(api_covered_dates(day, hm, tz))
            span_days.append(day)
        if span_days:
            spans.append((min(span_days) - timedelta(days=1),
                          max(span_days) + timedelta(days=1)))
    return events, covered, spans, failures


def njpw_from_gcal(covered, spans):
    """Borrow near-term NJPW shows the API series don't cover (one-offs, not-yet-added tours)."""
    events = []
    try:
        raw = fetch(GCAL_ICS)
    except Exception as e:
        print(f"  ! Google mirror fetch failed: {e}", file=sys.stderr)
        return events
    raw = raw.replace("\r\n", "\n").replace("\n ", "")  # unfold
    today = datetime.now(JST).date()
    horizon = today + timedelta(days=GCAL_WINDOW_DAYS)
    seen_uids = set()
    for idx, block in enumerate(re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S), 1):
        mds = re.search(r"\nDTSTART[^:]*:([0-9T]+Z?)", block)
        msum = re.search(r"\nSUMMARY:(.*)", block)
        muid = re.search(r"\nUID:(.*)", block)
        if not mds:
            continue
        rawdt = mds.group(1)
        try:
            if rawdt.endswith("Z"):
                dt = datetime.strptime(rawdt, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(JST)
                hm = (dt.hour, dt.minute)
            elif "T" in rawdt:
                dt = datetime.strptime(rawdt, "%Y%m%dT%H%M%S").replace(tzinfo=JST)
                hm = (dt.hour, dt.minute)
            else:
                dt = datetime.strptime(rawdt, "%Y%m%d").replace(tzinfo=JST)
                hm = None
        except ValueError:
            continue
        day = dt.date()
        if not (today <= day <= horizon):
            continue
        if day in covered:
            continue
        source_uid = (muid.group(1).strip() if muid else f"{day:%Y%m%d}-{idx}")
        source_uid = re.sub(r"[^A-Za-z0-9@._-]+", "-", source_uid).strip("-")
        if source_uid in seen_uids:
            continue
        seen_uids.add(source_uid)
        summary = (msum.group(1).strip() if msum else "NJPW Event")
        # summaries look like "Road to G1 CLIMAX （東京・後楽園ホール）"
        name, _, venue = summary.partition("（")  # fullwidth (
        name = name.strip() or "NJPW Event"
        venue = venue.rstrip("）").strip()  # trailing fullwidth )
        events.append(Event(
            uid=f"njpw-gcal-{source_uid}",
            summary=f"NJPW — {name}",
            location=venue,
            desc="Source: njpwworld Schedule (near-term).",
            date=day, hm=hm, tz="Asia/Tokyo"))
    return events


def stardom_month_shows(html_text):
    """Parse a monthly grid page -> [(YYYYMMDD, slug, name)] for Stardom's OWN shows only.

    box_game = Stardom in-ring show; box_other = other promotion; box_event = press conf.
    """
    import html as _html
    shows = []
    for slug, box, title in re.findall(
            r'href="[^"]*/schedule/(20\d{6}_[^/"]+)/"[^>]*><div class="(box_[a-z]+)">(.*?)</div>',
            html_text, re.S):
        if box != "box_game":
            continue
        name = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        shows.append((slug[:8], slug, name))
    return shows


def parse_stardom_bell(html_text):
    """Detail page -> (h, m) bell time or None. Uses the precise English phrase so it does
    not pick up the autograph-session time (Japanese 開始 is ambiguous)."""
    import html as _html
    t = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html_text)))
    m = re.search(r"start time for the main event\s*(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if h < 24 and mi < 60 else None


def clean_stardom_name(n):
    n = re.sub(r"^Sammy presents\s*", "", n, flags=re.I)
    n = n.replace("5★STAR", "5STAR").replace("5☆STAR", "5STAR")
    n = re.split(r"[　-鿿＀-￯]", n)[0]  # drop trailing JP venue text
    return re.sub(r"\s+", " ", n).strip()


def scrape_stardom():
    """Best-effort: {date_str: {'name','venue','hm'}} from the official site, {} on total failure."""
    today = datetime.now(JST).date()
    months, y, mo = [], today.year, today.month
    for _ in range(STARDOM_MONTHS):
        months.append(f"{y}{mo:02d}")
        mo = mo + 1 if mo < 12 else 1
        y = y + 1 if mo == 1 else y
    out, got_any = {}, False
    for ym in months:
        try:
            page = fetch(STARDOM_MONTH.format(ym=ym))
        except Exception as e:
            print(f"  ! Stardom grid {ym} failed: {e}", file=sys.stderr)
            continue
        got_any = True
        for ymd, slug, name in stardom_month_shows(page):
            ds = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            if ds in out:
                continue
            try:
                day = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            token = slug.split("_", 1)[1] if "_" in slug else ""
            venue = STARDOM_VENUE_HINT.get(token, token.replace("-", " ").title())
            hm = None
            if today <= day <= today + timedelta(days=STARDOM_DETAIL_LOOKAHEAD):
                try:
                    hm = parse_stardom_bell(fetch(STARDOM_DETAIL.format(slug=slug)))
                except Exception as e:
                    print(f"  - Stardom detail {slug} failed: {e}", file=sys.stderr)
            out[ds] = {"name": clean_stardom_name(name), "venue": venue, "hm": hm}
    return out if got_any else {}


def stardom_events():
    """Union of the hand-maintained baseline (data/stardom.json) and the best-effort scrape.

    Scrape wins for bell times (fresh); baseline wins for curated names/venues; neither side
    drops the other's shows (the live grid isn't always a superset of announced tour stops).
    """
    baseline = {}
    try:
        d = json.loads((DATA / "stardom.json").read_text(encoding="utf-8"))
        for s in d.get("shows", []):
            if s.get("date"):
                baseline[s["date"]] = s
    except Exception as e:
        print(f"  ! Stardom baseline read failed: {e}", file=sys.stderr)

    scraped = scrape_stardom()
    print(f"Stardom: {len(baseline)} baseline + {len(scraped)} scraped "
          f"= {len(set(baseline) | set(scraped))} shows")

    events = []
    for ds in sorted(set(baseline) | set(scraped)):
        try:
            day = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            continue
        b, s = baseline.get(ds), scraped.get(ds)
        hm = (s or {}).get("hm") or parse_bell((b or {}).get("time"))  # scrape time first, never invent
        name = (b or {}).get("name") or (s or {}).get("name") or "Stardom"
        venue = (b or {}).get("venue") or (s or {}).get("venue") or ""
        summary = "Stardom" + name[7:] if name.lower().startswith("stardom") else f"Stardom — {name}"
        desc = (f"Bell {hm[0]:02d}:{hm[1]:02d} JST." if hm else "Bell time not yet announced.")
        events.append(Event(
            uid=f"stardom-{ds}@njpw-stardom-cal",
            summary=summary, location=venue, desc=desc,
            date=day, hm=hm, tz="Asia/Tokyo"))
    return events


# ---- iCalendar emission -----------------------------------------------------

def esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC5545 folding at 75 octets."""
    out = []
    b = line.encode("utf-8")
    while len(b) > 73:
        cut = 73
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b[cut:]
        line = " " + b.decode("utf-8")
        b = line.encode("utf-8")
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


VTIMEZONES = """BEGIN:VTIMEZONE
TZID:Asia/Tokyo
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0900
TZOFFSETTO:+0900
TZNAME:JST
END:STANDARD
END:VTIMEZONE
BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
TZNAME:PDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
TZNAME:PST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE""".replace("\n", "\r\n")


def emit(events, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//njpw-stardom-cal//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:NJPW & Stardom", "X-WR-TIMEZONE:Asia/Tokyo",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
        "X-WR-CALDESC:" + esc("NJPW (official API) + Stardom shows. Auto-rebuilt daily. "
                              "Times auto-convert to your device timezone."),
    ]
    body = [VTIMEZONES]
    for e in sorted(events, key=lambda x: (x.date, x.hm or (0, 0))):
        ev = ["BEGIN:VEVENT", f"UID:{e.uid}", f"DTSTAMP:{stamp}"]
        if e.hm:
            start = f"{e.date:%Y%m%d}T{e.hm[0]:02d}{e.hm[1]:02d}00"
            endh, endm = (e.hm[0] + 3) % 24, e.hm[1]
            endday = e.date + (timedelta(days=1) if e.hm[0] + 3 >= 24 else timedelta())
            end = f"{endday:%Y%m%d}T{endh:02d}{endm:02d}00"
            ev.append(f"DTSTART;TZID={e.tz}:{start}")
            ev.append(f"DTEND;TZID={e.tz}:{end}")
        else:
            ev.append(f"DTSTART;VALUE=DATE:{e.date:%Y%m%d}")
            ev.append(f"DTEND;VALUE=DATE:{e.date + timedelta(days=1):%Y%m%d}")
        ev.append("SUMMARY:" + esc(e.summary))
        if e.location:
            ev.append("LOCATION:" + esc(e.location))
        if e.desc:
            ev.append("DESCRIPTION:" + esc(e.desc))
        ev.append("END:VEVENT")
        body.append("\r\n".join(fold(x) for x in ev))
    head = "\r\n".join(fold(x) for x in lines)
    lines_out = head + "\r\n" + "\r\n".join(body) + "\r\nEND:VCALENDAR\r\n"
    return lines_out


def main():
    # Deterministic DTSTAMP: identical source data -> byte-identical file, so the daily
    # CI job only commits when something real changed (no wall-clock churn). DTSTAMP is
    # metadata only; subscribed clients update events by UID + DTSTART, not DTSTAMP.
    stamp = "20260101T000000Z"
    njpw, covered, spans, api_failures = njpw_from_api()
    if api_failures:
        print("Refusing to write: required NJPW API fetch failed.", file=sys.stderr)
        sys.exit(1)
    print(f"NJPW from API: {len(njpw)} shows")
    borrowed = njpw_from_gcal(covered, spans)
    print(f"NJPW from Google mirror (extra near-term): {len(borrowed)} shows")
    star = stardom_events()
    events = njpw + borrowed + star
    if len(events) < 10:
        print("Refusing to write: implausibly few events (source failure?)", file=sys.stderr)
        sys.exit(1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(emit(events, stamp).encode("utf-8"))
    print(f"Wrote {OUT} with {len(events)} events.")


if __name__ == "__main__":
    main()
