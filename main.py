#!/usr/bin/env python3
"""CLI for scraping Zillow and analyzing rental investment properties."""

from __future__ import annotations

import argparse
import sys

from scraper.zillow import ZillowScraper
from scraper.db import (
    get_connection, init_db, load_district_data,
    upsert_properties, query_properties, get_stats,
)
from scraper.analysis import (
    analyze_from_db, print_analysis,
    rent_bias_analysis, print_bias_report,
)


def cmd_scrape(args):
    """Scrape Zillow listings for a location."""
    conn = get_connection()
    init_db(conn)

    with ZillowScraper() as scraper:
        if args.type in ("sale", "both"):
            print(f"\n--- Scraping FOR SALE listings in {args.location} ---")
            sale_props = scraper.search(args.location, "sale", max_pages=args.pages)
            if sale_props:
                if args.enrich:
                    scraper.enrich_properties(sale_props)
                upsert_properties(conn, sale_props)
                print(f"Saved {len(sale_props)} sale listings")

        if args.type in ("rent", "both"):
            print(f"\n--- Scraping RENTAL listings in {args.location} ---")
            rent_props = scraper.search(args.location, "rent", max_pages=args.pages)
            if rent_props:
                upsert_properties(conn, rent_props)
                print(f"Saved {len(rent_props)} rental listings")

    stats = get_stats(conn)
    print(f"\nDatabase totals: {stats['total']} properties ({stats['for_sale']} sale, {stats['for_rent']} rent)")
    conn.close()


def cmd_analyze(args):
    """Analyze stored properties for investment potential."""
    conn = get_connection()
    init_db(conn)
    load_district_data(conn)

    results = analyze_from_db(
        conn,
        city=args.city,
        state=args.state,
        zipcode=args.zipcode,
        district=args.district,
        min_price=args.min_price,
        max_price=args.max_price,
        expense_rate=args.expense_rate,
        down_payment_pct=args.down_payment,
        interest_rate=args.interest_rate,
    )

    print_analysis(results, top_n=args.top, group_by=args.group_by)
    conn.close()


def cmd_bias(args):
    """Run rent bias analysis comparing listings vs Zestimates."""
    conn = get_connection()
    init_db(conn)
    load_district_data(conn)

    bias = rent_bias_analysis(conn, city=args.city, district=args.district)
    print_bias_report(bias)
    conn.close()


def cmd_stats(args):
    """Show database statistics."""
    conn = get_connection()
    init_db(conn)
    load_district_data(conn)
    stats = get_stats(conn, city=args.city, district=args.district)

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
        district=args.district,
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


def main():
    parser = argparse.ArgumentParser(
        description="Zillow scraper for rental investment analysis"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- scrape ---
    p_scrape = subparsers.add_parser("scrape", help="Scrape Zillow listings")
    p_scrape.add_argument("location", help="Zillow location slug (e.g. 'austin-tx', '90210', 'tampa-fl')")
    p_scrape.add_argument(
        "-t", "--type", choices=["sale", "rent", "both"], default="both",
        help="Listing type to scrape (default: both)"
    )
    p_scrape.add_argument(
        "-p", "--pages", type=int, default=5,
        help="Max pages to scrape per listing type (default: 5)"
    )
    p_scrape.add_argument(
        "-e", "--enrich", action="store_true",
        help="Fetch detail pages for sale listings to get rent Zestimates (slower)"
    )
    p_scrape.set_defaults(func=cmd_scrape)

    # --- analyze ---
    p_analyze = subparsers.add_parser("analyze", help="Analyze properties for investment")
    p_analyze.add_argument("--city", help="Filter by city name")
    p_analyze.add_argument("--state", help="Filter by state (e.g. TX)")
    p_analyze.add_argument("--zipcode", help="Filter by zip code")
    p_analyze.add_argument("--district", help="Filter by school district name (partial match)")
    p_analyze.add_argument("--min-price", type=float, help="Minimum price")
    p_analyze.add_argument("--max-price", type=float, help="Maximum price")
    p_analyze.add_argument("--expense-rate", type=float, default=0.40, help="Expense rate (default: 0.40)")
    p_analyze.add_argument("--down-payment", type=float, default=0.20, help="Down payment pct (default: 0.20)")
    p_analyze.add_argument("--interest-rate", type=float, default=0.07, help="Mortgage rate (default: 0.07)")
    p_analyze.add_argument("--top", type=int, default=20, help="Show top N results (default: 20)")
    p_analyze.add_argument(
        "--group-by", choices=["district"], default=None,
        help="Group results by school district"
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # --- bias ---
    p_bias = subparsers.add_parser("bias", help="Compare rental listing prices vs Zestimates")
    p_bias.add_argument("--city", help="Filter by city name")
    p_bias.add_argument("--district", help="Filter by school district name")
    p_bias.set_defaults(func=cmd_bias)

    # --- stats ---
    p_stats = subparsers.add_parser("stats", help="Show database statistics")
    p_stats.add_argument("--city", help="Filter by city name")
    p_stats.add_argument("--district", help="Filter by school district name")
    p_stats.set_defaults(func=cmd_stats)

    # --- export ---
    p_export = subparsers.add_parser("export", help="Export properties to CSV")
    p_export.add_argument("-o", "--output", help="Output file path (default: export.csv)")
    p_export.add_argument("-t", "--type", choices=["sale", "rent"], help="Filter by listing type")
    p_export.add_argument("--city", help="Filter by city name")
    p_export.add_argument("--zipcode", help="Filter by zip code")
    p_export.add_argument("--district", help="Filter by school district name")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
