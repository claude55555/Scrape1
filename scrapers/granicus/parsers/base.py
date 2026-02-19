"""Base class for Granicus agenda parsers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class ParseContext:
    """Contextual information passed to every parser.

    Parsers should NOT need to know about the scraper itself — only
    the data required to build proper URLs and media references.
    """

    base_url: str       # e.g. "https://sacramento.granicus.com"
    view_id: int
    clip_id: int


class AgendaParser(ABC):
    """Abstract base for agenda page parsers.

    Each concrete parser detects a specific HTML pattern in
    GeneratedAgendaViewer.php and extracts structured agenda items.

    Implementations should:
        - Override ``can_parse()`` to detect their HTML pattern.
        - Override ``parse()`` to extract agenda items.
        - Use helpers from ``scrapers.granicus.extraction`` for
          document/body/timestamp extraction.
    """

    @abstractmethod
    def can_parse(self, soup: BeautifulSoup) -> bool:
        """Return True if this parser can handle this page's structure.

        Should be fast — just check for the presence of key HTML
        elements/classes, not full parsing.
        """

    @abstractmethod
    def parse(self, soup: BeautifulSoup, ctx: ParseContext) -> list[dict]:
        """Extract agenda items from the page.

        Returns a list of agenda item dicts.  Each item should have
        at minimum a ``title`` and ``source`` key.  Optional keys:
        ``order``, ``level``, ``classification``, ``documents``,
        ``media_ref``, ``body``.
        """
