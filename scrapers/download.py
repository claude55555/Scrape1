"""Download files referenced in scraped meeting data.

Walks a meeting dict, finds all downloadable URL fields, applies
extension-based filters, downloads matching files, and records the
local relative path back into the meeting dict.

Downloadable locations:
    - documents[].url           (meeting-level documents)
    - agenda_items[].documents[].url  (item-level, recursively)
    - media[].url               (video/audio recordings)
    - media[].captions.url      (caption/transcript files)

NOT downloaded (informational links):
    - location.url, jurisdiction.url, sources[].url
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Extensions treated as documents/transcripts (downloaded by default)
DEFAULT_INCLUDE = frozenset({
    ".pdf", ".vtt", ".srt", ".smi",
    ".doc", ".docx", ".xls", ".xlsx",
    ".txt", ".rtf", ".csv",
})

# Extensions for large media files (excluded by default)
DEFAULT_EXCLUDE = frozenset({
    ".mp4", ".m3u8", ".mp3", ".wav", ".m4a",
    ".avi", ".mov", ".wmv", ".flv", ".webm",
})


@dataclass
class DownloadConfig:
    """Configuration for the file download step."""

    enabled: bool = False
    base_dir: Path | None = None
    include_types: set[str] = field(default_factory=set)
    exclude_types: set[str] = field(default_factory=set)

    def should_download(self, url: str) -> bool:
        """Decide whether a URL should be downloaded based on extension filters."""
        ext = _url_extension(url)

        # If include list is set, only download matching extensions
        if self.include_types:
            return ext in self.include_types

        # If exclude list is set, skip matching extensions
        if self.exclude_types:
            return ext not in self.exclude_types

        # Neither set — use defaults: include docs, exclude media
        if ext in DEFAULT_EXCLUDE:
            return False
        if ext in DEFAULT_INCLUDE:
            return True

        # Unknown extension: download (err on the side of getting the file)
        return True


def download_meeting_files(
    meeting: dict,
    config: DownloadConfig,
    session: requests.Session,
    json_dir: Path,
) -> dict:
    """Download files from a meeting dict and add local_path fields.

    Args:
        meeting: The meeting dict (modified in place and returned).
        config: Download configuration (filters, base dir).
        session: HTTP session to use for downloads.
        json_dir: Directory where the meeting JSON file lives.
                  local_path values are stored relative to this.

    Returns:
        The meeting dict with local_path fields populated where files
        were successfully downloaded.
    """
    # Determine where to save files
    # Try to derive a clip-specific subdirectory from meeting identifiers
    clip_slug = _meeting_clip_slug(meeting)
    files_dir = (config.base_dir or json_dir) / "files" / clip_slug
    files_dir.mkdir(parents=True, exist_ok=True)

    used_filenames: set[str] = set()

    # --- Documents (meeting-level) ---
    for doc in meeting.get("documents", []):
        _download_document(doc, files_dir, json_dir, config, session, used_filenames)

    # --- Agenda items (recursive) ---
    for item in meeting.get("agenda_items", []):
        _process_agenda_item(item, files_dir, json_dir, config, session, used_filenames)

    # --- Media ---
    for media in meeting.get("media", []):
        url = media.get("url", "")
        if url and config.should_download(url):
            filename = _unique_filename(
                _media_filename(media), used_filenames
            )
            local = _download_file(url, files_dir, filename, session)
            if local:
                media["local_path"] = _relative_path(local, json_dir)

        # Captions
        captions = media.get("captions")
        if isinstance(captions, dict):
            cap_url = captions.get("url", "")
            if cap_url and config.should_download(cap_url):
                ext = _url_extension(cap_url) or ".vtt"
                filename = _unique_filename(f"captions{ext}", used_filenames)
                local = _download_file(cap_url, files_dir, filename, session)
                if local:
                    captions["local_path"] = _relative_path(local, json_dir)

    return meeting


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _process_agenda_item(
    item: dict,
    files_dir: Path,
    json_dir: Path,
    config: DownloadConfig,
    session: requests.Session,
    used_filenames: set[str],
) -> None:
    """Process documents on an agenda item, recursing into sub-items."""
    for doc in item.get("documents", []):
        _download_document(doc, files_dir, json_dir, config, session, used_filenames)

    for sub_item in item.get("items", []):
        _process_agenda_item(sub_item, files_dir, json_dir, config, session, used_filenames)


def _download_document(
    doc: dict,
    files_dir: Path,
    json_dir: Path,
    config: DownloadConfig,
    session: requests.Session,
    used_filenames: set[str],
) -> None:
    """Download a single document dict's URL if it passes filters."""
    url = doc.get("url", "")
    if not url or not config.should_download(url):
        return

    filename = _unique_filename(_doc_filename(doc), used_filenames)
    local = _download_file(url, files_dir, filename, session)
    if local:
        doc["local_path"] = _relative_path(local, json_dir)


def _download_file(
    url: str, dest_dir: Path, filename: str, session: requests.Session
) -> Path | None:
    """Download a URL to dest_dir/filename. Returns the path on success, None on failure."""
    dest = dest_dir / filename
    if dest.exists():
        logger.debug("Already downloaded: %s", dest)
        return dest

    logger.info("Downloading %s -> %s", url, dest)
    try:
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to download %s: %s", url, e)
        return None

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    size = dest.stat().st_size
    logger.info("  Saved %s (%d bytes)", dest.name, size)
    return dest


def _meeting_clip_slug(meeting: dict) -> str:
    """Derive a directory name for this meeting's files."""
    # Try to get a clip ID from identifiers
    for ident in meeting.get("identifiers", []):
        scheme = ident.get("scheme", "")
        if "granicus" in scheme and "event" not in scheme:
            return f"clip-{ident['identifier']}"

    # Try media IDs
    for media in meeting.get("media", []):
        mid = media.get("id", "")
        if mid:
            return mid

    # Fallback: org-slug + date
    org = meeting.get("organization", {})
    org_name = org.get("name", "") if isinstance(org, dict) else str(org)
    slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-")
    date = meeting.get("start_date", "unknown")[:10]
    return f"{slug}-{date}" if slug else f"meeting-{date}"


def _doc_filename(doc: dict) -> str:
    """Derive a filename for a document from its metadata."""
    url = doc.get("url", "")
    doc_type = doc.get("type", "")
    title = doc.get("title", "")
    ext = _url_extension(url) or ".pdf"

    # Try to extract a meta_id from Granicus MetaViewer URLs
    meta_match = re.search(r"meta_id=(\d+)", url)
    if meta_match:
        meta_id = meta_match.group(1)
        if doc_type:
            return f"{doc_type}-meta-{meta_id}{ext}"
        return f"doc-meta-{meta_id}{ext}"

    # Use doc type if available
    if doc_type:
        slug = re.sub(r"[^a-z0-9]+", "-", doc_type.lower()).strip("-")
        return f"{slug}{ext}"

    # Use title
    if title:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        return f"{slug}{ext}"

    # Last resort: derive from URL path
    url_path = urlparse(url).path
    url_name = PurePosixPath(url_path).name
    if url_name and "." in url_name:
        return url_name

    return f"document{ext}"


def _media_filename(media: dict) -> str:
    """Derive a filename for a media entry."""
    url = media.get("url", "")
    media_type = media.get("type", "video")
    ext = _url_extension(url) or ".mp4"
    return f"{media_type}{ext}"


def _url_extension(url: str) -> str:
    """Extract the file extension from a URL, or empty string if none."""
    if not url:
        return ""
    path = urlparse(url).path
    if "." in PurePosixPath(path).name:
        ext = PurePosixPath(path).suffix.lower()
        # Only return recognisable extensions (skip things like .php)
        if len(ext) <= 6 and ext not in {".php", ".asp", ".aspx", ".jsp", ".htm", ".html"}:
            return ext
    return ""


def _unique_filename(desired: str, used: set[str]) -> str:
    """Ensure a filename is unique within the used set."""
    if desired not in used:
        used.add(desired)
        return desired

    stem = PurePosixPath(desired).stem
    ext = PurePosixPath(desired).suffix
    n = 2
    while True:
        candidate = f"{stem}-{n}{ext}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _relative_path(file_path: Path, relative_to: Path) -> str:
    """Compute a relative path string from relative_to to file_path."""
    try:
        return str(file_path.relative_to(relative_to))
    except ValueError:
        # Different drives or can't compute relative — use PurePosixPath
        return str(file_path)
