# NJPW & Stardom calendar

An auto-updating iCalendar feed of **New Japan Pro-Wrestling** and **Stardom** show dates and
bell times, rebuilt daily by GitHub Actions. Subscribe once on your iPhone and it stays current.

## Subscribe on iPhone

Settings → **Calendar** → **Accounts** → **Add Account** → **Other** → **Add Subscribed Calendar**,
then paste:

```
https://raw.githubusercontent.com/tarff/njpw-stardom-calendar/main/docs/njpw-stardom.ics
```

Times are stored in each venue's local zone, so your phone shows them in **your** timezone
automatically (e.g. a 15:00 JST bell shows as 16:00 in Sydney during winter).

## What's in it

- **NJPW** — from the official NJPW schedule API (`app.njpw1972.com`), English venues + confirmed
  bell times, full announced horizon. Near-term one-off shows are also pulled from the public
  "njpwworld Schedule" Google Calendar so nothing near-term is missed.
- **Stardom** — scraped best-effort from the official site (`wwr-stardom.com`): the monthly
  schedule grid for show dates/names (Stardom's own `box_game` shows only — other-promotion
  and press-conference entries are excluded), and each near-term show's detail page for the
  confirmed bell time. Merged as a **union** with the hand-maintained baseline
  [`data/stardom.json`](data/stardom.json): the scrape fills bell times and adds newly-listed
  shows, while the baseline is never dropped (and wins for curated names/venues). If the scrape
  fails, the baseline is used as-is. Shows with no announced bell time appear as **all-day** —
  no time is ever guessed.

## Keeping it current

- **NJPW:** fully self-updating. The builder reads NJPW's own paginated schedule index, so new
  tours appear as soon as NJPW lists them and finished tours drop off by themselves — no ID list
  to maintain. Anything the index hasn't picked up yet still appears ~1 month out via the Google
  mirror.
- **Stardom:** mostly self-updating now — the daily scrape fills bell times and adds newly-listed
  shows automatically. [`data/stardom.json`](data/stardom.json) remains the fallback/override:
  edit it to correct a scrape mistake, pre-seed a show announced by press release before it hits
  the live grid, or provide a bell time the site lists only in Japanese.

Any push to `data/` or `scripts/` rebuilds immediately; otherwise it rebuilds daily at 05:00 JST.

## Build locally

```
python scripts/build_ics.py    # writes docs/njpw-stardom.ics
```

No dependencies — Python 3.11 standard library only.
