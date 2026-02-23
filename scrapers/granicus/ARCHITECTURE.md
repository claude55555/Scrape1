# Granicus Scraper — Architecture Guide

This document explains how the Granicus scraper is structured, how the
pieces fit together, and how to extend it for new cities.

## High-level pipeline

Every meeting goes through this pipeline:

```
┌─────────────┐     ┌──────────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Discovery   │ ──> │ Agenda Parse │ ──> │  Merge   │ ──> │ Assembly │ ──> │ Download │
│ (discovery)  │     │  (parsers/)  │     │ (merge)  │     │(scraper) │     │(optional)│
└─────────────┘     └──────────────┘     └──────────┘     └──────────┘     └──────────┘
ViewPublisher.php    AgendaViewer.php      HTML items       Final JSON      Fetch files,
RSS feed             → auto-detect         + JSON markers   per meeting     add local_path
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
| `../download.py` | ~250 | **Post-scrape.** Optional file download with extension filtering. Scraper-agnostic (lives in `scrapers/`, not `scrapers/granicus/`). |

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
│   ├── _find_caption_url(soup, clip_id)     # → captions {url, media_type}
│   └── _extract_index_points(soup, clip_id) # → index_items
│
├── merge_agenda_sources(agenda, index)      # Merge again if index points found
│
├── scrape_minutes_link(minutes_url)         # → minutes_doc
│
├── assemble meeting JSON                    # → meeting dict
│
└── save_meeting(meeting)                    # In BaseScraper:
    │
    ├── validate(meeting)                    # Schema check
    ├── download_meeting_files(meeting, ...) # Optional: if DownloadConfig.enabled
    │   │                                    # Walks documents[], media[], captions,
    │   │                                    # agenda_items[] (recursive).
    │   │                                    # Filters by extension, downloads,
    │   │                                    # adds local_path to each object.
    │   └── (skips location.url, jurisdiction.url, sources[].url)
    └── write JSON to disk
```

## File download system (`scrapers/download.py`)

The download module is **scraper-agnostic** — it operates on any meeting dict
conforming to the schema, not just Granicus output. It lives in `scrapers/`
rather than `scrapers/granicus/`.

### What gets downloaded

The module walks these locations in the meeting dict:

| Location | Typical content | Downloaded by default? |
|---|---|---|
| `documents[].url` | Agenda PDFs, minutes, packets | Yes |
| `agenda_items[].documents[].url` | Staff reports, attachments (recursive into `items[]`) | Yes |
| `media[].captions.url` | VTT/SRT caption files | Yes |
| `media[].url` | Video/audio streams | No (large files) |

These are **never** downloaded (informational links, not files):
- `location.url` — Zoom link / venue page
- `jurisdiction.url` — municipality homepage
- `sources[].url` — scrape provenance

### Extension filtering

`DownloadConfig` supports three modes:

1. **`include_types` set** — only download matching extensions (strictest)
2. **`exclude_types` set** — download everything *except* matching extensions
3. **Neither set** — use built-in defaults (docs/captions yes, large media no)

If both are set, `include_types` takes precedence.

### How it integrates

```python
# CLI: --download flag builds a DownloadConfig and passes to scraper
scraper = GranicusScraper(site, output_dir=out, download_config=dl_config)

# Programmatic: pass DownloadConfig directly
from scrapers.download import DownloadConfig

dl = DownloadConfig(enabled=True, include_types={".pdf", ".vtt"})
scraper = GranicusScraper(site, download_config=dl)
```

`BaseScraper.save_meeting()` calls `download_meeting_files()` after validation
but before writing the JSON, so the output file includes `local_path` fields.

### File layout on disk

```
output/erie/
├── board-of-trustees_2025-03-15_clip-6627.json
└── files/
    └── clip-6627/
        ├── agenda.pdf
        ├── minutes.pdf
        ├── captions.vtt
        └── staff-report-meta-12345.pdf
```

`local_path` values are relative to the JSON file's directory.

### The `captions` object

Media entries use a structured `captions` object instead of a bare URL string:

```json
{
  "id": "clip-6627",
  "url": "https://erie.granicus.com/...",
  "type": "video",
  "captions": {
    "url": "https://erie.granicus.com/captions/6627.vtt",
    "media_type": "text/vtt",
    "local_path": "files/clip-6627/captions.vtt"
  }
}
```

`media.py` populates `captions.url` and `captions.media_type` during scraping.
The download module adds `captions.local_path` if downloading is enabled.

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
