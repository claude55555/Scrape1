"""Pluggable agenda page parsers for Granicus sites.

Each parser handles a specific HTML structure found in
GeneratedAgendaViewer.php pages.  The scraper tries parsers in
priority order; the first one whose ``can_parse()`` returns True
and whose ``parse()`` returns items wins.

To add support for a new city's HTML layout:
    1. Create a new parser module in this package.
    2. Implement ``can_parse(soup)`` to detect the HTML pattern.
    3. Implement ``parse(soup, ctx)`` using shared helpers from
       ``scrapers.granicus.extraction``.
    4. Register it in ``ALL_PARSERS`` below (higher priority = earlier).
"""

from scrapers.granicus.parsers.base import AgendaParser, ParseContext
from scrapers.granicus.parsers.bold import BoldParser
from scrapers.granicus.parsers.css_classes import CssClassParser
from scrapers.granicus.parsers.flat import FlatParser
from scrapers.granicus.parsers.headings import HeadingsParser
from scrapers.granicus.parsers.table import TableParser

# Ordered by specificity: most specific patterns first, generic fallbacks last.
ALL_PARSERS: list[AgendaParser] = [
    CssClassParser(),    # Sacramento-style Agenda0/1/2 CSS classes
    HeadingsParser(),    # Shoreline-style h2/h3 with span numbers
    BoldParser(),        # Generic: bold text + adjacent docs/timestamps
    TableParser(),       # Generic: table rows as items
    FlatParser(),        # Last resort: numbered lines in any structure
]

__all__ = [
    "AgendaParser",
    "ParseContext",
    "ALL_PARSERS",
    "CssClassParser",
    "HeadingsParser",
    "BoldParser",
    "TableParser",
    "FlatParser",
]
