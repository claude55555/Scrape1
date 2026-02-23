# Plan: File Download Feature

## Overview

Add an optional download step that fetches files referenced by URLs in scraped meeting data, saves them locally, and records the local path alongside the URL. This applies to documents, captions, and optionally media — anywhere the URL points to a downloadable file rather than an informational link.

## Schema Changes

### 1. Add `local_path` to `document` definition (`schema/v0.1/meeting.json`)

Add an optional `local_path` field to `$defs/document`:
```json
"local_path": {
  "type": "string",
  "description": "Relative path to a locally-downloaded copy of this file. Path is relative to the directory containing this JSON file."
}
```

This applies everywhere `document` is used: top-level `documents[]` and `agenda_items[].documents[]`.

### 2. Add `local_path` to `media` items

Add to the media item properties:
```json
"local_path": {
  "type": "string",
  "description": "Relative path to a locally-downloaded copy of this recording."
}
```

### 3. Replace `caption_url` with a `captions` object on media

Currently `caption_url` is an ad-hoc string field. Replace with:
```json
"captions": {
  "type": "object",
  "properties": {
    "url": { "type": "string", "format": "uri" },
    "local_path": { "type": "string" },
    "media_type": { "type": "string" }
  }
}
```

Update `scrapers/granicus/media.py` `_find_caption_url` to populate the new shape:
```python
media_entry["captions"] = {"url": caption_url}
```
instead of:
```python
media_entry["caption_url"] = caption_url
```

## New Module: `scrapers/download.py`

A scraper-agnostic download module. Not Granicus-specific — any scraper's output can use it.

### `DownloadConfig` dataclass
```python
@dataclass
class DownloadConfig:
    enabled: bool = False
    base_dir: Path | None = None        # defaults to same dir as JSON output
    include_types: set[str] = None      # e.g. {".pdf", ".vtt", ".srt", ".doc", ".docx"}
    exclude_types: set[str] = None      # e.g. {".mp4", ".m3u8"}
```

Filtering logic:
- If `include_types` is set, only download URLs whose extension is in the set
- If `exclude_types` is set, skip URLs whose extension is in the set
- If neither is set, download everything
- `include_types` takes precedence if both are set

### `download_meeting_files(meeting: dict, config: DownloadConfig, session: requests.Session) -> dict`

Walks the meeting dict, finds all downloadable URL fields, applies filters, downloads, and returns a modified meeting dict with `local_path` fields populated.

**Downloadable locations** (exhaustive list):
- `documents[].url` — meeting-level documents
- `agenda_items[].documents[].url` — item-level documents (recursive into nested `items[]`)
- `media[].url` — video/audio recordings
- `media[].captions.url` — caption/transcript files

**NOT downloadable** (informational links):
- `location.url` — Zoom link / venue page
- `jurisdiction.url` — municipality homepage
- `sources[].url` — scrape provenance metadata

### File organization on disk

```
output/sacramento/
├── city-council_2025-03-15_clip-6627.json
└── files/
    └── clip-6627/
        ├── captions.vtt
        ├── agenda.pdf
        ├── minutes.pdf
        ├── doc-meta-12345.pdf      # staff reports, attachments
        └── doc-meta-67890.pdf
```

`local_path` values stored in JSON are relative to the JSON file's directory:
```json
"local_path": "files/clip-6627/agenda.pdf"
```

### Filename derivation

- **Captions**: `captions.{ext}` (extension from URL or `.vtt` default)
- **Meeting-level docs**: `{type}.{ext}` when type is unique (e.g. `agenda.pdf`, `minutes.pdf`), otherwise `{type}-{n}.{ext}`
- **Item-level docs**: `doc-{slugified-title}.{ext}` or `doc-{meta_id}.{ext}` if a Granicus meta_id can be extracted from the URL
- **Media**: `video.{ext}` / `audio.{ext}` (rarely downloaded but supported)

### Extension detection

Extract extension from the URL path. For URLs with no clear extension (like `JSON.php?clip_id=123`), try a HEAD request and use Content-Type → extension mapping. Skip if extension can't be determined and no filter match.

## CLI Changes (`scrapers/granicus/__main__.py`)

Add these arguments:
```
--download              Enable file downloading
--download-include EXT  Only download files with these extensions (repeatable)
                        e.g. --download-include .pdf --download-include .vtt
--download-exclude EXT  Skip files with these extensions (repeatable)
                        e.g. --download-exclude .mp4
```

`--download` without include/exclude uses sensible defaults:
- Include: `.pdf`, `.vtt`, `.srt`, `.smi`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.txt`, `.rtf`
- Exclude: `.mp4`, `.m3u8`, `.mp3`, `.wav`, `.m4a` (large media files)

## Integration into `BaseScraper`

Add to `BaseScraper`:
```python
def save_meeting(self, meeting: dict, download_config: DownloadConfig | None = None) -> Path:
    # ... existing validation and save ...
    if download_config and download_config.enabled:
        meeting = download_meeting_files(meeting, config=download_config, ...)
        # Re-save with local_path fields populated
        path.write_text(json.dumps(meeting, indent=2) + "\n")
    return path
```

## Implementation Order

1. Schema: add `local_path` to document and media, add `captions` object to media
2. Update `media.py`: `caption_url` string → `captions` object
3. Update any tests that reference `caption_url`
4. Create `scrapers/download.py` with `DownloadConfig` and `download_meeting_files`
5. Integrate into `BaseScraper.save_meeting`
6. Add CLI flags to `__main__.py`
7. Tests for download module (mock HTTP, verify file paths and meeting dict updates)

## Files Modified
- `schema/v0.1/meeting.json` — add `local_path`, add `captions` to media
- `scrapers/granicus/media.py` — `caption_url` → `captions` object
- `scrapers/base.py` — accept download config in `save_meeting`
- `scrapers/granicus/__main__.py` — CLI flags

## Files Created
- `scrapers/download.py` — download logic
- `tests/test_download.py` — tests
