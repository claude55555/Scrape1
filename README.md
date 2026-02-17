# Open Civic Agenda — Scrapers

Scrapers that collect municipal meeting data (agendas, minutes, votes) and output it in the [Open Civic Agenda](schema/v0.1/meeting.json) format.

## Repository layout

```
schema/v0.1/meeting.json        # The JSON Schema (v0.1)
examples/                       # Sample meeting files that validate against the schema
scrapers/
  base.py                       # Base class all scrapers inherit from
  example/scraper.py            # Annotated example — copy this to start a new scraper
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
