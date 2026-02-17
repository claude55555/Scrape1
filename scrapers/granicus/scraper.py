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

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from scrapers.base import BaseScraper

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

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.granicus.com"


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

    def __init__(self, site: GranicusSite, output_dir="output", request_delay=REQUEST_DELAY):
        self.site = site
        self.jurisdiction_id = site.jurisdiction_id
        self.jurisdiction_name = site.jurisdiction_name
        self.jurisdiction_url = site.jurisdiction_url
        self.request_delay = request_delay

        super().__init__(output_dir)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; OpenCivicAgenda/0.1; "
                "+https://github.com/opencivicagenda)"
            ),
        })

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
        """Scrape ViewPublisher.php for each configured view to get clip listings.

        Returns a list of dicts (serialized ClipRefs) for scrape_meeting().
        """
        all_refs = []
        for view_id, body_name in self.site.views.items():
            logger.info("Listing meetings for view %d (%s)", view_id, body_name)
            refs = self._parse_view_publisher(view_id, body_name)
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

        # Return as dicts for the base class interface
        return [self._clip_ref_to_dict(r) for r in filtered]

    def _parse_view_publisher(self, view_id: int, body_name: str) -> list[ClipRef]:
        """Parse the ViewPublisher.php page for a single view.

        The page has two sections: "Upcoming Events" and past/archived events.
        We only care about archived events that have clip recordings.

        The archive table structure is typically:
            <table>
              <tr>  (header row)
              <tr>
                <td>Date</td>
                <td>Title/Duration</td>
                <td><a href="player/clip/123?view_id=5">Video</a></td>
                <td><a href="GeneratedAgendaViewer.php?...">Agenda</a></td>
                <td><a href="MinutesViewer.php?...">Minutes</a></td>
              </tr>
              ...
            </table>
        """
        url = self._url("ViewPublisher.php", view_id=view_id)
        soup = self._soup(url)
        refs = []

        # Find all links to the player — each one represents an archived clip
        # Pattern: /player/clip/{clip_id} or MediaPlayer.php?clip_id={id}
        player_links = soup.find_all("a", href=re.compile(r"(player/clip/\d+|MediaPlayer\.php\?.*clip_id=\d+)"))

        for link in player_links:
            clip_id = self._extract_clip_id(link["href"])
            if clip_id is None:
                continue

            ref = ClipRef(
                clip_id=clip_id,
                view_id=view_id,
                title=body_name,
                video_url=urljoin(self.site.base_url + "/", link["href"]),
            )

            # Walk up to the containing row to find sibling data
            row = link.find_parent("tr")
            if row:
                self._extract_row_data(row, ref, view_id)

            refs.append(ref)

        # Fallback: some ViewPublisher pages use divs instead of tables.
        # Look for clip references in any remaining anchor tags.
        if not refs:
            refs = self._parse_view_publisher_divs(soup, view_id, body_name)

        return refs

    def _parse_view_publisher_divs(self, soup: BeautifulSoup, view_id: int, body_name: str) -> list[ClipRef]:
        """Fallback parser for ViewPublisher pages that use div-based layouts."""
        refs = []
        # Look for any anchors with clip references we haven't caught
        for link in soup.find_all("a", href=True):
            href = link["href"]
            clip_id = self._extract_clip_id(href)
            if clip_id is None:
                continue

            # Avoid duplicates
            if any(r.clip_id == clip_id for r in refs):
                continue

            ref = ClipRef(
                clip_id=clip_id,
                view_id=view_id,
                title=body_name,
                video_url=urljoin(self.site.base_url + "/", href),
            )

            # Try to find a date near this link
            parent = link.find_parent(["div", "li", "section"])
            if parent:
                date_match = re.search(
                    r"(\w+ \d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
                    parent.get_text(),
                )
                if date_match:
                    ref.date = date_match.group(1).strip()

            # Look for sibling agenda/minutes links in the same container
            if parent:
                self._find_sibling_links(parent, ref, view_id)

            refs.append(ref)

        return refs

    def _extract_row_data(self, row: Tag, ref: ClipRef, view_id: int):
        """Extract date, agenda link, and minutes link from a table row."""
        cells = row.find_all("td")
        if not cells:
            return

        # Date is typically in the first cell
        date_text = cells[0].get_text(strip=True)
        date_match = re.search(
            r"(\w+ \d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
            date_text,
        )
        if date_match:
            ref.date = date_match.group(1).strip()

        self._find_sibling_links(row, ref, view_id)

    def _find_sibling_links(self, container: Tag, ref: ClipRef, view_id: int):
        """Find agenda and minutes links within a container element."""
        for link in container.find_all("a", href=True):
            href = link["href"]
            if "GeneratedAgendaViewer" in href or "AgendaViewer" in href:
                ref.agenda_url = urljoin(self.site.base_url + "/", href)
                # Try to extract event_id from agenda URL
                event_id = self._extract_param(href, "event_id")
                if event_id:
                    ref.event_id = int(event_id)
            elif "MinutesViewer" in href:
                ref.minutes_url = urljoin(self.site.base_url + "/", href)

    # ------------------------------------------------------------------
    # Phase 2: Scrape a single meeting
    # ------------------------------------------------------------------

    def scrape_meeting(self, meeting_ref: dict) -> dict:
        """Scrape a single meeting and assemble it into schema format.

        Correlates data from:
        - The agenda page (items, documents, structure)
        - The player page (video URL, duration)
        - The minutes page (approved minutes document)
        """
        clip_id = meeting_ref["clip_id"]
        view_id = meeting_ref["view_id"]
        body_name = meeting_ref.get("title", "")
        raw_date = meeting_ref.get("date", "")
        event_id = meeting_ref.get("event_id")
        agenda_url = meeting_ref.get("agenda_url")
        minutes_url = meeting_ref.get("minutes_url")

        logger.info("Scraping clip %d (%s, %s)", clip_id, body_name, raw_date)

        # Parse the date
        parsed_date = self._parse_date_str(raw_date)
        if parsed_date:
            start_date = parsed_date.isoformat()
        else:
            start_date = raw_date or "unknown"

        # Build the body slug and org ID
        body_slug = re.sub(r"[^a-z0-9]+", "-", body_name.lower()).strip("-")

        # Scrape agenda items
        agenda_items = []
        agenda_docs = []
        if agenda_url:
            agenda_items, agenda_docs = self._scrape_agenda(agenda_url, view_id, clip_id)
        else:
            # Try constructing the URL ourselves
            constructed_url = self._url(
                "GeneratedAgendaViewer.php", view_id=view_id, clip_id=clip_id
            )
            agenda_items, agenda_docs = self._scrape_agenda(constructed_url, view_id, clip_id)

        # Scrape video metadata from the player page
        media = self._scrape_media(clip_id, view_id)

        # Scrape minutes if available
        minutes_doc = None
        if minutes_url:
            minutes_doc = self._scrape_minutes_link(minutes_url)

        # Assemble documents list
        documents = []
        documents.extend(agenda_docs)
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

        # Assemble the meeting record
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
    ) -> tuple[list[dict], list[dict]]:
        """Parse a GeneratedAgendaViewer page.

        Returns (agenda_items, meeting_level_documents).

        The agenda viewer page typically has:
        - A header with meeting title, date, time, location
        - A structured list of agenda items, potentially nested
        - Each item may link to:
          - A video timestamp (meta_id parameter in player links)
          - Attached documents via MetaViewer.php links
        """
        try:
            soup = self._soup(url)
        except requests.RequestException as e:
            logger.warning("Could not fetch agenda at %s: %s", url, e)
            return [], []

        items = []
        meeting_docs = []

        # Look for the agenda content — usually in a div or table structure
        # Common patterns:
        #   <div class="generated-agenda"> or <div id="generated-agenda-viewer">
        #   <table class="agenda-table">
        #   <ol> / <ul> with agenda entries

        # Strategy: find all elements that look like agenda items.
        # Granicus uses numbered/lettered entries with links to documents and video.

        # Try structured div-based agenda first
        agenda_container = (
            soup.find("div", class_=re.compile(r"agenda", re.I))
            or soup.find("div", id=re.compile(r"agenda", re.I))
            or soup.find("div", id="recorddetail_content")
            or soup.find("div", class_="recorddetail")
        )

        if agenda_container:
            items = self._parse_agenda_container(agenda_container, view_id, clip_id)
        else:
            # Fallback: try to parse the whole body for agenda-like structures
            items = self._parse_agenda_flat(soup, view_id, clip_id)

        # Look for a meeting-level agenda PDF link
        for link in soup.find_all("a", href=re.compile(r"MetaViewer\.php")):
            text = link.get_text(strip=True).lower()
            if "full agenda" in text or "agenda packet" in text or "meeting packet" in text:
                meeting_docs.append({
                    "title": link.get_text(strip=True),
                    "url": urljoin(self.site.base_url + "/", link["href"]),
                    "type": "agenda" if "agenda" in text else "packet",
                })

        return items, meeting_docs

    def _parse_agenda_container(
        self, container: Tag, view_id: int, clip_id: int
    ) -> list[dict]:
        """Parse agenda items from a container div.

        Granicus GeneratedAgendaViewer typically renders items as a series of
        divs or table rows. Items at different nesting depths are differentiated
        by CSS classes (e.g., level-0, level-1) or by indentation.

        Common structure:
            <div class="agenda-item level-0">
                <span class="item-number">1.</span>
                <span class="item-title">Call to Order</span>
                <a href="player/clip/123?meta_id=456">Video</a>
            </div>
            <div class="agenda-item level-1">
                <span class="item-number">1.a</span>
                ...
            </div>
        """
        items = []

        # Look for individual agenda item divs
        item_divs = container.find_all(
            ["div", "tr", "li"],
            class_=re.compile(r"(agenda.?item|row|item|entry)", re.I),
        )

        if item_divs:
            for div in item_divs:
                item = self._parse_agenda_item_element(div, view_id, clip_id)
                if item:
                    items.append(item)
        else:
            # Fallback: look for any paragraphs or divs with content
            items = self._parse_agenda_flat(container, view_id, clip_id)

        # Try to nest items based on order numbering (e.g., "3" is parent of "3.a")
        items = self._nest_agenda_items(items)

        return items

    def _parse_agenda_flat(
        self, container: Tag, view_id: int, clip_id: int
    ) -> list[dict]:
        """Flat parse: extract agenda items from any text structure.

        Looks for numbered/lettered lines and associated links.
        """
        items = []
        seen_titles = set()

        # Find elements that have both text content and potentially links
        for el in container.find_all(["p", "div", "tr", "li", "span"]):
            text = el.get_text(strip=True)
            if not text or len(text) < 3:
                continue

            # Skip if this is a child of something we already processed
            if text in seen_titles:
                continue

            # Look for numbered/lettered patterns at the start
            order_match = re.match(
                r"^(\d+[\.\)]?\s*[a-z]?[\.\)]?|[A-Z][\.\)]|[IVXLC]+[\.\)])\s+(.+)",
                text,
            )

            if order_match:
                order = order_match.group(1).rstrip(".)")
                title = order_match.group(2).strip()
            else:
                # Check if this looks like a header/section
                if el.name in ("b", "strong", "h1", "h2", "h3", "h4"):
                    title = text
                    order = None
                else:
                    continue

            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            item = {"title": title}
            if order:
                item["order"] = order

            # Classify based on keywords
            item["classification"] = self._classify_item(title)

            # Look for document links
            docs = self._extract_documents(el, view_id, clip_id)
            if docs:
                item["documents"] = docs

            # Look for video timestamp
            media_ref = self._extract_media_ref(el, clip_id)
            if media_ref:
                item["media_ref"] = media_ref

            items.append(item)

        return items

    def _parse_agenda_item_element(
        self, el: Tag, view_id: int, clip_id: int
    ) -> dict | None:
        """Parse a single agenda item element."""
        text = el.get_text(strip=True)
        if not text or len(text) < 3:
            return None

        # Try to find order number
        order = None
        title = text

        # Look for a specific item-number span
        num_span = el.find(class_=re.compile(r"(number|order|index)", re.I))
        if num_span:
            order = num_span.get_text(strip=True).rstrip(".)")
            # Title is the rest
            title_span = el.find(class_=re.compile(r"(title|name|text|description)", re.I))
            if title_span:
                title = title_span.get_text(strip=True)
            else:
                title = text.replace(num_span.get_text(), "", 1).strip()
        else:
            # Try regex on the full text
            m = re.match(r"^([\d]+[a-z]?[\.\)]?)\s+(.+)", text)
            if m:
                order = m.group(1).rstrip(".)")
                title = m.group(2).strip()

        if not title:
            return None

        # Detect nesting level from CSS class
        level = 0
        classes = " ".join(el.get("class", []))
        level_match = re.search(r"level[_-]?(\d+)", classes)
        if level_match:
            level = int(level_match.group(1))

        item = {"title": title, "_level": level}
        if order:
            item["order"] = order

        item["classification"] = self._classify_item(title)

        # Extract documents
        docs = self._extract_documents(el, view_id, clip_id)
        if docs:
            item["documents"] = docs

        # Extract video timestamp
        media_ref = self._extract_media_ref(el, clip_id)
        if media_ref:
            item["media_ref"] = media_ref

        return item

    def _nest_agenda_items(self, items: list[dict]) -> list[dict]:
        """Convert a flat list with _level annotations into a nested structure.

        Items with _level > 0 become children of the nearest preceding item
        with a lower _level. Also handles dotted numbering (e.g., "3.a" nests
        under "3").
        """
        if not items:
            return []

        # Clean up: use _level if present, otherwise infer from order
        for item in items:
            if "_level" not in item:
                order = item.get("order", "")
                if isinstance(order, str):
                    # Count dots/letters to infer depth: "3" = 0, "3.a" = 1, "3.a.i" = 2
                    parts = re.split(r"[.\s]+", order)
                    item["_level"] = max(0, len(parts) - 1)
                else:
                    item["_level"] = 0

        # Build the tree
        root = []
        stack: list[tuple[int, list[dict]]] = [(-1, root)]

        for item in items:
            level = item.pop("_level", 0)

            # Pop stack until we find the right parent
            while stack and stack[-1][0] >= level:
                stack.pop()

            parent_list = stack[-1][1] if stack else root
            parent_list.append(item)

            # This item can be a parent for deeper items
            item.setdefault("items", [])
            stack.append((level, item["items"]))

        # Clean up empty items arrays
        self._cleanup_empty_items(root)
        return root

    def _cleanup_empty_items(self, items: list[dict]):
        for item in items:
            if "items" in item:
                if item["items"]:
                    self._cleanup_empty_items(item["items"])
                else:
                    del item["items"]

    # ------------------------------------------------------------------
    # Phase 2b: Scrape video/media from the player page
    # ------------------------------------------------------------------

    def _scrape_media(self, clip_id: int, view_id: int) -> list[dict]:
        """Scrape video metadata from the player page.

        The player page at /player/clip/{id} is an HTML5 video player.
        The actual video stream URL is typically an HLS (m3u8) URL served
        from archive-stream.granicus.com. We look for it in:
        - <video> or <source> tags
        - JavaScript variables / JSON config in <script> blocks
        - og:video meta tags
        """
        url = self._url(f"player/clip/{clip_id}", view_id=view_id)
        try:
            soup = self._soup(url)
        except requests.RequestException as e:
            logger.warning("Could not fetch player page for clip %d: %s", clip_id, e)
            return []

        video_url = None
        duration = None

        # Method 1: Look for <source> tags
        for source in soup.find_all("source"):
            src = source.get("src", "")
            if src and ("m3u8" in src or "mp4" in src):
                video_url = src
                break

        # Method 2: Look for <video> tag
        if not video_url:
            video_tag = soup.find("video")
            if video_tag and video_tag.get("src"):
                video_url = video_tag["src"]

        # Method 3: Look for og:video meta tag
        if not video_url:
            og_video = soup.find("meta", property="og:video")
            if og_video:
                video_url = og_video.get("content", "")

        # Method 4: Search script blocks for stream URLs
        if not video_url:
            for script in soup.find_all("script"):
                text = script.string or ""
                # Look for m3u8 URLs
                m3u8_match = re.search(r'"(https?://[^"]+\.m3u8[^"]*)"', text)
                if m3u8_match:
                    video_url = m3u8_match.group(1)
                    break
                # Look for mp4 URLs
                mp4_match = re.search(r'"(https?://[^"]+\.mp4[^"]*)"', text)
                if mp4_match:
                    video_url = mp4_match.group(1)
                    break

        # Try to find duration
        for script in soup.find_all("script"):
            text = script.string or ""
            dur_match = re.search(r"duration[\"']?\s*[:=]\s*(\d+)", text, re.I)
            if dur_match:
                duration = int(dur_match.group(1))
                break

        # Also check for og:video:duration
        if not duration:
            og_dur = soup.find("meta", property="og:video:duration")
            if og_dur:
                try:
                    duration = int(og_dur["content"])
                except (ValueError, KeyError):
                    pass

        # Build the media entry — always include the player page URL as fallback
        player_url = f"{self.site.base_url}/player/clip/{clip_id}?view_id={view_id}"
        media_entry = {
            "id": f"clip-{clip_id}",
            "url": video_url or player_url,
            "type": "video",
            "label": "Granicus recording",
        }

        # If we found a stream URL, also include the player page as a secondary ref
        if video_url:
            media_entry["media_type"] = (
                "application/x-mpegURL" if "m3u8" in video_url else "video/mp4"
            )

        if duration:
            media_entry["duration_seconds"] = duration

        return [media_entry]

    # ------------------------------------------------------------------
    # Phase 2c: Scrape minutes
    # ------------------------------------------------------------------

    def _scrape_minutes_link(self, url: str) -> dict | None:
        """Extract a minutes document reference from MinutesViewer.

        MinutesViewer.php renders or links to the approved minutes.
        We don't parse the full minutes content — just get the document link.
        """
        try:
            soup = self._soup(url)
        except requests.RequestException:
            return None

        # Look for a PDF link or the main content
        pdf_link = soup.find("a", href=re.compile(r"\.(pdf|PDF)"))
        if pdf_link:
            return {
                "title": "Approved Minutes",
                "url": urljoin(url, pdf_link["href"]),
                "media_type": "application/pdf",
                "type": "minutes",
            }

        # Fallback: the MinutesViewer URL itself serves as the document reference
        return {
            "title": "Approved Minutes",
            "url": url,
            "media_type": "text/html",
            "type": "minutes",
        }

    # ------------------------------------------------------------------
    # Helpers: document & media extraction from agenda items
    # ------------------------------------------------------------------

    def _extract_documents(self, el: Tag, view_id: int, clip_id: int) -> list[dict]:
        """Find document links (MetaViewer.php) within an element."""
        docs = []
        for link in el.find_all("a", href=re.compile(r"MetaViewer\.php")):
            href = link["href"]
            full_url = urljoin(self.site.base_url + "/", href)
            text = link.get_text(strip=True) or "Attachment"

            # Guess type from the link text
            text_lower = text.lower()
            if "staff report" in text_lower:
                doc_type = "staff_report"
            elif "ordinance" in text_lower:
                doc_type = "ordinance"
            elif "resolution" in text_lower:
                doc_type = "resolution"
            elif "agenda" in text_lower:
                doc_type = "agenda"
            elif "minutes" in text_lower:
                doc_type = "minutes"
            else:
                doc_type = "attachment"

            docs.append({
                "title": text,
                "url": full_url,
                "type": doc_type,
            })

        return docs

    def _extract_media_ref(self, el: Tag, clip_id: int) -> dict | None:
        """Find a video timestamp link within an element.

        Granicus agenda items often link to the player with a meta_id or
        timestamp parameter that deep-links into the video.
        """
        for link in el.find_all("a", href=re.compile(r"(player/clip|MediaPlayer)")):
            href = link["href"]

            # Look for entrytime or offset parameter
            offset = (
                self._extract_param(href, "entrytime")
                or self._extract_param(href, "starttime")
                or self._extract_param(href, "offset")
            )
            if offset:
                try:
                    return {
                        "media_id": f"clip-{clip_id}",
                        "offset_seconds": int(float(offset)),
                    }
                except ValueError:
                    pass

            # Some Granicus implementations use meta_id for timestamp markers
            meta_id = self._extract_param(href, "meta_id")
            if meta_id:
                return {
                    "media_id": f"clip-{clip_id}",
                }

        return None

    # ------------------------------------------------------------------
    # Helpers: classification, date parsing, URL parsing
    # ------------------------------------------------------------------

    def _classify_item(self, title: str) -> str | None:
        """Guess the classification of an agenda item from its title."""
        t = title.lower()
        patterns = {
            "call_to_order": r"call to order",
            "roll_call": r"roll\s*call",
            "adjournment": r"adjourn",
            "public_comment": r"public\s+(comment|input|forum|testimony)",
            "public_hearing": r"public\s+hearing",
            "consent": r"consent\s+(calendar|agenda|item)",
            "closed_session": r"closed\s+session|executive\s+session",
            "presentation": r"presentation|proclamation|recognition|ceremonial",
            "action": r"(ordinance|resolution)\s+\d",
        }
        for classification, pattern in patterns.items():
            if re.search(pattern, t, re.I):
                return classification
        return None

    def _parse_date_str(self, s: str) -> datetime | None:
        """Try to parse various date formats seen on Granicus pages."""
        if not s:
            return None

        formats = [
            "%B %d, %Y",       # March 15, 2025
            "%b %d, %Y",       # Mar 15, 2025
            "%m/%d/%Y",        # 03/15/2025
            "%m/%d/%y",        # 03/15/25
            "%Y-%m-%d",        # 2025-03-15
        ]
        # Clean up whitespace
        s = re.sub(r"\s+", " ", s.strip())

        for fmt in formats:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_clip_id(href: str) -> int | None:
        """Extract clip_id from a URL (path or query parameter)."""
        # Path form: /player/clip/123
        m = re.search(r"player/clip/(\d+)", href)
        if m:
            return int(m.group(1))

        # Query param form: clip_id=123
        m = re.search(r"clip_id=(\d+)", href)
        if m:
            return int(m.group(1))

        return None

    @staticmethod
    def _extract_param(href: str, param: str) -> str | None:
        """Extract a query parameter value from a URL."""
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        values = params.get(param)
        return values[0] if values else None

    @staticmethod
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
