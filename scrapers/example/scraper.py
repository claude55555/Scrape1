"""Example scraper implementation.

This shows how to subclass BaseScraper to scrape a specific municipality's
meeting data. Copy this file as a starting point for a new scraper.
"""

import logging

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class ExampleScraper(BaseScraper):
    """Example scraper for City of Millfield (fictional).

    Replace this with the actual data source logic. Most real scrapers will
    use requests + BeautifulSoup for HTML scraping, or hit a JSON API
    (Legistar, Granicus, PrimeGov, etc.).
    """

    jurisdiction_id = "ocd-jurisdiction/country:us/state:co/place:millfield/government"
    jurisdiction_name = "City of Millfield"
    jurisdiction_url = "https://www.millfield.gov"
    source_system = "example"

    def __init__(self, output_dir="output"):
        super().__init__(output_dir)
        # Set up any session, API keys, base URLs, etc. here
        # self.base_url = "https://www.millfield.gov/api/meetings"
        # self.session = requests.Session()

    def list_meetings(self, start_date=None, end_date=None):
        """Discover available meetings.

        In a real scraper this would hit an API or scrape an index page.
        Return a list of dicts — each one gets passed to scrape_meeting().
        """
        # Example: return a hardcoded list for demonstration
        return [
            {"event_id": "12345", "date": "2025-03-15", "body": "City Council"},
        ]

    def scrape_meeting(self, meeting_ref):
        """Scrape a single meeting into the Open Civic Agenda schema.

        This is where the real work happens. Fetch the detail page / API
        endpoint and map the source data into the schema fields.
        """
        event_id = meeting_ref["event_id"]
        date = meeting_ref["date"]
        body = meeting_ref["body"]

        # In a real scraper:
        # response = self.session.get(f"{self.base_url}/{event_id}")
        # data = response.json()  (or parse HTML with BeautifulSoup)

        body_slug = body.lower().replace(" ", "-")

        meeting = {
            "id": self.make_event_id(),
            "identifiers": [
                self.make_identifier(f"{self.source_system}:millfield", event_id),
            ],
            "name": f"Regular {body} Meeting",
            "organization": {
                "id": self.make_org_id(body_slug),
                "name": body,
                "classification": "legislature",
            },
            "jurisdiction": self.jurisdiction_obj(),
            "start_date": f"{date}T18:00:00-07:00",
            "status": "completed",
            "location": {
                "name": "City Hall Council Chambers",
                "address": "100 Main Street, Millfield, CO 80000",
            },
            "agenda_items": self._scrape_agenda_items(event_id),
            "participants": self._scrape_participants(event_id),
            "documents": [],
            "media": [],
            "sources": [
                {
                    "url": f"https://www.millfield.gov/meetings/{event_id}",
                    "note": "Scraped from city website",
                }
            ],
        }

        return meeting

    def _scrape_agenda_items(self, event_id):
        """Scrape and return agenda items for a meeting.

        Break out sub-scraping tasks into helper methods like this
        to keep scrape_meeting() readable.
        """
        # Placeholder — real implementation would parse actual agenda data
        return [
            {
                "order": "1",
                "title": "Call to Order",
                "classification": "call_to_order",
            },
            {
                "order": "2",
                "title": "Roll Call",
                "classification": "roll_call",
            },
            # ... more items from actual scraping
        ]

    def _scrape_participants(self, event_id):
        """Scrape and return participants for a meeting."""
        # Placeholder
        return [
            {"name": "Sarah Chen", "role": "chair", "present": True},
        ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = ExampleScraper(output_dir="output/millfield")
    paths = scraper.scrape_all()
    for p in paths:
        print(f"  {p}")
