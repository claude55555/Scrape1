"""Video/caption/index-point extraction from Granicus player pages.

Handles Phase 2b of scraping: fetching the player page for a clip
and extracting the video URL, duration, caption URL, and index points.
"""

import json
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrapers.granicus.extraction import classify_item, split_order_prefix

logger = logging.getLogger(__name__)


def scrape_media(
    get_fn, soup_fn, base_url: str, clip_id: int, view_id: int
) -> tuple[list[dict], list[dict]]:
    """Scrape video metadata and index points from the player page.

    Returns ``(media_entries, index_items)``.
    """
    url = f"{base_url}/player/clip/{clip_id}?view_id={view_id}"
    try:
        soup = soup_fn(url)
    except requests.RequestException as e:
        logger.warning("Could not fetch player page for clip %d: %s", clip_id, e)
        return [], []

    video_url = _find_video_url(soup)
    duration = _find_duration(soup)

    player_url = f"{base_url}/player/clip/{clip_id}?view_id={view_id}"
    media_entry = {
        "id": f"clip-{clip_id}",
        "url": video_url or player_url,
        "type": "video",
        "label": "Granicus recording",
    }

    if video_url:
        media_entry["media_type"] = (
            "application/x-mpegURL" if "m3u8" in video_url else "video/mp4"
        )

    if duration:
        media_entry["duration_seconds"] = duration

    caption_url = _find_caption_url(soup, base_url, clip_id)
    if caption_url:
        captions = {"url": caption_url}
        if caption_url.endswith(".vtt"):
            captions["media_type"] = "text/vtt"
        elif caption_url.endswith(".srt"):
            captions["media_type"] = "application/x-subrip"
        media_entry["captions"] = captions

    index_items = _extract_index_points(soup, clip_id)

    return [media_entry], index_items


def scrape_agenda_json(get_fn, base_url: str, clip_id: int) -> list[dict]:
    """Fetch agenda items from the ``JSON.php`` endpoint.

    Entries with ``"type": "meta"`` are agenda-item markers with
    titles and video timestamps.
    """
    url = f"{base_url}/JSON.php?clip_id={clip_id}"
    try:
        resp = get_fn(url)
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        logger.debug("JSON.php not available for clip %d: %s", clip_id, e)
        return []

    if not isinstance(data, list):
        return []

    entries: list[dict] = []
    for element in data:
        if isinstance(element, list):
            entries.extend(e for e in element if isinstance(e, dict))
        elif isinstance(element, dict):
            entries.append(element)

    items: list[dict] = []
    order_num = 0
    for entry in entries:
        if entry.get("type") != "meta":
            continue

        title = (entry.get("title") or entry.get("name") or "").strip()
        if not title or len(title) < 3:
            continue

        order_num += 1
        item: dict = {
            "title": title,
            "order": str(order_num),
            "classification": classify_item(title),
            "source": "video_markers",
        }

        timestamp = entry.get("time")
        if timestamp is not None:
            try:
                item["media_ref"] = {
                    "media_id": f"clip-{clip_id}",
                    "offset_seconds": int(float(timestamp)),
                }
            except (ValueError, TypeError):
                pass

        items.append(item)

    return items


def scrape_minutes_link(soup_fn, url: str) -> dict | None:
    """Extract a minutes document reference from MinutesViewer."""
    try:
        soup = soup_fn(url)
    except requests.RequestException:
        return None

    pdf_link = soup.find("a", href=re.compile(r"\.(pdf|PDF)"))
    if pdf_link:
        return {
            "title": "Approved Minutes",
            "url": urljoin(url, pdf_link["href"]),
            "media_type": "application/pdf",
            "type": "minutes",
        }

    return {
        "title": "Approved Minutes",
        "url": url,
        "media_type": "text/html",
        "type": "minutes",
    }


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _find_video_url(soup: BeautifulSoup) -> str | None:
    """Search for the video stream URL on the player page."""
    # Method 1: <source> tags
    for source in soup.find_all("source"):
        src = source.get("src", "")
        if src and ("m3u8" in src or "mp4" in src):
            return src

    # Method 2: <video> tag
    video_tag = soup.find("video")
    if video_tag and video_tag.get("src"):
        return video_tag["src"]

    # Method 3: og:video meta tag
    og_video = soup.find("meta", property="og:video")
    if og_video:
        content = og_video.get("content", "")
        if content:
            return content

    # Method 4: script blocks
    for script in soup.find_all("script"):
        text = script.string or ""
        m3u8_match = re.search(r'["\']?(https?://[^"\']+\.m3u8[^"\']*)["\']?', text)
        if m3u8_match:
            return m3u8_match.group(1)
        mp4_match = re.search(r'"(https?://[^"]+\.mp4[^"]*)"', text)
        if mp4_match:
            return mp4_match.group(1)

    return None


def _find_duration(soup: BeautifulSoup) -> int | None:
    """Try to find the video duration from scripts or meta tags."""
    for script in soup.find_all("script"):
        text = script.string or ""
        dur_match = re.search(r"duration[\"']?\s*[:=]\s*(\d+)", text, re.I)
        if dur_match:
            return int(dur_match.group(1))

    og_dur = soup.find("meta", property="og:video:duration")
    if og_dur:
        try:
            return int(og_dur["content"])
        except (ValueError, KeyError):
            pass

    return None


def _find_caption_url(
    soup: BeautifulSoup, base_url: str, clip_id: int
) -> str | None:
    """Find the caption/transcript URL for a clip."""
    for track in soup.find_all("track"):
        src = track.get("src", "")
        if src and any(ext in src for ext in (".vtt", ".srt", "caption")):
            return urljoin(base_url + "/", src)

    for script in soup.find_all("script"):
        text = script.string or ""
        vtt_match = re.search(
            r'["\']?(https?://[^"\']+\.(?:vtt|srt)[^"\']*)["\']?', text
        )
        if vtt_match:
            return vtt_match.group(1)

    captions_enabled = False
    for script in soup.find_all("script"):
        text = script.string or ""
        if "captionsEnabled" in text and "true" in text.lower():
            captions_enabled = True
            break

    if captions_enabled:
        return f"{base_url}/JSON.php?clip_id={clip_id}"

    return None


def _extract_index_points(soup: BeautifulSoup, clip_id: int) -> list[dict]:
    """Extract agenda items from player page index points."""
    items: list[dict] = []
    order_num = 0

    for div in soup.find_all("div", class_="index-point"):
        title = div.get_text(strip=True)
        if not title or len(title) < 3:
            continue

        order_num += 1
        order, clean_title = split_order_prefix(title)

        item: dict = {
            "title": clean_title or title,
            "order": order or str(order_num),
            "classification": classify_item(clean_title or title),
            "source": "video_markers",
        }

        time_attr = div.get("time")
        if time_attr is not None:
            try:
                item["media_ref"] = {
                    "media_id": f"clip-{clip_id}",
                    "offset_seconds": int(float(time_attr)),
                }
            except (ValueError, TypeError):
                pass

        items.append(item)

    return items
