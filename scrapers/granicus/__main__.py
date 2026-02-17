#!/usr/bin/env python3
"""CLI entry point for the Granicus scraper.

Usage:
    # Scrape a known site
    python -m scrapers.granicus erie
    python -m scrapers.granicus simi-valley --start-date 2025-01-01

    # Scrape a custom site by subdomain (auto-discovers view_id=1)
    python -m scrapers.granicus --subdomain mytown --view-id 3 --body "City Council"

    # List known sites
    python -m scrapers.granicus --list-sites

    # Scrape all known sites
    python -m scrapers.granicus --all --start-date 2025-01-01
"""

import argparse
import logging
import sys

from scrapers.granicus.scraper import GranicusScraper, GranicusSite
from scrapers.granicus.sites import ALL_SITES


def main():
    parser = argparse.ArgumentParser(
        description="Scrape meeting data from Granicus Classic sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Site selection
    parser.add_argument(
        "site",
        nargs="?",
        help="Name of a known site (e.g., 'erie', 'simi-valley'). Use --list-sites to see all.",
    )
    parser.add_argument("--list-sites", action="store_true", help="List all known sites and exit.")
    parser.add_argument("--all", action="store_true", help="Scrape all known sites.")

    # Custom site options
    parser.add_argument("--subdomain", help="Granicus subdomain for a custom site.")
    parser.add_argument("--view-id", type=int, action="append", dest="view_ids",
                        help="View ID to scrape (can be repeated). Requires --subdomain.")
    parser.add_argument("--body", action="append", dest="bodies",
                        help="Body name for each --view-id (in order). Defaults to 'Meeting'.")

    # Date filtering
    parser.add_argument("--start-date", help="Only scrape meetings on or after this date (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="Only scrape meetings on or before this date (YYYY-MM-DD).")

    # Output
    parser.add_argument("-o", "--output-dir", default="output", help="Output directory (default: output).")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default: 1.0).")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_sites:
        print("Known Granicus sites:\n")
        for name, site in sorted(ALL_SITES.items()):
            views = ", ".join(f"{vid} ({bname})" for vid, bname in site.views.items())
            print(f"  {name:20s}  {site.subdomain}.granicus.com  views: {views}")
        return

    sites_to_scrape: list[GranicusSite] = []

    if args.all:
        sites_to_scrape = list(ALL_SITES.values())
    elif args.site:
        if args.site not in ALL_SITES:
            print(f"Unknown site '{args.site}'. Use --list-sites to see options.", file=sys.stderr)
            sys.exit(1)
        sites_to_scrape = [ALL_SITES[args.site]]
    elif args.subdomain:
        view_ids = args.view_ids or [1]
        bodies = args.bodies or ["Meeting"] * len(view_ids)
        if len(bodies) < len(view_ids):
            bodies.extend(["Meeting"] * (len(view_ids) - len(bodies)))
        views = dict(zip(view_ids, bodies))

        site = GranicusSite(
            subdomain=args.subdomain,
            jurisdiction_id=f"ocd-jurisdiction/country:us/custom:{args.subdomain}/government",
            jurisdiction_name=args.subdomain.replace("-", " ").title(),
            views=views,
        )
        sites_to_scrape = [site]
    else:
        parser.print_help()
        sys.exit(1)

    total = 0
    for site in sites_to_scrape:
        out = f"{args.output_dir}/{site.subdomain}"
        print(f"\n{'='*60}")
        print(f"Scraping {site.jurisdiction_name} ({site.subdomain}.granicus.com)")
        print(f"Output:  {out}")
        print(f"{'='*60}\n")

        scraper = GranicusScraper(site, output_dir=out, request_delay=args.delay)
        paths = scraper.scrape_all(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        total += len(paths)
        for p in paths:
            print(f"  -> {p}")

    print(f"\nDone. {total} meeting(s) scraped.")


if __name__ == "__main__":
    main()
