"""Known Granicus Classic sites.

Each entry configures a municipality that uses the classic Granicus platform
(ViewPublisher.php / GeneratedAgendaViewer.php / player/clip/).

To add a new site:
    1. Find the subdomain (e.g., "cityname" in cityname.granicus.com)
    2. Visit {subdomain}.granicus.com/ViewPublisher.php?view_id=1 and increment
       view_id to discover which bodies have content.
    3. Add a GranicusSite entry below.
"""

from scrapers.granicus.scraper import GranicusSite

# ── Colorado ──────────────────────────────────────────────────────────

ERIE_CO = GranicusSite(
    subdomain="erie",
    jurisdiction_id="ocd-jurisdiction/country:us/state:co/place:erie/government",
    jurisdiction_name="Town of Erie",
    jurisdiction_url="https://www.erieco.gov",
    views={
        3: "Board of Trustees",
    },
)

# ── California ────────────────────────────────────────────────────────

SIMI_VALLEY_CA = GranicusSite(
    subdomain="simivalley",
    jurisdiction_id="ocd-jurisdiction/country:us/state:ca/place:simi_valley/government",
    jurisdiction_name="City of Simi Valley",
    jurisdiction_url="https://www.simivalley.org",
    views={
        5: "City Council",
    },
)

SACRAMENTO_CA = GranicusSite(
    subdomain="sacramento",
    jurisdiction_id="ocd-jurisdiction/country:us/state:ca/place:sacramento/government",
    jurisdiction_name="City of Sacramento",
    jurisdiction_url="https://www.cityofsacramento.org",
    views={
        21: "City Council",
    },
)

# ── Washington ────────────────────────────────────────────────────────

KIRKLAND_WA = GranicusSite(
    subdomain="kirkland",
    jurisdiction_id="ocd-jurisdiction/country:us/state:wa/place:kirkland/government",
    jurisdiction_name="City of Kirkland",
    jurisdiction_url="https://www.kirklandwa.gov",
    views={
        54: "City Council",
    },
)

SHORELINE_WA = GranicusSite(
    subdomain="shoreline",
    jurisdiction_id="ocd-jurisdiction/country:us/state:wa/place:shoreline/government",
    jurisdiction_name="City of Shoreline",
    jurisdiction_url="https://www.shorelinewa.gov",
    views={
        1: "City Council",
    },
)

# ── Registry ──────────────────────────────────────────────────────────

ALL_SITES: dict[str, GranicusSite] = {
    "erie": ERIE_CO,
    "simi-valley": SIMI_VALLEY_CA,
    "sacramento": SACRAMENTO_CA,
    "kirkland": KIRKLAND_WA,
    "shoreline": SHORELINE_WA,
}
