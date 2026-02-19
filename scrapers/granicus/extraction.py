"""Shared extraction helpers for Granicus agenda parsing.

These helpers are building blocks that any agenda parser can compose.
They handle the common tasks of extracting document links, body text,
order numbers, timestamps, and classifying items — without coupling
to any specific HTML structure.
"""

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import Tag

# Strict date pattern: requires real month names so we never match durations,
# agenda-item numbers ("Item 3, 2025"), or other non-date text.
_MONTH_RE = (
    r"(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December"
    r"|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
)
_DATE_RE = re.compile(
    rf"({_MONTH_RE}\.?\s+\d{{1,2}},?\s+\d{{4}}"  # Jan 15, 2025 / Jan. 15 2025
    rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"             # 1/15/2025 or 1/15/25
    rf"|\d{{4}}-\d{{2}}-\d{{2}})",                  # 2025-01-15
    re.IGNORECASE,
)


# ------------------------------------------------------------------
# Order / title splitting
# ------------------------------------------------------------------

def split_order_prefix(text: str) -> tuple[str | None, str]:
    """Split a leading order number/letter from agenda item text.

    Returns (order, remaining_text).  If no prefix is found, returns
    (None, original_text).

    Examples:
        "1. Call to Order"  → ("1", "Call to Order")
        "IV. Public Hearing" → ("IV", "Public Hearing")
        "Call to Order"      → (None, "Call to Order")
    """
    m = re.match(r"^(\d+[a-z]?[\.\)]?)\s*\.?\s+(.+)", text, re.DOTALL)
    if m:
        return m.group(1).rstrip(".)"), m.group(2).strip()
    m = re.match(r"^([A-Z]{1,5}[\.\)]|[IVXLC]+[\.\)])\s+(.+)", text)
    if m:
        return m.group(1).rstrip(".)"), m.group(2).strip()
    return None, text


# ------------------------------------------------------------------
# Classification
# ------------------------------------------------------------------

_ITEM_PATTERNS: dict[str, str] = {
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


def classify_item(title: str) -> str | None:
    """Guess the classification of an agenda item from its title."""
    t = title.lower()
    for classification, pattern in _ITEM_PATTERNS.items():
        if re.search(pattern, t, re.I):
            return classification
    return None


def classify_document(text: str) -> str:
    """Classify a document by its link text."""
    t = text.lower()
    if "staff report" in t:
        return "staff_report"
    if "ordinance" in t:
        return "ordinance"
    if "resolution" in t:
        return "resolution"
    if "agenda" in t:
        return "agenda"
    if "minutes" in t:
        return "minutes"
    if "presentation" in t:
        return "presentation"
    return "attachment"


# ------------------------------------------------------------------
# HTML analysis helpers
# ------------------------------------------------------------------

def is_centered(el: Tag) -> bool:
    """Return True if *el* (or an ancestor) is center-aligned."""
    node = el
    while node and node.name not in ("body", "html", "[document]"):
        style = node.get("style") or ""
        if "text-align" in style and "center" in style:
            return True
        if (node.get("align") or "").lower() == "center":
            return True
        if node.name == "center":
            return True
        node = node.parent
    return False


def extract_date_from_text(text: str) -> str:
    """Return the first date found in *text*, or empty string."""
    m = _DATE_RE.search(text)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------------
# Document extraction
# ------------------------------------------------------------------

def extract_documents_near(element: Tag, base_url: str) -> list[dict]:
    """Extract ``class="Document"`` links near an agenda item element.

    Walks forward through the document from *element* collecting
    ``Document``-class links until the next agenda marker (another
    ``Agenda``-class link, an ``<h2>/<h3>`` heading, or a bold
    section header).
    """
    docs: list[dict] = []
    seen_urls: set[str] = set()

    for tag in element.find_all_next(["a", "h2", "h3", "strong"]):
        if tag.name in ("h2", "h3") and tag is not element:
            break

        if tag.name == "strong" and tag.find("u"):
            if tag.find("a", class_=re.compile(r"\bAgenda\b")):
                break
            if not is_centered(tag):
                break

        if tag.name != "a":
            continue

        classes = " ".join(tag.get("class") or [])

        if "Agenda" in classes and "Document" not in classes:
            break

        if "Document" not in classes:
            continue
        href = tag.get("href", "")
        if "MetaViewer" not in href:
            continue
        full_url = urljoin(base_url + "/", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        text = tag.get_text(strip=True) or "Attachment"
        docs.append({
            "title": text,
            "url": full_url,
            "type": classify_document(text),
        })

    return docs


def extract_documents_within(el: Tag, base_url: str) -> list[dict]:
    """Find document links (MetaViewer.php) within an element."""
    docs = []
    for link in el.find_all("a", href=re.compile(r"MetaViewer\.php")):
        href = link["href"]
        full_url = urljoin(base_url + "/", href)
        text = link.get_text(strip=True) or "Attachment"
        docs.append({
            "title": text,
            "url": full_url,
            "type": classify_document(text),
        })
    return docs


def extract_meeting_level_docs(soup, base_url: str) -> list[dict]:
    """Extract meeting-level documents (full agenda, packet, etc.)."""
    meeting_docs: list[dict] = []
    seen_urls: set[str] = set()

    for link in soup.find_all("a", class_=re.compile(r"\bDocument\b")):
        href = link.get("href", "")
        if "MetaViewer" not in href:
            continue
        text = link.get_text(strip=True)
        text_lower = text.lower()

        classes = " ".join(link.get("class") or [])
        is_doc0_or_1 = "Document0" in classes or "Document1" in classes
        is_meeting_level = any(
            kw in text_lower for kw in (
                "print meeting", "pdf packet", "full agenda",
                "agenda packet", "meeting packet",
            )
        )
        if is_doc0_or_1 and is_meeting_level:
            full_url = urljoin(base_url + "/", href)
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                meeting_docs.append({
                    "title": text,
                    "url": full_url,
                    "type": "agenda" if "agenda" in text_lower else "packet",
                })

    if not meeting_docs:
        for link in soup.find_all("a", href=re.compile(r"MetaViewer\.php")):
            text = link.get_text(strip=True).lower()
            if "full agenda" in text or "agenda packet" in text or "meeting packet" in text:
                full_url = urljoin(base_url + "/", link["href"])
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    meeting_docs.append({
                        "title": link.get_text(strip=True),
                        "url": full_url,
                        "type": "agenda" if "agenda" in text else "packet",
                    })

    return meeting_docs


# ------------------------------------------------------------------
# Media / timestamp extraction
# ------------------------------------------------------------------

def extract_media_ref(el: Tag, clip_id: int) -> dict | None:
    """Find a video timestamp link within an element."""
    for link in el.find_all("a", href=re.compile(r"(player/clip|MediaPlayer)")):
        href = link["href"]

        offset = (
            _extract_param(href, "entrytime")
            or _extract_param(href, "starttime")
            or _extract_param(href, "offset")
        )
        if offset:
            try:
                return {
                    "media_id": f"clip-{clip_id}",
                    "offset_seconds": int(float(offset)),
                }
            except ValueError:
                pass

        meta_id = _extract_param(href, "meta_id")
        if meta_id:
            return {"media_id": f"clip-{clip_id}"}

    return None


# ------------------------------------------------------------------
# Body text extraction
# ------------------------------------------------------------------

def extract_body_from_table(element: Tag) -> str | None:
    """Extract body text from a Sacramento-style item table.

    Sacramento items live inside ``<table>`` elements with rows where
    subsequent rows after the title contain Location, Recommendation, etc.
    """
    table = element.find_parent("table")
    if not table:
        return None

    rows = table.find_all("tr")
    if len(rows) < 2:
        return None

    parts: list[str] = []
    for row in rows[1:]:
        cells = row.find_all("td")
        cell = cells[-1] if cells else None
        if not cell:
            continue
        text = cell.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue
        parts.append(text)

    body = "\n".join(parts).strip()
    return body if body else None


def extract_body_after_heading(heading: Tag) -> str | None:
    """Extract body text from ``<p>`` elements after a heading."""
    parts: list[str] = []

    for sibling in heading.next_siblings:
        if isinstance(sibling, str):
            continue
        if not isinstance(sibling, Tag):
            continue
        if sibling.name in ("h2", "h3"):
            break
        if sibling.name == "blockquote":
            break
        if sibling.name == "p":
            text = sibling.get_text(" ", strip=True)
            if text and len(text) > 10:
                parts.append(text)

    body = "\n".join(parts).strip()
    return body if body else None


def extract_body_after_bold(element: Tag) -> str | None:
    """Extract body text following a bold/underline section header."""
    parts: list[str] = []

    for sibling in element.next_siblings:
        if isinstance(sibling, str):
            text = sibling.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(sibling, Tag):
            continue
        if sibling.name in ("table", "blockquote", "div", "strong", "h2", "h3"):
            break
        if sibling.name == "br":
            continue
        text = sibling.get_text(" ", strip=True)
        if text:
            parts.append(text)

    body = " ".join(parts).strip()
    return body if body and len(body) > 10 else None


# ------------------------------------------------------------------
# URL helpers
# ------------------------------------------------------------------

def extract_clip_id(href: str) -> int | None:
    """Extract clip_id from a URL (path or query parameter)."""
    m = re.search(r"player/clip/(\d+)", href)
    if m:
        return int(m.group(1))
    m = re.search(r"clip_id=(\d+)", href)
    if m:
        return int(m.group(1))
    return None


def _extract_param(href: str, param: str) -> str | None:
    """Extract a query parameter value from a URL."""
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    values = params.get(param)
    return values[0] if values else None


# Re-export for convenience (used by parsers and scraper)
extract_param = _extract_param
