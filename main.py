#!/usr/bin/env python3
"""CLI for scraping Zillow and analyzing rental investment properties."""

from __future__ import annotations

import argparse
import sys

from scraper.zillow import ZillowScraper
from scraper.db import (
    get_connection, init_db,
    upsert_property, upsert_properties, query_properties, get_stats,
    get_recently_enriched_addresses, get_recently_typed_addresses,
    load_enrichment_data,
)
from scraper.config import load_config, resolve_scrape_options


def cmd_scrape(args):
    """Scrape Zillow listings for a location."""
    conn = get_connection()
    init_db(conn)

    config = load_config()
    o = resolve_scrape_options(config, args)
    location = o["location"]
    start_url = o["start_url"]
    listing_type = o["type"]
    pages = o["pages"]

    def save_page(props):
        for p in props:
            p.search_name = location
        upsert_properties(conn, props)

    def save_enriched(prop):
        upsert_property(conn, prop)
        conn.commit()

    with ZillowScraper() as scraper:
        if listing_type in ("sale", "both"):
            print(f"\n--- Scraping FOR SALE listings: {start_url or location} ---")
            sale_props = scraper.search(location, "sale", max_pages=pages, start_url=start_url, on_page=save_page)
            for p in sale_props:
                p.search_name = location
            if sale_props:
                if o["enrich"]:
                    skip_addresses = set()
                    if o["skip_recent"]:
                        skip_addresses = get_recently_enriched_addresses(conn, o["skip_recent"])
                        if skip_addresses:
                            print(f"[*] Skipping {len(skip_addresses)} properties enriched in the last {o['skip_recent']} day(s)")
                        # Pre-populate enrichment fields from DB for skipped properties
                        load_enrichment_data(conn, sale_props)
                    scraper.enrich_properties(sale_props, skip_addresses=skip_addresses, on_enrich=save_enriched)
                print(f"Saved {len(sale_props)} sale listings")

        if listing_type in ("rent", "both"):
            print(f"\n--- Scraping RENTAL listings: {start_url or location} ---")
            rent_props = scraper.search(location, "rent", max_pages=pages, start_url=start_url, on_page=save_page)
            for p in rent_props:
                p.search_name = location
            if rent_props:
                if o["filter_rentals"]:
                    skip_addresses = set()
                    if o["skip_recent"]:
                        skip_addresses = get_recently_typed_addresses(conn, o["skip_recent"])
                        if skip_addresses:
                            print(f"[*] Skipping {len(skip_addresses)} rentals typed in the last {o['skip_recent']} day(s)")
                        load_enrichment_data(conn, rent_props)
                    rent_props = scraper.enrich_and_filter_rentals(rent_props, skip_addresses=skip_addresses, on_enrich=save_enriched)
                print(f"Saved {len(rent_props)} rental listings")

    stats = get_stats(conn)
    print(f"\nDatabase totals: {stats['total']} properties ({stats['for_sale']} sale, {stats['for_rent']} rent)")
    conn.close()


def cmd_stats(args):
    """Show database statistics."""
    conn = get_connection()
    init_db(conn)
    stats = get_stats(conn, city=args.city)

    print(f"\n--- Database Statistics ---")
    print(f"Total properties:    {stats['total']}")
    print(f"For sale:            {stats['for_sale']}")
    print(f"For rent:            {stats['for_rent']}")
    print(f"Distinct locations:  {stats['locations']}")
    if stats['avg_sale_price']:
        print(f"Avg sale price:      ${stats['avg_sale_price']:,.0f}")
    if stats['avg_rent']:
        print(f"Avg listed rent:     ${stats['avg_rent']:,.0f}/mo")
    if stats['avg_rent_zestimate']:
        print(f"Avg rent zestimate:  ${stats['avg_rent_zestimate']:,.0f}/mo")

    conn.close()


def cmd_export(args):
    """Export properties to CSV."""
    import csv

    conn = get_connection()
    init_db(conn)

    rows = query_properties(
        conn,
        listing_type=args.type,
        city=args.city,
        zipcode=args.zipcode,
    )

    if not rows:
        print("No matching properties found.")
        conn.close()
        return

    output = args.output or "export.csv"
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} properties to {output}")
    conn.close()


def cmd_scrape_all(args):
    """Run every saved search in config.yaml sequentially."""
    config = load_config()
    searches = config.get("searches", {})
    if not searches:
        print("No searches found in config.yaml.")
        return

    keys = list(searches.keys())
    print(f"Running {len(keys)} search(es): {', '.join(keys)}\n")
    for key in keys:
        print(f"{'='*60}")
        print(f"Search: {key}")
        print(f"{'='*60}")
        args.location = key
        cmd_scrape(args)
        print()


def cmd_chart(args):
    """Generate price vs. estimated rent chart for one or more zip codes."""
    from pathlib import Path
    from scraper.db import get_connection, init_db
    from analysis.chart import build

    conn = get_connection()
    init_db(conn)
    try:
        out = Path(args.output) if args.output else None
        build(conn, args.zip, model=args.model, output_path=out, open_browser=not args.no_browser)
    finally:
        conn.close()


def cmd_analyze_rents(args):
    """Train OLS rent model and estimate rent for all sale listings."""
    from scraper.db import get_connection, init_db
    from analysis.rent_model import run as run_model

    conn = get_connection()
    init_db(conn)
    try:
        summary = run_model(conn)
        print(f"\nModel '{args.model}': trained on {summary['trained_on']} rentals, "
              f"MAE ${summary['mae']:,.0f}/mo, R² {summary['r2']:.3f}, "
              f"{summary['predicted']} sale listings estimated.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Zillow scraper for rental investment analysis"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- scrape ---
    p_scrape = subparsers.add_parser("scrape", help="Scrape Zillow listings")
    p_scrape.add_argument(
        "location",
        help="Saved search name from config.yaml, a full Zillow URL, or a location slug (e.g. 'austin-tx')"
    )
    p_scrape.add_argument(
        "-t", "--type", choices=["sale", "rent", "both"], default=None,
        help="Listing type to scrape — overrides config (default: both)"
    )
    p_scrape.add_argument(
        "-p", "--pages", type=int, default=None,
        help="Max pages to scrape per listing type — overrides config (default: 5, Zillow caps at 20)"
    )
    p_scrape.add_argument(
        "-e", "--enrich", action="store_true", default=None,
        help="Fetch detail pages for sale listings to get rent Zestimates (slower)"
    )
    p_scrape.add_argument(
        "--skip-recent", type=int, metavar="DAYS", default=None,
        help="Skip enriching properties already updated within the last N days"
    )
    p_scrape.add_argument(
        "-f", "--filter-rentals", action="store_true", default=None,
        help="Enrich rental listings to get property type and keep only SFH/duplexes"
    )
    p_scrape.set_defaults(func=cmd_scrape)

    # --- scrape-all ---
    p_scrape_all = subparsers.add_parser("scrape-all", help="Run all saved searches from config.yaml")
    p_scrape_all.add_argument(
        "-t", "--type", choices=["sale", "rent", "both"], default=None,
        help="Override listing type for all searches"
    )
    p_scrape_all.add_argument(
        "-p", "--pages", type=int, default=None,
        help="Override max pages for all searches"
    )
    p_scrape_all.add_argument(
        "-e", "--enrich", action="store_true", default=None,
        help="Override enrich setting for all searches"
    )
    p_scrape_all.add_argument(
        "--skip-recent", type=int, metavar="DAYS", default=None,
        help="Override skip-recent days for all searches"
    )
    p_scrape_all.add_argument(
        "-f", "--filter-rentals", action="store_true", default=None,
        help="Override filter-rentals setting for all searches"
    )
    p_scrape_all.set_defaults(func=cmd_scrape_all)

    # --- analyze-rents ---
    p_ar = subparsers.add_parser("analyze-rents", help="Estimate rent for sale listings using OLS model")
    p_ar.add_argument("--model", default="ols_v1", help="Model name tag stored in rent_estimates (default: ols_v1)")
    p_ar.set_defaults(func=cmd_analyze_rents)

    # --- chart ---
    p_chart = subparsers.add_parser("chart", help="Price vs. estimated rent scatter chart (Plotly HTML)")
    p_chart.add_argument("--zip", nargs="+", required=True, metavar="ZIPCODE",
                         help="One or more zip codes to include")
    p_chart.add_argument("--model", default="ols_v1", help="Which rent_estimates model to plot (default: ols_v1)")
    p_chart.add_argument("-o", "--output", default=None, help="Output HTML file path (default: data/charts/...)")
    p_chart.add_argument("--no-browser", action="store_true", help="Save file without opening browser")
    p_chart.set_defaults(func=cmd_chart)

    # --- stats ---
    p_stats = subparsers.add_parser("stats", help="Show database statistics")
    p_stats.add_argument("--city", help="Filter by city name")
    p_stats.set_defaults(func=cmd_stats)

    # --- export ---
    p_export = subparsers.add_parser("export", help="Export properties to CSV")
    p_export.add_argument("-o", "--output", help="Output file path (default: export.csv)")
    p_export.add_argument("-t", "--type", choices=["sale", "rent"], help="Filter by listing type")
    p_export.add_argument("--city", help="Filter by city name")
    p_export.add_argument("--zipcode", help="Filter by zip code")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
