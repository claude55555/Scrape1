"""Meeting discovery from Granicus ViewPublisher / RSS feeds.

Handles Phase 1 of scraping: discovering which meetings exist for
each configured view_id, extracting clip_ids, dates, and agenda/minutes URLs.
"""

import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from scrapers.granicus.extraction import (
    _DATE_RE,
    extract_clip_id,
    extract_date_from_text,
    extract_param,
)

logger = logging.getLogger(__name__)


def discover_views(session, base_url: str, request_delay: float) -> dict[int, str]:
    """Auto-discover available views by probing ViewPublisher.php.

    Tries view_id 1..30 and returns {view_id: body_name} for valid ones.
    """
    import time

    discovered = {}
    for vid in range(1, 31):
        url = f"{base_url}/ViewPublisher.php?view_id={vid}"
        try:
            time.sleep(request_delay)
            resp = session.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            name = extract_body_name(soup, vid)
            if name:
                discovered[vid] = name
                logger.info("  Discovered view %d: %s", vid, name)
        except requests.RequestException:
            continue
    return discovered


def extract_body_name(soup: BeautifulSoup, view_id: int) -> str | None:
    """Extract the body/committee name from a ViewPublisher page."""
    has_content = (
        soup.find("a", href=re.compile(r"(player/clip|MediaPlayer|GeneratedAgenda)"))
        or soup.find(string=re.compile(r"(Upcoming|Archived|Events)", re.I))
    )
    if not has_content:
        return None

    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        for suffix in [" - Granicus", " | Granicus", " - ViewPublisher"]:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
        if text and text.lower() not in ("granicus", "viewpublisher", ""):
            return text

    for tag in soup.find_all(["h1", "h2"]):
        text = tag.get_text(strip=True)
        if text and len(text) > 2:
            return text

    return f"View {view_id}"


def parse_view_publisher(
    get_fn, soup_fn, base_url: str, view_id: int, body_name: str
) -> list:
    """Parse meetings for a single view.

    Primary strategy: RSS feed. Fallback: HTML scraping.

    ``get_fn`` and ``soup_fn`` are callables from the scraper for
    HTTP requests (handles delay/retry/session).

    Returns a list of ClipRef-like dicts.
    """
    from scrapers.granicus.scraper import ClipRef

    # --- Primary: RSS feed ---
    refs = _parse_view_publisher_rss(get_fn, base_url, view_id, body_name)
    if refs:
        logger.info("  Parsed %d clips from RSS feed", len(refs))
        return refs

    # --- Fallback: HTML scraping ---
    url = f"{base_url}/ViewPublisher.php?view_id={view_id}"
    soup = soup_fn(url)

    refs: list[ClipRef] = []
    seen_clip_ids: set[int] = set()

    tables = soup.find_all("table", class_="listingTable")
    if not tables:
        table = _find_meeting_table(soup)
        if table:
            tables = [table]

    for table in tables:
        tbody = table.find("tbody") or table
        for row in tbody.find_all("tr", recursive=False):
            clip_id = _extract_clip_id_from_row(row)
            if clip_id is None or clip_id in seen_clip_ids:
                continue
            seen_clip_ids.add(clip_id)

            date_str = _extract_date_from_row(row)

            agenda_url = None
            minutes_url = None
            for link in row.find_all("a", href=True):
                href = link["href"]
                if not agenda_url and (
                    "AgendaViewer" in href
                    or "GeneratedAgendaViewer" in href
                ):
                    link_clip = extract_clip_id(href)
                    if link_clip is None or link_clip == clip_id:
                        agenda_url = urljoin(base_url + "/", href)
                elif not minutes_url and "MinutesViewer" in href:
                    link_clip = extract_clip_id(href)
                    if link_clip is None or link_clip == clip_id:
                        minutes_url = urljoin(base_url + "/", href)

            ref = ClipRef(
                clip_id=clip_id,
                view_id=view_id,
                title=body_name,
                date=date_str,
                video_url=(
                    f"{base_url}/MediaPlayer.php"
                    f"?view_id={view_id}&clip_id={clip_id}"
                ),
                agenda_url=agenda_url,
                minutes_url=minutes_url,
            )
            refs.append(ref)

    if not refs:
        refs = _parse_view_publisher_divs(soup, base_url, view_id, body_name)

    return refs


def _parse_view_publisher_rss(
    get_fn, base_url: str, view_id: int, body_name: str
) -> list:
    """Parse meeting listings from the ViewPublisher RSS feed."""
    from scrapers.granicus.scraper import ClipRef

    url = f"{base_url}/ViewPublisherRSS.php?view_id={view_id}"
    try:
        resp = get_fn(url)
    except requests.RequestException as e:
        logger.debug("RSS feed not available for view %d: %s", view_id, e)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.debug("RSS parse error for view %d: %s", view_id, e)
        return []

    refs: list[ClipRef] = []
    seen: set[int] = set()

    for item in root.iter("item"):
        link_el = item.find("link")
        if link_el is None or not link_el.text:
            continue

        clip_id = extract_clip_id(link_el.text.strip())
        if clip_id is None or clip_id in seen:
            continue
        seen.add(clip_id)

        date_str = ""
        pub_date_el = item.find("pubDate")
        if pub_date_el is not None and pub_date_el.text:
            try:
                dt = parsedate_to_datetime(pub_date_el.text.strip())
                date_str = dt.strftime("%B %d, %Y")
            except (ValueError, TypeError):
                pass

        title_el = item.find("title")
        title_text = title_el.text.strip() if title_el is not None and title_el.text else ""
        if not date_str and title_text:
            date_str = extract_date_from_text(title_text)

        agenda_url = None
        minutes_url = None
        desc_el = item.find("description")
        if desc_el is not None and desc_el.text:
            desc_soup = BeautifulSoup(desc_el.text, "html.parser")
            for a in desc_soup.find_all("a", href=True):
                href = a["href"]
                if not agenda_url and (
                    "GeneratedAgendaViewer" in href or "AgendaViewer" in href
                ):
                    agenda_url = urljoin(base_url + "/", href)
                elif not minutes_url and "MinutesViewer" in href:
                    minutes_url = urljoin(base_url + "/", href)

        ref = ClipRef(
            clip_id=clip_id,
            view_id=view_id,
            title=body_name or title_text,
            date=date_str,
            video_url=urljoin(base_url + "/", link_el.text.strip()),
            agenda_url=agenda_url,
            minutes_url=minutes_url,
        )
        refs.append(ref)

    return refs


def _find_meeting_table(soup: BeautifulSoup) -> Tag | None:
    """Locate the innermost table that contains player links."""
    player_re = re.compile(
        r"(player/clip/\d+|MediaPlayer\.php\?.*clip_id=\d+)"
    )
    candidates = [
        t for t in soup.find_all("table")
        if t.find("a", href=player_re)
    ]
    if not candidates:
        return None

    for table in candidates:
        has_inner = any(
            child in candidates
            for child in table.find_all("table")
        )
        if not has_inner:
            return table

    return candidates[0]


def _extract_clip_id_from_row(row: Tag) -> int | None:
    """Extract clip_id from any link in a table row."""
    for link in row.find_all("a"):
        onclick = link.get("onclick") or ""
        if onclick:
            m = re.search(r"clip_id=(\d+)", onclick)
            if m:
                return int(m.group(1))

        href = link.get("href") or ""
        m = re.search(r"(?:player/clip/|clip_id=)(\d+)", href)
        if m:
            return int(m.group(1))

    return None


def _extract_date_from_row(row: Tag) -> str:
    """Extract the date from a ViewPublisher table row."""
    for cell in row.find_all("td"):
        headers = cell.get("headers") or ""
        classes = " ".join(cell.get("class") or [])
        if "Date" in headers or "Date" in classes:
            text = cell.get_text(" ", strip=True)
            m = _DATE_RE.search(text)
            return m.group(1).strip() if m else ""

    text = row.get_text(" ", strip=True)
    m = _DATE_RE.search(text)
    return m.group(1).strip() if m else ""


def _parse_view_publisher_divs(
    soup: BeautifulSoup, base_url: str, view_id: int, body_name: str
) -> list:
    """Fallback parser for ViewPublisher pages that use div-based layouts."""
    from scrapers.granicus.scraper import ClipRef

    refs = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        clip_id = extract_clip_id(href)
        if clip_id is None:
            continue

        if any(r.clip_id == clip_id for r in refs):
            continue

        ref = ClipRef(
            clip_id=clip_id,
            view_id=view_id,
            title=body_name,
            video_url=urljoin(base_url + "/", href),
        )

        parent = link.find_parent(["div", "li", "section"])
        if parent:
            date = extract_date_from_text(parent.get_text())
            if date:
                ref.date = date

            _find_sibling_links(parent, ref, base_url)

        refs.append(ref)

    return refs


def _find_sibling_links(container: Tag, ref, base_url: str):
    """Find agenda and minutes links within a container element."""
    for link in container.find_all("a", href=True):
        href = link["href"]
        if "GeneratedAgendaViewer" in href or "AgendaViewer" in href:
            link_clip = extract_clip_id(href)
            if link_clip is not None and link_clip != ref.clip_id:
                continue
            ref.agenda_url = urljoin(base_url + "/", href)
            event_id = extract_param(href, "event_id")
            if event_id:
                ref.event_id = int(event_id)
        elif "MinutesViewer" in href:
            link_clip = extract_clip_id(href)
            if link_clip is not None and link_clip != ref.clip_id:
                continue
            ref.minutes_url = urljoin(base_url + "/", href)
