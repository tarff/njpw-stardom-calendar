#!/usr/bin/env python3
"""
Build a combined NJPW + Stardom iCalendar feed.

Sources
-------
NJPW (reliable):
  * Official schedule API, one static JSON per tour ("series"):
      https://app.njpw1972.com/series/tournaments/schedule/list/<ID>.json
    IDs are listed in data/njpw_series.txt. Gives English venues + confirmed bell times,
    full announced horizon (untruncated).
  * Public "njpwworld Schedule" Google Calendar (auto-enumerates near-term shows incl.
    one-offs). Used only to FILL dates the API series don't already cover, so brand-new
    tours show up ~1 month out before their series ID is added.

Stardom (best-effort):
  * data/stardom.json (maintained by hand; Stardom has no public API). time=null -> all-day.

Times are emitted in venue-local zones (Asia/Tokyo, or America/Chicago for US shows) with
VTIMEZONE blocks, so subscribers' devices convert to their own timezone automatically.
No bell time is ever invented: unknown Stardom times become all-day events.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "njpw-stardom.ics"

NJPW_API = "https://app.njpw1972.com/series/tournaments/schedule/list/{id}.json"
GCAL_ICS = ("https://calendar.google.com/calendar/ical/"
            "n6l35ni6rcbffi1m4m5g5ocnh4%40group.calendar.google.com/public/basic.ics")
UA = "Mozilla/5.0 (compatible; njpw-stardom-cal/1.0; +https://github.com/)"
GCAL_WINDOW_DAYS = 60  # how far ahead to trust/borrow from the Google mirror

JST = timezone(timedelta(hours=9))

# Pretty names for NJPW series hashtags (extend as needed).
SERIES_NAMES = {
    "G1CLIMAX36": "G1 Climax 36",
}

# Prefecture -> IANA timezone for non-Japan shows. JP prefectures default to Asia/Tokyo.
FOREIGN_TZ = {
    "USA": "America/Chicago",  # only current US venue (NOW Arena) is in Illinois / Central
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
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(AM|PM)", t)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3) == "PM":
            h += 12
        return h, int(m.group(2) or 0)
    return None


def pretty_series(hashtag, fallback):
    if hashtag and hashtag in SERIES_NAMES:
        return SERIES_NAMES[hashtag]
    if hashtag:
        # e.g. NEWJAPANCUP2026 -> "New Japan Cup 2026"-ish; leave hashtag if unmapped
        return hashtag
    return fallback or "NJPW Event"


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


def load_njpw_series_ids():
    ids = []
    for line in (DATA / "njpw_series.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            ids.append(line)
    return ids


def njpw_from_api():
    events = []
    covered = set()   # exact dates an API series already accounts for
    spans = []        # (start_date, end_date) per loaded tour, to swallow tz date-shifts
    for sid in load_njpw_series_ids():
        try:
            d = json.loads(fetch(NJPW_API.format(id=sid)))
        except Exception as e:
            print(f"  ! NJPW series {sid} fetch failed: {e}", file=sys.stderr)
            continue
        name = pretty_series(d.get("twitter_hash_tags"), d.get("stadium_name"))
        shows = d.get("tournaments") or []
        span_days = []
        for t in shows:
            ds = (t.get("event_start_date") or "")[:10]
            try:
                day = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            v = t.get("venue") or {}
            pref = (v.get("prefecture") or "").strip()
            stadium = (v.get("stadium_name") or "").strip()
            tz = FOREIGN_TZ.get(pref, "Asia/Tokyo")
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
            events.append(Event(
                uid=f"njpw-{sid}-{ds}@njpw-stardom-cal",
                summary=f"NJPW — {name}" + (f" · {region}" if region else ""),
                location=loc, desc=" ".join(desc_bits),
                date=day, hm=hm, tz=tz))
            covered.add(day)
            span_days.append(day)
        if span_days:
            spans.append((min(span_days) - timedelta(days=1),
                          max(span_days) + timedelta(days=1)))
    return events, covered, spans


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
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        mds = re.search(r"\nDTSTART[^:]*:([0-9T]+Z?)", block)
        msum = re.search(r"\nSUMMARY:(.*)", block)
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
        if day in covered or not (today <= day <= horizon):
            continue
        if any(a <= day <= b for a, b in spans):  # inside a loaded tour (tz date-shift dup)
            continue
        summary = (msum.group(1).strip() if msum else "NJPW Event")
        # summaries look like "Road to G1 CLIMAX （東京・後楽園ホール）"
        name, _, venue = summary.partition("（")  # fullwidth (
        name = name.strip() or "NJPW Event"
        venue = venue.rstrip("）").strip()  # trailing fullwidth )
        events.append(Event(
            uid=f"njpw-gcal-{day:%Y%m%d}@njpw-stardom-cal",
            summary=f"NJPW — {name}",
            location=venue,
            desc="Source: njpwworld Schedule (near-term).",
            date=day, hm=hm, tz="Asia/Tokyo"))
        covered.add(day)
    return events


def stardom_events():
    events = []
    d = json.loads((DATA / "stardom.json").read_text(encoding="utf-8"))
    for s in d.get("shows", []):
        try:
            day = datetime.strptime(s["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        hm = parse_bell(s.get("time"))
        desc = (f"Bell {s['time']} JST (confirmed)." if s.get("time")
                else "Bell time not yet announced.")
        events.append(Event(
            uid=f"stardom-{s['date']}@njpw-stardom-cal",
            summary=f"Stardom — {s.get('name','Stardom')}",
            location=s.get("venue", ""), desc=desc,
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
END:VTIMEZONE""".replace("\n", "\r\n")


def emit(events, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//njpw-stardom-cal//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:NJPW & Stardom", "X-WR-TIMEZONE:Asia/Tokyo",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
        "X-WR-CALDESC:" + esc(f"NJPW (official API) + Stardom shows. Auto-rebuilt daily. "
                              f"Times auto-convert to your device timezone. Last build {stamp} UTC."),
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    njpw, covered, spans = njpw_from_api()
    print(f"NJPW from API: {len(njpw)} shows")
    borrowed = njpw_from_gcal(covered, spans)
    print(f"NJPW from Google mirror (extra near-term): {len(borrowed)} shows")
    star = stardom_events()
    print(f"Stardom: {len(star)} shows")
    events = njpw + borrowed + star
    if len(events) < 10:
        print("Refusing to write: implausibly few events (source failure?)", file=sys.stderr)
        sys.exit(1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(emit(events, stamp).encode("utf-8"))
    print(f"Wrote {OUT} with {len(events)} events.")


if __name__ == "__main__":
    main()
