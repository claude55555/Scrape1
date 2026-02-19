"""Generic parser for Granicus pages using table rows as agenda items.

Handles pages where each table row represents an agenda item, with
cells containing item number, title, document links, and video links.
"""

import re

from bs4 import BeautifulSoup

from scrapers.granicus.extraction import (
    classify_item,
    extract_documents_within,
    extract_media_ref,
    split_order_prefix,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext


class TableParser(AgendaParser):
    """Parse agenda items from HTML table rows."""

    def can_parse(self, soup: BeautifulSoup) -> bool:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) >= 2:
                has_links = bool(
                    table.find("a", href=re.compile(
                        r"(player/clip|MediaPlayer|MetaViewer|entrytime|starttime)"
                    ))
                )
                if has_links:
                    return True
        return False

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        items: list[dict] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            for row in rows:
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue

                row_text = row.get_text(" ", strip=True)
                if not row_text or len(row_text) < 3:
                    continue

                if row_text.lower() in ("video", "agenda", "minutes", "date", "duration"):
                    continue

                # Find the cell with the most text content
                text_content = ""
                for cell in cells:
                    cell_text = cell.get_text(strip=True)
                    if len(cell_text) > len(text_content):
                        text_content = cell_text

                if not text_content or len(text_content) < 3:
                    continue

                order = None
                title = text_content

                # Check if the first non-empty cell is just a number
                for cell in cells:
                    ct = cell.get_text(strip=True)
                    if not ct:
                        continue
                    m = re.match(r"^(\d+[a-z]?\.?\)?|[A-Z]\.?\)?|[IVXLC]+\.?\)?)$", ct)
                    if m:
                        order = ct.rstrip(".)")
                        for other_cell in cells:
                            ot = other_cell.get_text(strip=True)
                            if ot and ot != ct and len(ot) > len(ct):
                                title = ot
                                break
                        break
                    else:
                        m2 = re.match(r"^(\d+[a-z]?[\.\)]?)\s+(.+)", ct)
                        if m2:
                            order = m2.group(1).rstrip(".)")
                            title = m2.group(2).strip()
                        else:
                            title = ct
                        break

                if not title or len(title) < 3:
                    continue

                item: dict = {"title": title, "source": "agenda_page"}
                if order:
                    item["order"] = order

                item["classification"] = classify_item(title)

                docs = extract_documents_within(row, ctx.base_url)
                if docs:
                    item["documents"] = docs

                media = extract_media_ref(row, ctx.clip_id)
                if media:
                    item["media_ref"] = media

                if not any(existing["title"] == title for existing in items):
                    items.append(item)

        return items
