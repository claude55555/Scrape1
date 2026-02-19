"""Generic parser for Granicus pages using bold text as item markers.

Granicus GeneratedAgendaViewer universally wraps every agenda-item title
in bold text, regardless of the surrounding HTML layout.  This strategy
finds bold elements and pairs each with adjacent video-timestamp links
and MetaViewer document links.
"""

import re

from bs4 import BeautifulSoup

from scrapers.granicus.extraction import (
    _DATE_RE,
    classify_item,
    extract_documents_within,
    extract_media_ref,
    is_centered,
    split_order_prefix,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext

_SKIP = {
    "video", "agenda", "minutes", "back", "home", "print",
    "close", "search", "login", "granicus", "",
}


class BoldParser(AgendaParser):
    """Parse agenda items by locating <b>/<strong> markers."""

    def can_parse(self, soup: BeautifulSoup) -> bool:
        # Bold text is extremely common — this parser is a generic
        # fallback, so it always *can* parse.  The scraper only
        # reaches it if more specific parsers didn't match.
        return bool(soup.find(["b", "strong"]))

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        items: list[dict] = []
        seen_titles: set[str] = set()

        for bold in soup.find_all(["b", "strong"]):
            text = bold.get_text(strip=True)
            if not text or len(text) < 3:
                continue

            if text.lower().strip("»«:") in _SKIP:
                continue

            if not re.match(r"^\d", text):
                if is_centered(bold):
                    continue

            date_m = _DATE_RE.search(text)
            if date_m and len(text) < len(date_m.group(1)) + 20:
                continue

            if text in seen_titles:
                continue
            seen_titles.add(text)

            order, title = split_order_prefix(text)

            if not title or len(title) < 3:
                continue

            item: dict = {"title": title, "source": "agenda_page"}
            if order:
                item["order"] = order
            item["classification"] = classify_item(title)

            # Walk up to the nearest block-level container for docs/media
            container = bold.parent
            if container and container.name in ("td", "span", "font", "p"):
                container = container.parent or container

            if container:
                docs = extract_documents_within(container, ctx.base_url)
                if docs:
                    item["documents"] = docs

                media = extract_media_ref(container, ctx.clip_id)
                if media:
                    item["media_ref"] = media

            items.append(item)

        return items
