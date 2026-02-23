"""Granicus Classic platform scraper.

Scrapes municipal meeting data from the older/classic Granicus system that
uses these URL patterns:
    - {subdomain}.granicus.com/ViewPublisher.php?view_id={id}
    - {subdomain}.granicus.com/GeneratedAgendaViewer.php?view_id={id}&clip_id={id}
    - {subdomain}.granicus.com/player/clip/{id}
    - {subdomain}.granicus.com/MinutesViewer.php?view_id={id}&clip_id={id}
    - {subdomain}.granicus.com/MetaViewer.php?view_id={id}&clip_id={id}&meta_id={id}

Strategy:
    1. ViewPublisher.php is the index page — lists all archived meetings for a
       body (view_id). We scrape this to discover clip_ids and event_ids.
    2. For each clip, we fetch GeneratedAgendaViewer.php to get structured
       agenda items with their document links (meta_ids) and video timestamps.
    3. The player page at /player/clip/{id} gives us the video URL and
       duration metadata.
    4. MinutesViewer gives us links to approved minutes documents.

All of this gets assembled into one Open Civic Agenda meeting record per event.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.granicus.discovery import (
    discover_views,
    extract_body_name,
    parse_view_publisher,
)
from scrapers.granicus.extraction import extract_date_from_text
from scrapers.granicus.media import scrape_agenda_json, scrape_media, scrape_minutes_link
from scrapers.granicus.merge import merge_agenda_sources
from scrapers.granicus.parsers import ALL_PARSERS, AgendaParser, ParseContext

logger = logging.getLogger(__name__)

# Default delay between HTTP requests to be polite
REQUEST_DELAY = 1.0


@dataclass
class ClipRef:
    """A reference to a single archived meeting clip found on ViewPublisher."""

    clip_id: int
    view_id: int
    event_id: int | None = None
    title: str = ""
    date: str = ""  # raw date string from the page
    video_url: str | None = None
    agenda_url: str | None = None
    minutes_url: str | None = None


@dataclass
class GranicusSite:
    """Configuration for a single Granicus site (municipality)."""

    subdomain: str
    jurisdiction_id: str
    jurisdiction_name: str
    jurisdiction_url: str | None = None
    views: dict[int, str] = field(default_factory=dict)
    """Mapping of view_id -> body name, e.g. {3: "City Council", 5: "Planning Commission"}"""

    parser_hints: list[str] = field(default_factory=list)
    """Optional: preferred parser names (e.g. ["css_classes"]) to try first."""

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.granicus.com"

    @classmethod
    def from_url(cls, url: str) -> "GranicusSite":
        """Create a GranicusSite from any Granicus URL.

        Accepts URLs like:
            https://erie.granicus.com/ViewPublisher.php?view_id=3
            https://erie.granicus.com/player/clip/1234?view_id=3
            https://erie.granicus.com/GeneratedAgendaViewer.php?view_id=3&clip_id=456
            https://erie.granicus.com
            erie.granicus.com
        """
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        parsed = urlparse(url)
        host = parsed.hostname or ""

        m = re.match(r"^(.+)\.granicus\.com$", host, re.I)
        if not m:
            raise ValueError(
                f"Not a Granicus URL: {url}  (expected {{subdomain}}.granicus.com)"
            )
        subdomain = m.group(1)

        params = parse_qs(parsed.query)
        views: dict[int, str] = {}
        if "view_id" in params:
            vid = int(params["view_id"][0])
            views[vid] = ""

        readable = subdomain.replace("-", " ").replace("_", " ").title()

        return cls(
            subdomain=subdomain,
            jurisdiction_id=f"ocd-jurisdiction/country:us/custom:{subdomain}/government",
            jurisdiction_name=readable,
            views=views,
        )


class GranicusScraper(BaseScraper):
    """Scraper for the classic Granicus meeting platform.

    Initialize with a GranicusSite config:

        site = GranicusSite(
            subdomain="erie",
            jurisdiction_id="ocd-jurisdiction/country:us/state:co/place:erie/government",
            jurisdiction_name="Town of Erie",
            views={3: "Board of Trustees"},
        )
        scraper = GranicusScraper(site)
        scraper.scrape_all(start_date="2025-01-01")
    """

    source_system = "granicus"

    def __init__(self, site: GranicusSite, output_dir="output", request_delay=REQUEST_DELAY,
                 download_config=None):
        self.site = site
        self.jurisdiction_id = site.jurisdiction_id
        self.jurisdiction_name = site.jurisdiction_name
        self.jurisdiction_url = site.jurisdiction_url
        self.request_delay = request_delay
        self._current_clip_id: int | None = None

        # Build ordered parser list: site hints first, then remaining defaults.
        self.parsers = self._build_parser_list(site.parser_hints)

        super().__init__(output_dir, download_config=download_config)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; OpenCivicAgenda/0.1; "
                "+https://github.com/opencivicagenda)"
            ),
        })

    def _build_parser_list(self, hints: list[str]) -> list[AgendaParser]:
        """Build parser list with hints prioritised, then remaining defaults."""
        if not hints:
            return list(ALL_PARSERS)

        # Map parser class names (lowercase) to instances
        name_map: dict[str, AgendaParser] = {}
        for p in ALL_PARSERS:
            # "CssClassParser" → "css_classes", "HeadingsParser" → "headings"
            cls_name = type(p).__name__
            # Convert CamelCase to snake_case, strip "Parser"
            key = cls_name.replace("Parser", "")
            key = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            name_map[key] = p
            # Also allow the class name itself
            name_map[cls_name.lower()] = p

        ordered: list[AgendaParser] = []
        used: set[str] = set()

        for hint in hints:
            hint_key = hint.lower().strip()
            if hint_key in name_map and hint_key not in used:
                ordered.append(name_map[hint_key])
                used.add(hint_key)

        # Append remaining parsers in default order
        for p in ALL_PARSERS:
            if p not in ordered:
                ordered.append(p)

        return ordered

    def output_filename(self, meeting: dict) -> str:
        """Include clip_id in filenames so meetings never collide."""
        org = meeting.get("organization", {})
        org_name = org.get("name", org) if isinstance(org, dict) else org
        slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-")

        date_str = meeting["start_date"][:10]
        clip_id = self._current_clip_id or "unknown"
        return f"{slug}_{date_str}_clip-{clip_id}.json"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET with polite delay and retry on transient failures."""
        time.sleep(self.request_delay)
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                if attempt == 2:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning("Request failed (%s), retrying in %ds: %s", e, wait, url)
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def _soup(self, url: str) -> BeautifulSoup:
        resp = self._get(url)
        return BeautifulSoup(resp.text, "html.parser")

    def _url(self, path: str, **params) -> str:
        """Build a full URL for this Granicus site."""
        base = f"{self.site.base_url}/{path}"
        if params:
            base += ("&" if "?" in path else "?") + urlencode(params)
        return base

    # ------------------------------------------------------------------
    # Phase 1: List meetings from ViewPublisher.php
    # ------------------------------------------------------------------

    def list_meetings(self, start_date=None, end_date=None) -> list[dict]:
        """Scrape ViewPublisher.php for each configured view to get clip listings."""
        # If no views configured, auto-discover
        if not self.site.views:
            logger.info("No views configured — discovering available views...")
            self.site.views = discover_views(
                self.session, self.site.base_url, self.request_delay
            )
            if not self.site.views:
                logger.warning("No views found for %s", self.site.subdomain)
                return []
            logger.info("Discovered %d view(s)", len(self.site.views))

        # Fill in empty body names
        for view_id, body_name in list(self.site.views.items()):
            if not body_name:
                url = self._url("ViewPublisher.php", view_id=view_id)
                try:
                    soup = self._soup(url)
                    discovered_name = extract_body_name(soup, view_id)
                    self.site.views[view_id] = discovered_name or f"View {view_id}"
                    logger.info("View %d: %s", view_id, self.site.views[view_id])
                except requests.RequestException:
                    self.site.views[view_id] = f"View {view_id}"

        all_refs = []
        for view_id, body_name in self.site.views.items():
            logger.info("Listing meetings for view %d (%s)", view_id, body_name)
            refs = parse_view_publisher(
                self._get, self._soup,
                self.site.base_url, view_id, body_name,
            )
            logger.info("  Found %d archived clips", len(refs))
            all_refs.extend(refs)

        # Filter by date range if provided
        filtered = []
        for ref in all_refs:
            parsed_date = self._parse_date_str(ref.date)
            if parsed_date is None:
                filtered.append(ref)
                continue
            date_str = parsed_date.strftime("%Y-%m-%d")
            if start_date and date_str < start_date:
                continue
            if end_date and date_str > end_date:
                continue
            filtered.append(ref)

        return [_clip_ref_to_dict(r) for r in filtered]

    # ------------------------------------------------------------------
    # Phase 2: Scrape a single meeting
    # ------------------------------------------------------------------

    def scrape_meeting(self, meeting_ref: dict) -> dict:
        """Scrape a single meeting and assemble it into schema format."""
        clip_id = meeting_ref["clip_id"]
        view_id = meeting_ref["view_id"]
        body_name = meeting_ref.get("title", "")
        raw_date = meeting_ref.get("date", "")
        event_id = meeting_ref.get("event_id")
        agenda_url = meeting_ref.get("agenda_url")
        minutes_url = meeting_ref.get("minutes_url")

        logger.info("Scraping clip %d (%s, %s)", clip_id, body_name, raw_date)

        parsed_date = self._parse_date_str(raw_date)
        start_date = parsed_date.isoformat() if parsed_date else None

        body_slug = re.sub(r"[^a-z0-9]+", "-", body_name.lower()).strip("-")
        self._current_clip_id = clip_id

        # --- Agenda ---
        actual_agenda_url = agenda_url or self._url(
            "AgendaViewer.php", view_id=view_id, clip_id=clip_id
        )
        agenda_items, agenda_docs, agenda_date = self._scrape_agenda(
            actual_agenda_url, view_id, clip_id
        )

        if not start_date and agenda_date:
            parsed_date = self._parse_date_str(agenda_date)
            if parsed_date:
                start_date = parsed_date.isoformat()
        if not start_date:
            start_date = raw_date or "unknown"

        # --- Media ---
        media, index_items = scrape_media(
            self._get, self._soup,
            self.site.base_url, clip_id, view_id,
        )

        # Merge player index points with agenda items
        if index_items:
            agenda_items = merge_agenda_sources(
                agenda_items, index_items, clip_id
            )

        # --- Minutes ---
        minutes_doc = None
        if minutes_url:
            minutes_doc = scrape_minutes_link(self._soup, minutes_url)

        # Assemble documents list
        documents = list(agenda_docs)
        if minutes_doc:
            documents.append(minutes_doc)

        # Build identifiers
        identifiers = [
            self.make_identifier(f"granicus:{self.site.subdomain}", str(clip_id)),
        ]
        if event_id:
            identifiers.append(
                self.make_identifier(f"granicus:{self.site.subdomain}:event", str(event_id))
            )

        meeting = {
            "id": self.make_event_id(),
            "identifiers": identifiers,
            "name": f"{body_name} Meeting",
            "organization": {
                "id": self.make_org_id(body_slug),
                "name": body_name,
                "identifiers": [
                    self.make_identifier(f"granicus:{self.site.subdomain}:view", str(view_id)),
                ],
            },
            "jurisdiction": self.jurisdiction_obj(),
            "start_date": start_date,
            "status": "completed",
            "agenda_items": agenda_items,
            "documents": documents,
            "media": media,
            "sources": [
                {
                    "url": self._url("ViewPublisher.php", view_id=view_id),
                    "note": f"Granicus archive for {body_name}",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
        }

        if agenda_url:
            meeting["sources"].append({
                "url": agenda_url,
                "note": "Granicus agenda viewer",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            })

        return meeting

    # ------------------------------------------------------------------
    # Phase 2a: Scrape agenda from GeneratedAgendaViewer.php
    # ------------------------------------------------------------------

    def _scrape_agenda(
        self, url: str, view_id: int, clip_id: int
    ) -> tuple[list[dict], list[dict], str | None]:
        """Fetch and parse agenda items for a meeting.

        Tries pluggable parsers in priority order, then merges with
        JSON.php video markers.

        Returns (agenda_items, meeting_level_documents, date_string).
        """
        from scrapers.granicus.extraction import extract_meeting_level_docs

        meeting_docs: list[dict] = []
        page_date: str | None = None

        # --- Step 1: Parse HTML agenda page ---
        html_items: list[dict] = []
        soup = None
        try:
            soup = self._soup(url)
        except requests.RequestException as e:
            logger.warning("Could not fetch agenda at %s: %s", url, e)

        if soup is not None:
            page_date = self._extract_date_from_page(soup)

            ctx = ParseContext(
                base_url=self.site.base_url,
                view_id=view_id,
                clip_id=clip_id,
            )

            # Try each parser in priority order
            for parser in self.parsers:
                if parser.can_parse(soup):
                    html_items = parser.parse(soup, ctx)
                    if html_items:
                        logger.info(
                            "  Parsed %d agenda items via %s",
                            len(html_items),
                            type(parser).__name__,
                        )
                        break

            meeting_docs = extract_meeting_level_docs(soup, self.site.base_url)

        # --- Step 2: Fetch JSON.php video-marker data ---
        marker_items = scrape_agenda_json(self._get, self.site.base_url, clip_id)
        if marker_items:
            logger.info(
                "  Parsed %d video markers from JSON.php", len(marker_items)
            )

        # --- Step 3: Merge both sources ---
        items = merge_agenda_sources(html_items, marker_items, clip_id)

        return items, meeting_docs, page_date

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_date_from_page(self, soup: BeautifulSoup) -> str | None:
        """Try to extract a date from the page title, headers, or body text."""
        title = soup.find("title")
        if title:
            d = extract_date_from_text(title.get_text())
            if d:
                return d

        for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
            d = extract_date_from_text(tag.get_text())
            if d:
                return d

        text = soup.get_text(" ", strip=True)[:2000]
        return extract_date_from_text(text) or None

    def _parse_date_str(self, s: str) -> datetime | None:
        """Try to parse various date formats seen on Granicus pages."""
        if not s:
            return None

        s = re.sub(r"\s+", " ", s.strip())
        s = re.sub(r"(\b\w{3})\.", r"\1", s)

        formats = [
            "%B %d, %Y",       # March 15, 2025
            "%b %d, %Y",       # Mar 15, 2025
            "%B %d %Y",        # March 15 2025
            "%b %d %Y",        # Mar 15 2025
            "%m/%d/%Y",        # 03/15/2025
            "%m/%d/%y",        # 03/15/25
            "%Y-%m-%d",        # 2025-03-15
        ]

        for fmt in formats:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None


def _clip_ref_to_dict(ref: ClipRef) -> dict:
    return {
        "clip_id": ref.clip_id,
        "view_id": ref.view_id,
        "event_id": ref.event_id,
        "title": ref.title,
        "date": ref.date,
        "video_url": ref.video_url,
        "agenda_url": ref.agenda_url,
        "minutes_url": ref.minutes_url,
    }
