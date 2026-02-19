"""Parser for Granicus pages using CSS class markers (Agenda0/1/2).

This is the Sacramento-style pattern where agenda items are marked with
``class="Agenda Agenda0/1/2"`` links and documents with
``class="Document Document1/2"`` links.  The Agenda class level indicates
nesting depth (0 = section, 1 = item, 2 = sub-item).
"""

import re

from bs4 import BeautifulSoup, Tag

from scrapers.granicus.extraction import (
    _DATE_RE,
    classify_document,
    classify_item,
    extract_body_after_bold,
    extract_body_from_table,
    extract_documents_near,
    extract_param,
    is_centered,
    split_order_prefix,
)
from scrapers.granicus.parsers.base import AgendaParser, ParseContext


class CssClassParser(AgendaParser):
    """Parse agenda items from ``class="Agenda Agenda0/1/2"`` markers."""

    def can_parse(self, soup: BeautifulSoup) -> bool:
        return bool(soup.find("a", class_=re.compile(r"\bAgenda\b")))

    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        agenda_links = soup.find_all("a", class_=re.compile(r"\bAgenda\b"))
        if not agenda_links:
            return []

        items: list[dict] = []
        seen: set[str] = set()

        all_markers = _collect_agenda_markers(soup, agenda_links)

        for marker in all_markers:
            title = marker["title"]
            if title in seen or not title or len(title) < 3:
                continue
            seen.add(title)

            item: dict = {"title": title, "source": "agenda_page"}
            if marker.get("order"):
                item["order"] = marker["order"]
            if marker.get("level") is not None:
                item["level"] = marker["level"]
            item["classification"] = classify_item(title)

            # Video timestamp from the Agenda link
            if marker.get("meta_id"):
                item["media_ref"] = {"media_id": f"clip-{ctx.clip_id}"}
                href = marker.get("href", "")
                offset = (
                    extract_param(href, "entrytime")
                    or extract_param(href, "starttime")
                )
                if offset:
                    try:
                        item["media_ref"]["offset_seconds"] = int(float(offset))
                    except ValueError:
                        pass

            # Documents from following siblings until next Agenda marker
            if marker.get("element"):
                docs = extract_documents_near(marker["element"], ctx.base_url)
                if docs:
                    item["documents"] = docs

                # Body text
                el = marker["element"]
                if el.name == "a":
                    body = extract_body_from_table(el)
                elif el.name in ("strong", "b"):
                    body = extract_body_after_bold(el)
                else:
                    body = None
                if body:
                    item["body"] = body

            items.append(item)

        # Supplement with numbered table items (consent calendar items
        # that lack Agenda-class links).
        supplemental = _find_numbered_table_items(soup, ctx)
        if supplemental:
            existing = {it["title"] for it in items}
            new_items = [s for s in supplemental if s["title"] not in existing]
            if new_items:
                insert_idx = len(items)
                for idx, it in enumerate(items):
                    if it.get("level") == 0 and "consent" in it["title"].lower():
                        insert_idx = idx + 1
                        for j in range(idx + 1, len(items)):
                            if items[j].get("level") == 0:
                                insert_idx = j
                                break
                        break
                for i, supp in enumerate(new_items):
                    items.insert(insert_idx + i, supp)

            # Deduplicate: remove docs from section headers that now
            # belong to individual sub-items.
            child_doc_urls: set[str] = set()
            for supp in supplemental:
                for doc in supp.get("documents", []):
                    child_doc_urls.add(doc.get("url", ""))
            if child_doc_urls:
                for it in items:
                    if it.get("documents") and it.get("level") == 0:
                        it["documents"] = [
                            d for d in it["documents"]
                            if d.get("url", "") not in child_doc_urls
                        ]
                        if not it["documents"]:
                            del it["documents"]

        return items


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _collect_agenda_markers(
    soup: BeautifulSoup, agenda_links: list[Tag]
) -> list[dict]:
    """Build an ordered list of agenda markers from class="Agenda" links
    and plain bold/underlined section headers."""
    agenda_names = {a.get("name") for a in agenda_links if a.get("name")}

    markers: list[dict] = []
    seen_names: set[str] = set()

    for el in soup.find_all(["a", "strong", "b", "h2", "h3"]):
        if el.name == "a":
            classes = " ".join(el.get("class") or [])
            if "Agenda" not in classes:
                continue
            name = el.get("name", "")
            if name in seen_names:
                continue
            seen_names.add(name)

            title = el.get_text(strip=True)
            meta_id = re.search(r"\d+", name).group() if re.search(r"\d+", name) else None
            order, clean_title = split_order_prefix(title)

            level: int | None = None
            level_m = re.search(r"\bAgenda(\d+)\b", classes)
            if level_m:
                level = int(level_m.group(1))

            markers.append({
                "title": clean_title or title,
                "order": order,
                "meta_id": meta_id,
                "href": el.get("href", ""),
                "element": el,
                "level": level,
            })

        elif el.name in ("h2", "h3"):
            # Handled by HeadingsParser instead.
            continue

        else:
            # <strong> or <b> wrapping a <u> child (standalone section header)
            u_child = el.find("u")
            if not u_child:
                continue
            if el.find("a", class_=re.compile(r"\bAgenda\b")):
                continue
            title = u_child.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            if is_centered(el):
                continue
            date_m = _DATE_RE.search(title)
            if date_m and len(title) < len(date_m.group(1)) + 20:
                continue
            title_lower = title.lower()
            if title_lower.startswith("print "):
                continue
            if title_lower.startswith(("supplemental material", "amended material")):
                continue

            order, clean_title = split_order_prefix(title)
            markers.append({
                "title": clean_title or title,
                "order": order,
                "meta_id": None,
                "href": "",
                "element": el,
                "level": 0,
            })

    return markers


def _find_numbered_table_items(
    soup: BeautifulSoup, ctx: ParseContext
) -> list[dict]:
    """Find numbered agenda items inside per-item ``<table>`` elements.

    Sacramento consent calendar items sometimes lack ``class="Agenda"``
    links but still appear in tables with numbered bold elements.
    """
    from urllib.parse import urljoin

    items: list[dict] = []
    seen: set[str] = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        first_row = rows[0]
        cells = first_row.find_all("td")
        if len(cells) < 2:
            continue

        num_strong = cells[0].find("strong")
        if not num_strong:
            continue
        num_text = num_strong.get_text(strip=True)
        if not re.match(r"^\d+[a-z]?\.$", num_text):
            continue

        title_strong = cells[1].find("strong")
        if not title_strong:
            continue

        if title_strong.find("a", class_=re.compile(r"\bAgenda\b")):
            continue

        title = title_strong.get_text(strip=True)
        if not title or len(title) < 5 or title in seen:
            continue
        seen.add(title)

        order = num_text.rstrip(".")

        item: dict = {
            "title": title,
            "order": order,
            "source": "agenda_page",
            "level": 1,
            "classification": classify_item(title),
        }

        body = extract_body_from_table(title_strong)
        if body:
            item["body"] = body

        docs: list[dict] = []
        seen_urls: set[str] = set()
        for tag in table.find_all_next(["a", "table"]):
            if tag.name == "table" and tag is not table:
                break
            if tag.name != "a":
                continue
            classes = " ".join(tag.get("class") or [])
            if "Document" not in classes:
                continue
            href = tag.get("href", "")
            if "MetaViewer" not in href:
                continue
            full_url = urljoin(ctx.base_url + "/", href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            text = tag.get_text(strip=True) or "Attachment"
            docs.append({
                "title": text,
                "url": full_url,
                "type": classify_document(text),
            })
        if docs:
            item["documents"] = docs

        items.append(item)

    return items
