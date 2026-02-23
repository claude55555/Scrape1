"""Base scraper class for Open Civic Agenda meeting scrapers."""

import json
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import requests

from scrapers.download import DownloadConfig, download_meeting_files

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Base class all meeting scrapers should inherit from.

    Subclasses must implement:
        - jurisdiction_id: OCD jurisdiction path
        - jurisdiction_name: Human-readable jurisdiction name
        - list_meetings(): discover meetings to scrape
        - scrape_meeting(meeting_ref): scrape a single meeting into schema format

    The base class provides helpers for ID generation, output writing,
    and schema validation.
    """

    jurisdiction_id: str  # e.g. "ocd-jurisdiction/country:us/state:co/place:millfield/government"
    jurisdiction_name: str  # e.g. "City of Millfield"
    jurisdiction_url: str | None = None
    source_system: str = ""  # e.g. "legistar", "granicus", "primegov"

    def __init__(self, output_dir: str | Path = "output", download_config: DownloadConfig | None = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_config = download_config
        self._schema = None

    # --- ID helpers ---

    def make_event_id(self) -> str:
        return f"ocd-event/{uuid.uuid4()}"

    def make_org_id(self, body_slug: str) -> str:
        return f"{self.jurisdiction_id}/body/{body_slug}"

    def make_identifier(self, scheme: str, identifier: str) -> dict:
        return {"scheme": scheme, "identifier": str(identifier)}

    # --- Output helpers ---

    def output_filename(self, meeting: dict) -> str:
        """Generate a filename from meeting data.

        Format: {org-slug}_{date}.json
        """
        org = meeting.get("organization", {})
        org_name = org.get("name", org) if isinstance(org, dict) else org
        slug = org_name.lower().replace(" ", "-")
        # Keep only alphanumeric and hyphens
        slug = "".join(c for c in slug if c.isalnum() or c == "-")

        date_str = meeting["start_date"][:10]
        return f"{slug}_{date_str}.json"

    def save_meeting(self, meeting: dict) -> Path:
        """Validate, optionally download files, and write a meeting record."""
        meeting.setdefault("schema_version", "0.1")
        meeting.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

        errors = self.validate(meeting)
        if errors:
            logger.warning("Validation errors for %s: %s", meeting.get("id"), errors)

        # Download referenced files if configured
        if self.download_config and self.download_config.enabled:
            session = getattr(self, "session", None) or requests.Session()
            meeting = download_meeting_files(
                meeting,
                config=self.download_config,
                session=session,
                json_dir=self.output_dir,
            )

        filename = self.output_filename(meeting)
        path = self.output_dir / filename
        path.write_text(json.dumps(meeting, indent=2, ensure_ascii=False) + "\n")
        logger.info("Wrote %s", path)
        return path

    # --- Validation ---

    def validate(self, meeting: dict) -> list[str]:
        """Validate a meeting dict against the schema. Returns a list of error messages."""
        try:
            import jsonschema
        except ImportError:
            logger.debug("jsonschema not installed, skipping validation")
            return []

        if self._schema is None:
            schema_path = Path(__file__).resolve().parent.parent / "schema" / "v0.1" / "meeting.json"
            self._schema = json.loads(schema_path.read_text())

        validator = jsonschema.Draft202012Validator(self._schema)
        return [e.message for e in validator.iter_errors(meeting)]

    # --- Jurisdiction helper ---

    def jurisdiction_obj(self, **extra) -> dict:
        """Build the jurisdiction block with optional extra fields."""
        obj = {"id": self.jurisdiction_id, "name": self.jurisdiction_name}
        if self.jurisdiction_url:
            obj["url"] = self.jurisdiction_url
        obj.update(extra)
        return obj

    # --- Abstract interface ---

    @abstractmethod
    def list_meetings(self, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
        """Return a list of meeting references to scrape.

        Each item is a dict with at least enough info to pass to scrape_meeting().
        The exact shape is up to each scraper — e.g. a Legistar scraper might
        return dicts with {"event_id": 12345, "date": "2025-03-15"}.
        """

    @abstractmethod
    def scrape_meeting(self, meeting_ref: dict) -> dict:
        """Scrape a single meeting and return a dict conforming to the schema."""

    def scrape_all(self, start_date: str | None = None, end_date: str | None = None) -> list[Path]:
        """Discover and scrape all meetings in a date range."""
        refs = self.list_meetings(start_date=start_date, end_date=end_date)
        logger.info("Found %d meetings to scrape", len(refs))

        paths = []
        for ref in refs:
            try:
                meeting = self.scrape_meeting(ref)
                path = self.save_meeting(meeting)
                paths.append(path)
            except Exception:
                logger.exception("Failed to scrape meeting: %s", ref)
        return paths
