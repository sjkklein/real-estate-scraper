"""Investment analysis for scraped properties."""

from __future__ import annotations

import sqlite3
from statistics import median, mean
from typing import Optional

from .db import get_rental_comps, get_district


# Default assumptions for expense estimation
DEFAULT_EXPENSE_RATE = 0.40  # 40% of gross rent goes to expenses
DEFAULT_DOWN_PAYMENT_PCT = 0.20
DEFAULT_INTEREST_RATE = 0.07  # 7% mortgage rate
DEFAULT_LOAN_TERM_YEARS = 30


def monthly_mortgage(principal: float, annual_rate: float, years: int) -> float:
    """Calculate monthly mortgage payment."""
    if annual_rate == 0:
        return principal / (years * 12)
    monthly_rate = annual_rate / 12
    n_payments = years * 12
    return principal * (monthly_rate * (1 + monthly_rate) ** n_payments) / (
        (1 + monthly_rate) ** n_payments - 1
    )


def analyze_property(
    price: float,
    monthly_rent: float,
    expense_rate: float = DEFAULT_EXPENSE_RATE,
    down_payment_pct: float = DEFAULT_DOWN_PAYMENT_PCT,
    interest_rate: float = DEFAULT_INTEREST_RATE,
    loan_term: int = DEFAULT_LOAN_TERM_YEARS,
) -> dict:
    """Calculate investment metrics for a single property."""
    annual_rent = monthly_rent * 12
    gross_yield = (annual_rent / price) * 100 if price else 0

    price_to_rent = price / annual_rent if annual_rent else float("inf")
    grm = price / annual_rent if annual_rent else float("inf")

    expenses = annual_rent * expense_rate
    noi = annual_rent - expenses
    cap_rate = (noi / price) * 100 if price else 0

    down_payment = price * down_payment_pct
    loan_amount = price - down_payment
    monthly_payment = monthly_mortgage(loan_amount, interest_rate, loan_term)
    annual_debt_service = monthly_payment * 12

    annual_cash_flow = noi - annual_debt_service
    monthly_cash_flow = annual_cash_flow / 12

    closing_costs = price * 0.03
    total_cash_invested = down_payment + closing_costs
    cash_on_cash = (annual_cash_flow / total_cash_invested) * 100 if total_cash_invested else 0

    return {
        "price": price,
        "monthly_rent": monthly_rent,
        "annual_rent": annual_rent,
        "gross_yield_pct": round(gross_yield, 2),
        "price_to_rent_ratio": round(price_to_rent, 1),
        "gross_rent_multiplier": round(grm, 1),
        "noi": round(noi, 0),
        "cap_rate_pct": round(cap_rate, 2),
        "down_payment": round(down_payment, 0),
        "monthly_mortgage": round(monthly_payment, 0),
        "monthly_cash_flow": round(monthly_cash_flow, 0),
        "annual_cash_flow": round(annual_cash_flow, 0),
        "cash_on_cash_pct": round(cash_on_cash, 2),
        "total_cash_invested": round(total_cash_invested, 0),
    }


def estimate_rent_from_comps(
    conn: sqlite3.Connection,
    zipcode: str,
    bedrooms: Optional[int] = None,
) -> Optional[dict]:
    """Estimate rent for a property using nearby rental listings as comps.

    Returns dict with comp stats or None if no comps found.
    """
    # Try exact bedroom match first
    comps = get_rental_comps(conn, zipcode, bedrooms)

    # Fall back to all bedrooms in the zip if too few exact matches
    if len(comps) < 3 and bedrooms is not None:
        comps = get_rental_comps(conn, zipcode, None)

    if not comps:
        return None

    prices = [c["price"] for c in comps]
    return {
        "comp_rent_median": round(median(prices), 0),
        "comp_rent_mean": round(mean(prices), 0),
        "comp_rent_min": min(prices),
        "comp_rent_max": max(prices),
        "comp_count": len(comps),
        "comp_bedrooms_matched": bedrooms is not None and len(get_rental_comps(conn, zipcode, bedrooms)) >= 3,
    }


def analyze_from_db(
    conn: sqlite3.Connection,
    city: Optional[str] = None,
    state: Optional[str] = None,
    zipcode: Optional[str] = None,
    district: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    expense_rate: float = DEFAULT_EXPENSE_RATE,
    down_payment_pct: float = DEFAULT_DOWN_PAYMENT_PCT,
    interest_rate: float = DEFAULT_INTEREST_RATE,
) -> list[dict]:
    """Analyze sale properties using rent Zestimates and/or rental comps.

    For each sale property, rent is estimated as:
    - rent_zestimate if available
    - median of rental comps in same zip+bedrooms otherwise
    - both are provided when available for comparison

    Returns list of properties sorted by cash-on-cash return (best first).
    """
    sql = "SELECT p.* FROM properties p"
    joins = []
    where = ["p.listing_type = 'sale'", "p.price IS NOT NULL", "p.price > 0"]
    params = []

    if district:
        joins.append("JOIN zip_districts zd ON p.zipcode = zd.zipcode")
        where.append("LOWER(zd.school_district) LIKE LOWER(?)")
        params.append("%" + district + "%")
    if city:
        where.append("LOWER(p.city) = LOWER(?)")
        params.append(city)
    if state:
        where.append("UPPER(p.state) = UPPER(?)")
        params.append(state)
    if zipcode:
        where.append("p.zipcode = ?")
        params.append(zipcode)
    if min_price is not None:
        where.append("p.price >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("p.price <= ?")
        params.append(max_price)

    sql += " " + " ".join(joins) + " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()
    results = []

    for row in rows:
        row = dict(row)

        # Get rental comps
        comp_data = estimate_rent_from_comps(conn, row["zipcode"], row.get("bedrooms"))

        # Determine best rent estimate
        rent_zest = row.get("rent_zestimate")
        comp_rent = comp_data["comp_rent_median"] if comp_data else None

        # Use Zestimate if available, otherwise fall back to comps
        if rent_zest and rent_zest > 0:
            monthly_rent = rent_zest
            rent_source = "zestimate"
        elif comp_rent and comp_rent > 0:
            monthly_rent = comp_rent
            rent_source = "comps"
        else:
            continue  # Can't analyze without any rent estimate

        metrics = analyze_property(
            price=row["price"],
            monthly_rent=monthly_rent,
            expense_rate=expense_rate,
            down_payment_pct=down_payment_pct,
            interest_rate=interest_rate,
        )

        # Add school district
        sd = get_district(conn, row["zipcode"]) if row.get("zipcode") else None

        result = {
            **row,
            **metrics,
            "rent_source": rent_source,
            "comp_rent": comp_rent,
            "school_district": sd,
        }
        if comp_data:
            result.update({k: v for k, v in comp_data.items()})

        results.append(result)

    results.sort(key=lambda x: x["cash_on_cash_pct"], reverse=True)
    return results


def rent_bias_analysis(conn: sqlite3.Connection, city: Optional[str] = None, district: Optional[str] = None) -> dict:
    """Compare rental listing prices against Zestimates to measure bias.

    Looks at properties that have BOTH a rent Zestimate and rental comps
    in the same zip/bedroom combination.

    Returns summary statistics on the bias.
    """
    sql = "SELECT p.* FROM properties p"
    joins = []
    where = [
        "p.listing_type = 'sale'",
        "p.rent_zestimate IS NOT NULL",
        "p.rent_zestimate > 0",
    ]
    params = []

    if district:
        joins.append("JOIN zip_districts zd ON p.zipcode = zd.zipcode")
        where.append("LOWER(zd.school_district) LIKE LOWER(?)")
        params.append("%" + district + "%")
    if city:
        where.append("LOWER(p.city) = LOWER(?)")
        params.append(city)

    sql += " " + " ".join(joins) + " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()

    comparisons = []
    for row in rows:
        row = dict(row)
        comp_data = estimate_rent_from_comps(conn, row["zipcode"], row.get("bedrooms"))
        if not comp_data or comp_data["comp_count"] < 2:
            continue

        zest = row["rent_zestimate"]
        comp = comp_data["comp_rent_median"]
        diff = comp - zest
        diff_pct = (diff / zest) * 100 if zest else 0

        comparisons.append({
            "zipcode": row["zipcode"],
            "bedrooms": row.get("bedrooms"),
            "rent_zestimate": zest,
            "comp_rent_median": comp,
            "diff": round(diff, 0),
            "diff_pct": round(diff_pct, 1),
            "comp_count": comp_data["comp_count"],
        })

    if not comparisons:
        return {"comparisons": [], "summary": None}

    diffs = [c["diff_pct"] for c in comparisons]
    return {
        "comparisons": comparisons,
        "summary": {
            "count": len(comparisons),
            "avg_bias_pct": round(mean(diffs), 1),
            "median_bias_pct": round(median(diffs), 1),
            "min_bias_pct": round(min(diffs), 1),
            "max_bias_pct": round(max(diffs), 1),
        },
    }


def group_by_district(results: list[dict]) -> dict:
    """Group analysis results by school district."""
    groups = {}
    for r in results:
        district = r.get("school_district") or "Unknown"
        if district not in groups:
            groups[district] = []
        groups[district].append(r)
    return groups


def print_analysis(results: list[dict], top_n: int = 20, group_by: Optional[str] = None):
    """Print a formatted table of investment analysis results."""
    if not results:
        print("No properties with rent estimates (Zestimate or comps) found.")
        print("Tip: scrape rental listings too with: scrape <location> -t both")
        return

    if group_by == "district":
        _print_grouped_analysis(results, top_n)
        return

    results = results[:top_n]

    print(f"\n{'=' * 140}")
    print(
        f"{'Address':<30} {'Price':>12} {'Rent/mo':>10} {'Src':>5} "
        f"{'Cap Rate':>9} {'CoC':>8} {'Cash Flow':>10} {'P/R':>6} {'District':<28}"
    )
    print(f"{'=' * 140}")

    for r in results:
        addr = r.get("address", "")[:28]
        district = (r.get("school_district") or "")[:26]
        src = "Z" if r["rent_source"] == "zestimate" else "C"
        print(
            f"{addr:<30} "
            f"${r['price']:>11,.0f} "
            f"${r['monthly_rent']:>8,.0f} "
            f"  {src:>3} "
            f"{r['cap_rate_pct']:>8.1f}% "
            f"{r['cash_on_cash_pct']:>7.1f}% "
            f"${r['monthly_cash_flow']:>8,.0f} "
            f"{r['price_to_rent_ratio']:>6.1f} "
            f"{district:<28}"
        )

    print(f"{'=' * 140}")
    print(f"Showing top {len(results)} properties by cash-on-cash return | Src: Z=Zestimate, C=Comps")
    print(f"Assumptions: 40% expense ratio, 20% down, 7% interest, 30yr mortgage")


def _print_grouped_analysis(results: list[dict], top_n: int):
    """Print analysis grouped by school district."""
    groups = group_by_district(results)

    print(f"\n{'=' * 100}")
    print(f"{'School District':<40} {'Props':>6} {'Avg Price':>14} {'Avg Rent':>10} {'Avg Cap':>9} {'Avg CoC':>9}")
    print(f"{'=' * 100}")

    district_summaries = []
    for district, props in groups.items():
        avg_price = mean(p["price"] for p in props)
        avg_rent = mean(p["monthly_rent"] for p in props)
        avg_cap = mean(p["cap_rate_pct"] for p in props)
        avg_coc = mean(p["cash_on_cash_pct"] for p in props)
        district_summaries.append({
            "district": district,
            "count": len(props),
            "avg_price": avg_price,
            "avg_rent": avg_rent,
            "avg_cap": avg_cap,
            "avg_coc": avg_coc,
        })

    district_summaries.sort(key=lambda x: x["avg_coc"], reverse=True)

    for d in district_summaries:
        name = d["district"][:38]
        print(
            f"{name:<40} "
            f"{d['count']:>6} "
            f"${d['avg_price']:>13,.0f} "
            f"${d['avg_rent']:>8,.0f} "
            f"{d['avg_cap']:>8.1f}% "
            f"{d['avg_coc']:>8.1f}%"
        )

    print(f"{'=' * 100}")
    print(f"Districts sorted by average cash-on-cash return")


def print_bias_report(bias: dict):
    """Print the rent bias analysis report."""
    if not bias["summary"]:
        print("\nNo properties found with both Zestimate and rental comps for comparison.")
        print("Tip: scrape both sale and rental listings for the same area.")
        return

    s = bias["summary"]
    print(f"\n{'=' * 80}")
    print(f"RENT BIAS ANALYSIS: Listed Rents vs Zestimates")
    print(f"{'=' * 80}")
    print(f"Properties compared:  {s['count']}")
    print(f"Average bias:         {s['avg_bias_pct']:+.1f}% (positive = listings higher than Zestimates)")
    print(f"Median bias:          {s['median_bias_pct']:+.1f}%")
    print(f"Range:                {s['min_bias_pct']:+.1f}% to {s['max_bias_pct']:+.1f}%")
    print()

    if abs(s["median_bias_pct"]) < 5:
        print("Interpretation: Listings and Zestimates are closely aligned.")
    elif s["median_bias_pct"] > 0:
        print(f"Interpretation: Rental listings are ~{s['median_bias_pct']:.0f}% HIGHER than Zestimates.")
        print("This is expected — listed asking rents tend to be optimistic.")
        print("Consider discounting comp-based estimates by this amount for conservative analysis.")
    else:
        print(f"Interpretation: Rental listings are ~{abs(s['median_bias_pct']):.0f}% LOWER than Zestimates.")

    # Show per-zip breakdown if there are enough
    if len(bias["comparisons"]) > 1:
        print(f"\n{'Zip':<8} {'Beds':>5} {'Zestimate':>10} {'Comp Med':>10} {'Diff':>8} {'Comps':>6}")
        print("-" * 52)
        for c in sorted(bias["comparisons"], key=lambda x: x["diff_pct"], reverse=True):
            beds = str(c["bedrooms"]) if c["bedrooms"] else "?"
            print(
                f"{c['zipcode']:<8} "
                f"{beds:>5} "
                f"${c['rent_zestimate']:>8,.0f} "
                f"${c['comp_rent_median']:>8,.0f} "
                f"{c['diff_pct']:>+7.1f}% "
                f"{c['comp_count']:>6}"
            )
