# Open Civic Agenda — Scrapers

Scrapers that collect municipal meeting data (agendas, minutes, votes) and output it in the [Open Civic Agenda](schema/v0.1/meeting.json) format.

## Repository layout

```
schema/v0.1/meeting.json        # The JSON Schema (v0.1)
examples/                       # Sample meeting files that validate against the schema
scrapers/
  base.py                       # Base class all scrapers inherit from
  example/scraper.py            # Annotated example — copy this to start a new scraper
  granicus/                     # Granicus Classic platform scraper
    scraper.py                  #   Core scraper (ViewPublisher, AgendaViewer, player)
    sites.py                    #   Known city configurations
    __main__.py                 #   CLI entry point
validate.py                     # CLI tool to validate output files
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

**Currently configured sites:** Erie CO, Simi Valley CA, Sacramento CA, Kirkland WA, Shoreline WA.
Add more in `scrapers/granicus/sites.py`.

**How it works:**
1. Scrapes `ViewPublisher.php` to discover all archived meeting clips for each body
2. For each clip, fetches `GeneratedAgendaViewer.php` to get structured agenda items,
   document links, and video timestamps
3. Fetches the `/player/clip/{id}` page to extract video stream URLs and duration
4. Checks `MinutesViewer.php` for approved minutes documents
5. Assembles everything into one Open Civic Agenda JSON file per meeting

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
| `documents` | Attached PDFs — agendas, minutes, staff reports |
| `media` | Video/audio recordings with duration |
| `result` | Vote outcomes with roll-call detail |
| `sources` | Provenance — where the data was scraped from |

See `examples/millfield-city-council-2025-03-15.json` for a fully populated example.
