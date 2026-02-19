# Granicus Scraper — Architecture Guide

This document explains how the Granicus scraper is structured, how the
pieces fit together, and how to extend it for new cities.

## High-level pipeline

Every meeting goes through this pipeline:

```
┌─────────────┐     ┌──────────────┐     ┌──────────┐     ┌──────────┐
│  Discovery   │ ──> │ Agenda Parse │ ──> │  Merge   │ ──> │ Assembly │
│ (discovery)  │     │  (parsers/)  │     │ (merge)  │     │(scraper) │
└─────────────┘     └──────────────┘     └──────────┘     └──────────┘
ViewPublisher.php    AgendaViewer.php      HTML items       Final JSON
RSS feed             → auto-detect         + JSON markers   per meeting
                     → pluggable parser    → fuzzy match
                                                    ↑
                                           ┌────────┴────────┐
                                           │ Media / Markers  │
                                           │    (media)       │
                                           │ JSON.php         │
                                           │ /player/clip/    │
                                           │ MinutesViewer    │
                                           └──────────────────┘
```

## Module map

| Module | Lines | Responsibility |
|---|---|---|
| `scraper.py` | ~510 | **Orchestrator.** HTTP session, phase coordination, meeting assembly. |
| `extraction.py` | ~370 | **Shared helpers.** Document/body/timestamp extraction, classification. Every parser composes from these. |
| `discovery.py` | ~340 | **Phase 1.** ViewPublisher HTML + RSS feed → list of `ClipRef`s. |
| `media.py` | ~260 | **Phase 2b.** Player page → video URL, duration, captions, index points. Also `JSON.php` markers and `MinutesViewer`. |
| `merge.py` | ~80 | **Phase 2 glue.** Fuzzy-match HTML agenda items with video markers. Enriches HTML items with timestamps; appends unmatched markers. |
| `sites.py` | ~80 | **Config.** `GranicusSite` entries for known cities with optional `parser_hints`. |
| `parsers/` | ~680 | **Phase 2a.** Pluggable agenda page parsers (see below). |

## The parser system

### How auto-detection works

`parsers/__init__.py` defines `ALL_PARSERS` — an ordered list of parser
instances sorted by specificity:

```python
ALL_PARSERS = [
    CssClassParser(),    # Most specific: requires class="Agenda Agenda0/1/2"
    HeadingsParser(),    # Specific: requires <h2>/<h3> with <span> children
    BoldParser(),        # Generic: any page with <b>/<strong> elements
    TableParser(),       # Generic: any page with <table> rows + links
    FlatParser(),        # Last resort: numbered lines in any structure
]
```

The scraper tries each in order:

```python
for parser in self.parsers:
    if parser.can_parse(soup):           # Quick structural check
        html_items = parser.parse(soup, ctx)  # Full extraction
        if html_items:
            break                         # First match wins
```

### How `parser_hints` work

A `GranicusSite` can specify `parser_hints=["headings"]` to reorder the
list so that parser runs first.  This is an **optimization** — the
auto-detection would find the right parser anyway, but hints skip the
`can_parse()` calls for inapplicable parsers.

### Existing parsers

| Parser | File | Pattern | Example city |
|---|---|---|---|
| `CssClassParser` | `css_classes.py` | `<a class="Agenda Agenda0/1/2">` links with `class="Document"` sibling links | Sacramento |
| `HeadingsParser` | `headings.py` | `<h2>/<h3>` headings with `<span>` children for number + title | Shoreline |
| `BoldParser` | `bold.py` | `<b>/<strong>` elements as item markers, docs/timestamps in parent container | Generic fallback |
| `TableParser` | `table.py` | `<table>` rows with cells for number, title, links | Generic fallback |
| `FlatParser` | `flat.py` | Numbered/lettered lines (`1. Title`) in any element | Last resort |

## Shared extraction helpers (`extraction.py`)

Parsers should **not** duplicate extraction logic. Instead, they compose
from these helpers:

### Title / order

- **`split_order_prefix(text)`** → `(order, title)` — Splits "1. Call to Order" into `("1", "Call to Order")`.
- **`classify_item(title)`** → `str | None` — Returns `"call_to_order"`, `"public_comment"`, `"consent"`, etc.

### Document extraction

- **`extract_documents_near(element, base_url)`** — Walks forward from an element collecting `class="Document"` links until the next agenda marker. Best for CSS-class and heading layouts.
- **`extract_documents_within(element, base_url)`** — Finds MetaViewer links inside a container. Best for table rows or bold-text containers.
- **`extract_meeting_level_docs(soup, base_url)`** — Finds meeting-level documents (full agenda PDF, packet).
- **`classify_document(text)`** → `str` — Returns `"staff_report"`, `"ordinance"`, etc.

### Body text extraction

- **`extract_body_from_table(element)`** — Sacramento-style: reads subsequent `<tr>` cells after the title row.
- **`extract_body_after_heading(heading)`** — Shoreline-style: reads `<p>` elements between headings.
- **`extract_body_after_bold(element)`** — Reads sibling text nodes after a `<strong><u>` header.

### Media / timestamps

- **`extract_media_ref(element, clip_id)`** — Finds video timestamp links (`entrytime`, `starttime`, `meta_id`).

### HTML analysis

- **`is_centered(element)`** — Returns True if element or ancestor is center-aligned (used to skip page chrome).
- **`extract_date_from_text(text)`** — Finds dates in text strings.
- **`extract_clip_id(href)`** — Extracts clip_id from a URL.
- **`extract_param(href, name)`** — Extracts a query parameter.

## How to add a new city

### Case 1: Existing HTML pattern matches

If the city's `GeneratedAgendaViewer.php` uses one of the patterns above
(CSS classes, headings, bold text, tables), no code changes are needed:

```python
# sites.py
MY_CITY = GranicusSite(
    subdomain="mycity",
    jurisdiction_id="ocd-jurisdiction/country:us/state:xx/place:mycity/government",
    jurisdiction_name="City of My City",
    views={1: "City Council"},
    # Optional: skip auto-detection if you know the pattern
    parser_hints=["css_classes"],
)
```

### Case 2: New HTML pattern

1. **Save a fixture** — download the agenda page HTML to `fixtures/mycity/`.

2. **Create a parser** — `parsers/mycity.py` (or a more generic name if the
   pattern isn't city-specific):

```python
"""Parser for cities using <div class="agenda-row"> patterns."""

import re
from bs4 import BeautifulSoup
from scrapers.granicus.extraction import (
    classify_item,
    extract_documents_within,
    extract_media_ref,
    split_order_prefix,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext


class AgendaRowParser(AgendaParser):

    def can_parse(self, soup: BeautifulSoup) -> bool:
        # Quick check: does the page have the structural marker?
        return bool(soup.find("div", class_="agenda-row"))

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        items = []
        for row in soup.find_all("div", class_="agenda-row"):
            text = row.get_text(strip=True)
            if not text or len(text) < 3:
                continue

            order, title = split_order_prefix(text)
            item = {
                "title": title,
                "source": "agenda_page",
                "classification": classify_item(title),
            }
            if order:
                item["order"] = order

            docs = extract_documents_within(row, ctx.base_url)
            if docs:
                item["documents"] = docs

            media = extract_media_ref(row, ctx.clip_id)
            if media:
                item["media_ref"] = media

            items.append(item)
        return items
```

3. **Register it** in `parsers/__init__.py`:

```python
from scrapers.granicus.parsers.agenda_row import AgendaRowParser

ALL_PARSERS = [
    CssClassParser(),
    HeadingsParser(),
    AgendaRowParser(),   # ← insert before generic fallbacks
    BoldParser(),
    TableParser(),
    FlatParser(),
]
```

4. **Test against the fixture**:

```python
from bs4 import BeautifulSoup
from scrapers.granicus.parsers import ALL_PARSERS, ParseContext

with open("fixtures/mycity/agenda.html") as f:
    soup = BeautifulSoup(f.read(), "html.parser")

ctx = ParseContext(base_url="https://mycity.granicus.com", view_id=1, clip_id=123)

for parser in ALL_PARSERS:
    if parser.can_parse(soup):
        items = parser.parse(soup, ctx)
        if items:
            print(f"{type(parser).__name__}: {len(items)} items")
            for item in items:
                print(f"  [{item.get('order', '-')}] {item['title']}")
            break
```

## Agenda item schema

Every parser returns a list of dicts.  Each item should have:

| Key | Type | Required | Description |
|---|---|---|---|
| `title` | `str` | yes | The agenda item title |
| `source` | `str` | yes | `"agenda_page"` or `"video_markers"` |
| `order` | `str` | no | Display order: `"1"`, `"A"`, `"IV"`, `"3a"` |
| `level` | `int` | no | Nesting depth: `0` = section, `1` = item, `2` = sub-item |
| `classification` | `str` | no | Semantic type: `"call_to_order"`, `"consent"`, etc. |
| `documents` | `list[dict]` | no | Attached docs, each with `title`, `url`, `type` |
| `media_ref` | `dict` | no | Video timestamp: `{"media_id": "clip-N", "offset_seconds": N}` |
| `body` | `str` | no | Descriptive text (recommendation, location, etc.) |

## Data flow for a single meeting

```
scraper.scrape_meeting(meeting_ref)
│
├── _scrape_agenda(url, view_id, clip_id)
│   │
│   ├── _soup(url)                           # Fetch HTML
│   ├── for parser in self.parsers:          # Auto-detect + parse
│   │     parser.can_parse(soup)
│   │     parser.parse(soup, ctx)            # → html_items
│   ├── extract_meeting_level_docs(soup)     # → meeting_docs
│   ├── scrape_agenda_json(clip_id)          # → marker_items
│   └── merge_agenda_sources(html, markers)  # → final items
│
├── scrape_media(clip_id, view_id)
│   │
│   ├── _soup(player_url)                    # Fetch player page
│   ├── _find_video_url(soup)                # → video stream URL
│   ├── _find_duration(soup)                 # → seconds
│   ├── _find_caption_url(soup, clip_id)     # → captions URL
│   └── _extract_index_points(soup, clip_id) # → index_items
│
├── merge_agenda_sources(agenda, index)      # Merge again if index points found
│
├── scrape_minutes_link(minutes_url)         # → minutes_doc
│
└── assemble meeting JSON                    # → output file
```

## Design principles

1. **Parsers are stateless.** They receive a `BeautifulSoup` and a `ParseContext`,
   return a list of dicts. No HTTP calls, no side effects.

2. **Helpers are composable.** `extract_documents_near()`, `split_order_prefix()`,
   etc. work independently — parsers pick the ones they need.

3. **Auto-detection is safe.** `can_parse()` is fast (one `soup.find()` call).
   Parsers are tried most-specific-first, so generic fallbacks only fire when
   no specific pattern matches.

4. **The scraper is a thin orchestrator.** It owns the HTTP session, phase
   coordination, and output assembly — but delegates all HTML parsing to
   the parser plugins and all extraction logic to shared helpers.
