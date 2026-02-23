# Open Civic Agenda — Scrapers

Scrapers that collect municipal meeting data (agendas, minutes, votes) and output it in the [Open Civic Agenda](schema/v0.1/meeting.json) format.

## Repository layout

```
schema/v0.1/meeting.json        # The JSON Schema (v0.1)
examples/                       # Sample meeting files that validate against the schema
scrapers/
  base.py                       # Base class all scrapers inherit from
  download.py                   # File download with extension-based filtering
  example/scraper.py            # Annotated example — copy this to start a new scraper
  granicus/                     # Granicus Classic platform scraper
    scraper.py                  #   Orchestrator (list meetings → scrape each → output)
    extraction.py               #   Shared helpers (doc/body/timestamp/classification)
    discovery.py                #   ViewPublisher/RSS meeting listing
    media.py                    #   Video, captions, index-point extraction
    merge.py                    #   HTML + JSON agenda merging
    sites.py                    #   Known city configurations
    __main__.py                 #   CLI entry point
    ARCHITECTURE.md             #   Developer guide — how to add a new city
    parsers/                    #   Pluggable agenda page parsers
      base.py                   #     AgendaParser ABC + ParseContext
      css_classes.py            #     Sacramento-style (Agenda0/1/2 CSS classes)
      headings.py               #     Shoreline-style (h2/h3 headings)
      bold.py                   #     Generic bold-text fallback
      table.py                  #     Generic table-row fallback
      flat.py                   #     Last-resort numbered-line fallback
validate.py                     # CLI tool to validate output files
fixtures/                       # Saved HTML pages for offline testing
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Validate the example file
python validate.py examples/

# Run the example scraper
python -m scrapers.example.scraper
```

## Writing a new scraper

1. Create a directory under `scrapers/` for your jurisdiction (e.g. `scrapers/denver/`).
2. Copy `scrapers/example/scraper.py` into it as a starting point.
3. Implement `list_meetings()` and `scrape_meeting()`.
4. The base class handles ID generation, validation, and writing output.

```python
from scrapers.base import BaseScraper

class DenverScraper(BaseScraper):
    jurisdiction_id = "ocd-jurisdiction/country:us/state:co/place:denver/government"
    jurisdiction_name = "City and County of Denver"
    source_system = "legistar"

    def list_meetings(self, start_date=None, end_date=None):
        # Hit the Legistar API, scrape an index page, etc.
        ...

    def scrape_meeting(self, meeting_ref):
        # Fetch detail page and map to schema fields
        ...
```

Key things every scraper should populate:
- **`id`** — use `self.make_event_id()` to generate an OCD event UUID
- **`organization`** — structured form with `id`, `name`, `classification`
- **`jurisdiction`** — use `self.jurisdiction_obj()` helper
- **`start_date`** — ISO 8601 with timezone
- **`agenda_items`** — the ordered list with `title`, `order`, and `classification`
- **`sources`** — where the data was scraped from, with `retrieved_at` timestamp

## Granicus Classic scraper

The first real scraper targets the classic Granicus platform used by many
municipalities (`{city}.granicus.com` with `ViewPublisher.php`, `GeneratedAgendaViewer.php`,
`/player/clip/{id}`).

```bash
# List known sites
python -m scrapers.granicus --list-sites

# Scrape a specific city
python -m scrapers.granicus erie --start-date 2025-01-01

# Scrape a custom Granicus site by subdomain
python -m scrapers.granicus --subdomain burbank --view-id 1 --body "City Council"

# Scrape all known sites
python -m scrapers.granicus --all --start-date 2025-06-01 -v
```

### Downloading files

By default the scraper only records URLs. Add `--download` to fetch
documents, captions, and transcripts to disk:

```bash
# Download with sensible defaults (PDFs, VTTs, SRTs — but not large video files)
python -m scrapers.granicus erie --start-date 2025-01-01 --download

# Only download specific types
python -m scrapers.granicus erie --download --download-include .pdf --download-include .vtt

# Download everything except video
python -m scrapers.granicus erie --download --download-exclude .mp4 --download-exclude .m3u8
```

Downloaded files are saved under `files/` next to the JSON output, and each
meeting record gets `local_path` fields pointing to the downloaded copies:

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

In the JSON, `local_path` appears alongside the URL on documents, media, and captions:

```json
{
  "title": "Meeting Agenda",
  "url": "https://erie.granicus.com/MetaViewer.php?meta_id=12345",
  "media_type": "application/pdf",
  "type": "agenda",
  "local_path": "files/clip-6627/agenda.pdf"
}
```

#### Programmatic usage

Pass a `DownloadConfig` when constructing any scraper:

```python
from scrapers.download import DownloadConfig
from scrapers.granicus.scraper import GranicusScraper, GranicusSite

site = GranicusSite(
    subdomain="erie",
    jurisdiction_id="ocd-jurisdiction/country:us/state:co/place:erie/government",
    jurisdiction_name="Town of Erie",
    views={3: "Board of Trustees"},
)

# Download only PDFs and caption files
dl = DownloadConfig(
    enabled=True,
    include_types={".pdf", ".vtt", ".srt"},
)

scraper = GranicusScraper(site, output_dir="output/erie", download_config=dl)
scraper.scrape_all(start_date="2025-01-01")
```

`DownloadConfig` options:

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Master switch — nothing is downloaded unless this is `True` |
| `base_dir` | `Path \| None` | `None` | Override where files are saved (defaults to `{output_dir}/files/`) |
| `include_types` | `set[str]` | `{}` | If non-empty, **only** download these extensions |
| `exclude_types` | `set[str]` | `{}` | If non-empty, **skip** these extensions |

When neither `include_types` nor `exclude_types` is set, built-in defaults apply:
- **Included:** `.pdf`, `.vtt`, `.srt`, `.smi`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.txt`, `.rtf`, `.csv`
- **Excluded:** `.mp4`, `.m3u8`, `.mp3`, `.wav`, `.m4a`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`

`include_types` takes precedence if both are set.

**Currently configured sites:** Erie CO, Simi Valley CA, Sacramento CA, Kirkland WA, Shoreline WA.
Add more in `scrapers/granicus/sites.py`.

**How it works:**
1. **Discovery** (`discovery.py`): Scrapes `ViewPublisher.php` / RSS to list all archived clips
2. **Agenda parsing** (`parsers/`): Fetches `GeneratedAgendaViewer.php` and auto-detects the
   correct parser for the page's HTML structure (CSS classes, headings, bold text, etc.)
3. **Video markers** (`media.py`): Fetches `JSON.php` for timestamped agenda markers
4. **Merge** (`merge.py`): Combines HTML agenda items with video markers via fuzzy title matching
5. **Media** (`media.py`): Fetches `/player/clip/{id}` for video URL, duration, captions
6. **Minutes** (`media.py`): Checks `MinutesViewer.php` for approved minutes documents
7. **Assembly** (`scraper.py`): Combines everything into one Open Civic Agenda JSON per meeting

**Adding a new city:**
If its Granicus HTML matches an existing pattern, just add a `GranicusSite` to `sites.py`.
If it has a new HTML layout, add a small parser class (~80 lines) in `parsers/`.
See [`scrapers/granicus/ARCHITECTURE.md`](scrapers/granicus/ARCHITECTURE.md) for the full guide.

## Validating output

```bash
# Single file
python validate.py output/denver-city-council_2025-03-15.json

# All files in a directory
python validate.py output/
```

## Schema overview

The schema (`schema/v0.1/meeting.json`) captures:

| Field | Description |
|---|---|
| `organization` | The body holding the meeting (council, commission, board) |
| `jurisdiction` | The governing municipality |
| `agenda_items` | Ordered, nestable agenda entries with classifications |
| `participants` | Attendees with roles and attendance |
| `documents` | Attached PDFs — agendas, minutes, staff reports (with optional `local_path`) |
| `media` | Video/audio recordings with duration and `captions` object (with optional `local_path`) |
| `result` | Vote outcomes with roll-call detail |
| `sources` | Provenance — where the data was scraped from |

See `examples/millfield-city-council-2025-03-15.json` for a fully populated example.
