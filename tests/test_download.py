"""Tests for the file download module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.download import (
    DownloadConfig,
    _doc_filename,
    _media_filename,
    _meeting_clip_slug,
    _unique_filename,
    _url_extension,
    download_meeting_files,
)


# ------------------------------------------------------------------
# DownloadConfig.should_download
# ------------------------------------------------------------------


class TestShouldDownload:
    def test_defaults_include_pdf(self):
        cfg = DownloadConfig(enabled=True)
        assert cfg.should_download("https://example.com/agenda.pdf") is True

    def test_defaults_include_vtt(self):
        cfg = DownloadConfig(enabled=True)
        assert cfg.should_download("https://example.com/captions.vtt") is True

    def test_defaults_exclude_mp4(self):
        cfg = DownloadConfig(enabled=True)
        assert cfg.should_download("https://example.com/video.mp4") is False

    def test_defaults_exclude_m3u8(self):
        cfg = DownloadConfig(enabled=True)
        assert cfg.should_download("https://example.com/stream.m3u8") is False

    def test_defaults_unknown_extension_included(self):
        cfg = DownloadConfig(enabled=True)
        assert cfg.should_download("https://example.com/file.xyz") is True

    def test_include_filter_overrides_defaults(self):
        cfg = DownloadConfig(enabled=True, include_types={".pdf"})
        assert cfg.should_download("https://example.com/file.pdf") is True
        assert cfg.should_download("https://example.com/file.vtt") is False
        assert cfg.should_download("https://example.com/file.mp4") is False

    def test_exclude_filter_overrides_defaults(self):
        cfg = DownloadConfig(enabled=True, exclude_types={".pdf", ".mp4"})
        assert cfg.should_download("https://example.com/file.pdf") is False
        assert cfg.should_download("https://example.com/file.vtt") is True
        assert cfg.should_download("https://example.com/file.mp4") is False

    def test_include_takes_precedence_over_exclude(self):
        cfg = DownloadConfig(
            enabled=True,
            include_types={".pdf"},
            exclude_types={".pdf"},
        )
        # include_types is checked first, so .pdf is included
        assert cfg.should_download("https://example.com/file.pdf") is True

    def test_no_extension_url(self):
        cfg = DownloadConfig(enabled=True)
        # URLs without a recognisable extension — should download
        assert cfg.should_download("https://example.com/JSON.php?clip_id=123") is True


# ------------------------------------------------------------------
# URL extension extraction
# ------------------------------------------------------------------


class TestUrlExtension:
    def test_pdf(self):
        assert _url_extension("https://example.com/doc.pdf") == ".pdf"

    def test_vtt(self):
        assert _url_extension("https://example.com/captions.vtt") == ".vtt"

    def test_mp4_with_query(self):
        assert _url_extension("https://example.com/video.mp4?token=abc") == ".mp4"

    def test_php_excluded(self):
        assert _url_extension("https://example.com/JSON.php?id=1") == ""

    def test_html_excluded(self):
        assert _url_extension("https://example.com/page.html") == ""

    def test_no_extension(self):
        assert _url_extension("https://example.com/path/to/resource") == ""

    def test_empty(self):
        assert _url_extension("") == ""


# ------------------------------------------------------------------
# Filename generation
# ------------------------------------------------------------------


class TestDocFilename:
    def test_with_type(self):
        doc = {"url": "https://example.com/file.pdf", "type": "agenda"}
        assert _doc_filename(doc) == "agenda.pdf"

    def test_with_meta_id(self):
        doc = {"url": "https://example.com/MetaViewer.php?meta_id=12345", "type": "staff_report"}
        assert _doc_filename(doc) == "staff_report-meta-12345.pdf"

    def test_with_title_only(self):
        doc = {"url": "https://example.com/file.pdf", "title": "Budget Report Q1"}
        assert _doc_filename(doc) == "budget-report-q1.pdf"

    def test_fallback_to_url_name(self):
        doc = {"url": "https://example.com/reports/annual-2025.pdf"}
        assert _doc_filename(doc) == "annual-2025.pdf"


class TestMediaFilename:
    def test_video_mp4(self):
        media = {"url": "https://example.com/video.mp4", "type": "video"}
        assert _media_filename(media) == "video.mp4"

    def test_audio(self):
        media = {"url": "https://example.com/audio.mp3", "type": "audio"}
        assert _media_filename(media) == "audio.mp3"


class TestUniqueFilename:
    def test_unique(self):
        used = set()
        assert _unique_filename("agenda.pdf", used) == "agenda.pdf"
        assert "agenda.pdf" in used

    def test_collision(self):
        used = {"agenda.pdf"}
        assert _unique_filename("agenda.pdf", used) == "agenda-2.pdf"

    def test_multiple_collisions(self):
        used = {"agenda.pdf", "agenda-2.pdf"}
        assert _unique_filename("agenda.pdf", used) == "agenda-3.pdf"


class TestMeetingClipSlug:
    def test_granicus_identifier(self):
        meeting = {
            "identifiers": [
                {"scheme": "granicus:erie", "identifier": "6627"},
            ],
        }
        assert _meeting_clip_slug(meeting) == "clip-6627"

    def test_media_id_fallback(self):
        meeting = {
            "identifiers": [],
            "media": [{"id": "clip-999"}],
        }
        assert _meeting_clip_slug(meeting) == "clip-999"

    def test_org_date_fallback(self):
        meeting = {
            "identifiers": [],
            "media": [],
            "organization": {"name": "City Council"},
            "start_date": "2025-03-15T18:00:00",
        }
        assert _meeting_clip_slug(meeting) == "city-council-2025-03-15"


# ------------------------------------------------------------------
# Full download_meeting_files integration test
# ------------------------------------------------------------------


class TestDownloadMeetingFiles:
    def _make_meeting(self):
        return {
            "identifiers": [
                {"scheme": "granicus:test", "identifier": "100"},
            ],
            "documents": [
                {
                    "title": "Agenda",
                    "url": "https://example.com/agenda.pdf",
                    "type": "agenda",
                },
                {
                    "title": "Video Attachment",
                    "url": "https://example.com/big.mp4",
                    "type": "attachment",
                },
            ],
            "agenda_items": [
                {
                    "title": "Item 1",
                    "documents": [
                        {
                            "title": "Staff Report",
                            "url": "https://example.com/report.pdf",
                            "type": "staff_report",
                        },
                    ],
                    "items": [
                        {
                            "title": "Sub-item 1.a",
                            "documents": [
                                {
                                    "title": "Exhibit A",
                                    "url": "https://example.com/exhibit.pdf",
                                    "type": "attachment",
                                },
                            ],
                        },
                    ],
                },
            ],
            "media": [
                {
                    "id": "clip-100",
                    "url": "https://example.com/stream.m3u8",
                    "type": "video",
                    "captions": {
                        "url": "https://example.com/captions.vtt",
                    },
                },
            ],
        }

    def test_downloads_docs_and_captions_skips_media(self, tmp_path):
        """With default config, PDFs and VTT are downloaded, MP4/M3U8 are not."""
        meeting = self._make_meeting()
        config = DownloadConfig(enabled=True)

        downloaded_urls = []

        def fake_get(url, **kwargs):
            downloaded_urls.append(url)
            resp = MagicMock()
            resp.iter_content.return_value = [b"fake content"]
            resp.raise_for_status.return_value = None
            return resp

        session = MagicMock()
        session.get = fake_get

        result = download_meeting_files(meeting, config, session, tmp_path)

        # PDFs and VTT should have been downloaded
        assert "https://example.com/agenda.pdf" in downloaded_urls
        assert "https://example.com/report.pdf" in downloaded_urls
        assert "https://example.com/exhibit.pdf" in downloaded_urls
        assert "https://example.com/captions.vtt" in downloaded_urls

        # Large media files should NOT have been downloaded
        assert "https://example.com/big.mp4" not in downloaded_urls
        assert "https://example.com/stream.m3u8" not in downloaded_urls

        # local_path should be set on downloaded items
        assert result["documents"][0].get("local_path")
        assert result["documents"][1].get("local_path") is None  # mp4 skipped

        # Captions should have local_path
        assert result["media"][0]["captions"].get("local_path")

        # Media url should NOT have local_path (m3u8 excluded)
        assert result["media"][0].get("local_path") is None

        # Nested agenda item docs should have local_path
        assert result["agenda_items"][0]["documents"][0].get("local_path")
        assert result["agenda_items"][0]["items"][0]["documents"][0].get("local_path")

    def test_include_filter_limits_downloads(self, tmp_path):
        """With include_types={.vtt}, only VTT is downloaded."""
        meeting = self._make_meeting()
        config = DownloadConfig(enabled=True, include_types={".vtt"})

        downloaded_urls = []

        def fake_get(url, **kwargs):
            downloaded_urls.append(url)
            resp = MagicMock()
            resp.iter_content.return_value = [b"fake"]
            resp.raise_for_status.return_value = None
            return resp

        session = MagicMock()
        session.get = fake_get

        download_meeting_files(meeting, config, session, tmp_path)

        assert downloaded_urls == ["https://example.com/captions.vtt"]

    def test_files_written_to_disk(self, tmp_path):
        """Downloaded files actually appear on disk."""
        meeting = self._make_meeting()
        config = DownloadConfig(enabled=True, include_types={".pdf"})

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.iter_content.return_value = [b"%PDF-1.4 fake"]
            resp.raise_for_status.return_value = None
            return resp

        session = MagicMock()
        session.get = fake_get

        download_meeting_files(meeting, config, session, tmp_path)

        files_dir = tmp_path / "files" / "clip-100"
        assert files_dir.exists()
        pdf_files = list(files_dir.glob("*.pdf"))
        assert len(pdf_files) == 3  # agenda, report, exhibit

    def test_skips_already_downloaded(self, tmp_path):
        """If a file already exists on disk, it's not re-downloaded."""
        meeting = {
            "identifiers": [{"scheme": "granicus:test", "identifier": "200"}],
            "documents": [
                {"title": "Agenda", "url": "https://example.com/agenda.pdf", "type": "agenda"},
            ],
            "agenda_items": [],
            "media": [],
        }
        config = DownloadConfig(enabled=True)

        # Pre-create the file
        files_dir = tmp_path / "files" / "clip-200"
        files_dir.mkdir(parents=True)
        (files_dir / "agenda.pdf").write_bytes(b"existing")

        session = MagicMock()
        # get should NOT be called
        result = download_meeting_files(meeting, config, session, tmp_path)

        session.get.assert_not_called()
        assert result["documents"][0]["local_path"] == "files/clip-200/agenda.pdf"
