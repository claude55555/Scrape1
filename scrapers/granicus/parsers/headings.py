"""Parser for Granicus pages using h2/h3 headings with span-based numbers.

This is the Shoreline-style pattern::

    <h2><span>1.&nbsp;</span><span>CALL TO ORDER</span></h2>
    <h3><span>A.&nbsp;</span><span>Sub-item title</span></h3>
"""

import re

from bs4 import BeautifulSoup

from scrapers.granicus.extraction import (
    classify_item,
    extract_body_after_heading,
    extract_documents_near,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext


class HeadingsParser(AgendaParser):
    """Parse agenda items from ``<h2>``/``<h3>`` headings."""

    def can_parse(self, soup: BeautifulSoup) -> bool:
        for h in soup.find_all(["h2", "h3"]):
            if h.find("span", recursive=False):
                return True
        return False

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        headings = soup.find_all(["h2", "h3"])
        if not headings:
            return []

        items: list[dict] = []
        seen: set[str] = set()

        for heading in headings:
            spans = heading.find_all("span", recursive=False)
            if len(spans) >= 2:
                order_text = spans[0].get_text(strip=True).rstrip(".\xa0 ")
                title = spans[1].get_text(strip=True)
            else:
                title = heading.get_text(strip=True)
                order_text = None
                order_m = re.match(
                    r"^(\d+[a-z]?[\.\)]?|[A-Z][\.\)]?)\s+(.+)", title
                )
                if order_m:
                    order_text = order_m.group(1).rstrip(".)")
                    title = order_m.group(2).strip()

            if not title or len(title) < 3 or title in seen:
                continue
            if title.lower() in ("links", "footer", "header", ""):
                continue
            seen.add(title)

            level = 0 if heading.name == "h2" else 1

            item: dict = {"title": title, "source": "agenda_page", "level": level}
            if order_text:
                item["order"] = order_text
            item["classification"] = classify_item(title)

            docs = extract_documents_near(heading, ctx.base_url)
            if docs:
                item["documents"] = docs

            body = extract_body_after_heading(heading)
            if body:
                item["body"] = body

            items.append(item)

        return items
