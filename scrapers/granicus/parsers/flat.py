"""Last-resort parser: extract agenda items from any text structure.

Looks for numbered/lettered lines and associated links.  Used when
no more specific parser matches.
"""

import re

from bs4 import BeautifulSoup

from scrapers.granicus.extraction import (
    classify_item,
    extract_documents_within,
    extract_media_ref,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext


class FlatParser(AgendaParser):
    """Flat parse: extract agenda items from any text structure."""

    def can_parse(self, soup: BeautifulSoup) -> bool:
        # Last resort — always available.
        return True

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        items: list[dict] = []
        seen_titles: set[str] = set()

        for el in soup.find_all(["p", "div", "tr", "li", "span"]):
            text = el.get_text(strip=True)
            if not text or len(text) < 3:
                continue

            if text in seen_titles:
                continue

            order_match = re.match(
                r"^(\d+[\.\)]?\s*[a-z]?[\.\)]?|[A-Z][\.\)]|[IVXLC]+[\.\)])\s+(.+)",
                text,
            )

            if order_match:
                order = order_match.group(1).rstrip(".)")
                title = order_match.group(2).strip()
            else:
                if el.name in ("b", "strong", "h1", "h2", "h3", "h4"):
                    title = text
                    order = None
                else:
                    continue

            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            item: dict = {"title": title, "source": "agenda_page"}
            if order:
                item["order"] = order

            item["classification"] = classify_item(title)

            docs = extract_documents_within(el, ctx.base_url)
            if docs:
                item["documents"] = docs

            media = extract_media_ref(el, ctx.clip_id)
            if media:
                item["media_ref"] = media

            items.append(item)

        return items
