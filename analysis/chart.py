"""Price vs. estimated rent scatter chart and cash-flow margin chart (Plotly HTML)."""

from __future__ import annotations

import sqlite3
import webbrowser
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go

# Reference yield lines to draw (monthly rent / price)
YIELD_LINES = {
    "0.7% rule": 0.007,
    "1% rule":   0.010,
}

BEDROOM_COLORS = {
    1: "#636EFA",
    2: "#00CC96",
    3: "#EF553B",
    4: "#AB63FA",
    5: "#FFA15A",
}
DEFAULT_COLOR = "#19D3F3"

# Injected into every chart HTML: click a point to open its Zillow URL.
# Scans customdata for the first http* string so it works across both chart types.
_CLICK_TO_OPEN_JS = """
var _plot = document.getElementsByClassName('plotly-graph-div')[0];
_plot.on('plotly_click', function(data) {
    var pt = data.points[0];
    if (!pt.customdata) return;
    for (var i = 0; i < pt.customdata.length; i++) {
        var v = pt.customdata[i];
        if (typeof v === 'string' && v.startsWith('http')) {
            window.open(v, '_blank');
            return;
        }
    }
});
"""


def _monthly_mortgage(price: float, down_pct: float, rate_annual: float, years: int) -> float:
    principal = price * (1 - down_pct)
    r = rate_annual / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def _load_data(conn: sqlite3.Connection, zipcodes: list[str], model: str, blacklist: list[str]) -> pd.DataFrame:
    zip_placeholders = ",".join("?" * len(zipcodes))
    bl_clause = ""
    bl_params: list = []
    if blacklist:
        bl_placeholders = ",".join("?" * len(blacklist))
        bl_clause = f"AND p.address NOT IN ({bl_placeholders})"
        bl_params = list(blacklist)
    return pd.read_sql(
        f"""SELECT
                p.rowid, p.zpid, p.address, p.city, p.zipcode,
                p.price, p.sqft, p.bedrooms, p.bathrooms,
                p.detail_url, p.rent_zestimate,
                re.estimated_rent, re.n_comps
            FROM properties p
            LEFT JOIN rent_estimates re
                ON p.zpid = re.zpid AND re.model = ?
            WHERE p.listing_type = 'sale'
              AND p.zipcode IN ({zip_placeholders})
              AND p.price IS NOT NULL AND p.price > 0
              AND p.sqft  IS NOT NULL AND p.sqft  > 0
              AND p.bedrooms IS NOT NULL
              {bl_clause}
            ORDER BY p.price""",
        conn,
        params=[model] + zipcodes + bl_params,
    )


def build(
    conn: sqlite3.Connection,
    zipcodes: list[str],
    model: str = "ols_v1",
    blacklist: Optional[list] = None,
    output_path: Optional[Path] = None,
    open_browser: bool = True,
) -> Path:
    df = _load_data(conn, zipcodes, model, blacklist or [])

    if df.empty:
        raise ValueError(f"No sale listings found for zip(s): {', '.join(zipcodes)}")

    zip_label = ", ".join(sorted(set(df["zipcode"])))
    n_with_estimate = df["estimated_rent"].notna().sum()
    n_with_zestimate = df["rent_zestimate"].notna().sum()
    print(f"[*] {len(df)} listings | {n_with_estimate} OLS estimates | {n_with_zestimate} Zillow zestimates")

    fig = go.Figure()

    # --- Reference yield lines ---
    if not df["price"].empty:
        price_range = [df["price"].min() * 0.9, df["price"].max() * 1.05]
        for label, rate in YIELD_LINES.items():
            y_vals = [p * rate for p in price_range]
            fig.add_trace(go.Scatter(
                x=price_range, y=y_vals,
                mode="lines",
                name=label,
                line=dict(dash="dash", width=1.5, color="gray"),
                hoverinfo="skip",
            ))

    # --- OLS estimates — one trace per bedroom count ---
    ols_df = df[df["estimated_rent"].notna()].copy()
    ols_df["gross_yield_pct"] = (ols_df["estimated_rent"] * 12 / ols_df["price"] * 100).round(2)

    for beds in sorted(ols_df["bedrooms"].unique()):
        sub = ols_df[ols_df["bedrooms"] == beds]
        color = BEDROOM_COLORS.get(int(beds), DEFAULT_COLOR)

        hover = (
            "<b>%{customdata[0]}</b><br>"
            "Price: $%{x:,.0f}<br>"
            "OLS rent: $%{y:,.0f}/mo<br>"
            "Gross yield: %{customdata[1]:.2f}%<br>"
            "Beds: %{customdata[2]} | Baths: %{customdata[3]} | Sqft: %{customdata[4]:,}<br>"
            "Zip: %{customdata[5]} | Comps used: %{customdata[6]}<br>"
            "Row ID: %{customdata[8]}<br>"
            "<i>Click to open Zillow listing</i>"
            "<extra></extra>"
        )
        fig.add_trace(go.Scatter(
            x=sub["price"],
            y=sub["estimated_rent"],
            mode="markers",
            name=f"{int(beds)} bed (OLS)",
            marker=dict(color=color, size=8, opacity=0.8),
            customdata=sub[[
                "address", "gross_yield_pct", "bedrooms", "bathrooms",
                "sqft", "zipcode", "n_comps", "detail_url", "rowid",
            ]].values,
            hovertemplate=hover,
        ))

    # --- Zillow zestimates (diamond markers, semi-transparent) ---
    zest_df = df[df["rent_zestimate"].notna()].copy()
    if not zest_df.empty:
        zest_df["gross_yield_zest_pct"] = (zest_df["rent_zestimate"] * 12 / zest_df["price"] * 100).round(2)
        hover_z = (
            "<b>%{customdata[0]}</b><br>"
            "Price: $%{x:,.0f}<br>"
            "Zillow zestimate: $%{y:,.0f}/mo<br>"
            "Gross yield: %{customdata[1]:.2f}%<br>"
            "Beds: %{customdata[2]} | Sqft: %{customdata[3]:,}<br>"
            "<extra></extra>"
        )
        fig.add_trace(go.Scatter(
            x=zest_df["price"],
            y=zest_df["rent_zestimate"],
            mode="markers",
            name="Zillow zestimate",
            marker=dict(symbol="diamond", color="gold", size=7, opacity=0.6,
                        line=dict(color="darkgoldenrod", width=1)),
            customdata=zest_df[[
                "address", "gross_yield_zest_pct", "bedrooms", "sqft",
            ]].values,
            hovertemplate=hover_z,
        ))

    fig.update_layout(
        title=f"Purchase Price vs. Estimated Monthly Rent — Zip {zip_label}",
        xaxis_title="Purchase Price ($)",
        yaxis_title="Estimated Monthly Rent ($/mo)",
        xaxis=dict(tickformat="$,.0f"),
        yaxis=dict(tickformat="$,.0f"),
        hovermode="closest",
        legend=dict(orientation="v", x=1.01, y=1),
        height=650,
        template="plotly_white",
    )

    if output_path is None:
        out_dir = Path(__file__).parent.parent / "data" / "charts"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = "_".join(sorted(zipcodes))
        output_path = out_dir / f"price_vs_rent_{slug}.html"

    fig.write_html(str(output_path), include_plotlyjs="cdn", post_script=_CLICK_TO_OPEN_JS)
    print(f"[*] Chart saved to {output_path}")

    if open_browser:
        webbrowser.open(f"file://{output_path.resolve()}")

    return output_path


def _load_data_with_costs(conn: sqlite3.Connection, zipcodes: list[str], model: str, blacklist: list[str]) -> pd.DataFrame:
    zip_placeholders = ",".join("?" * len(zipcodes))
    bl_clause = ""
    bl_params: list = []
    if blacklist:
        bl_placeholders = ",".join("?" * len(blacklist))
        bl_clause = f"AND p.address NOT IN ({bl_placeholders})"
        bl_params = list(blacklist)
    return pd.read_sql(
        f"""SELECT
                p.rowid, p.zpid, p.address, p.city, p.zipcode,
                p.price, p.sqft, p.bedrooms, p.bathrooms,
                p.detail_url, p.rent_zestimate,
                p.hoa_fee, p.annual_tax, p.annual_homeowners_insurance,
                re.estimated_rent, re.n_comps
            FROM properties p
            LEFT JOIN rent_estimates re
                ON p.zpid = re.zpid AND re.model = ?
            WHERE p.listing_type = 'sale'
              AND p.zipcode IN ({zip_placeholders})
              AND p.price IS NOT NULL AND p.price > 0
              AND p.sqft  IS NOT NULL AND p.sqft  > 0
              AND p.bedrooms IS NOT NULL
              {bl_clause}
            ORDER BY p.price""",
        conn,
        params=[model] + zipcodes + bl_params,
    )


def build_margin(
    conn: sqlite3.Connection,
    zipcodes: list[str],
    model: str = "ols_v1",
    down_pct: float = 0.20,
    rate: float = 0.07,
    years: int = 30,
    tax_rate: float = 0.012,
    insurance_rate: float = 0.005,
    blacklist: Optional[list] = None,
    output_path: Optional[Path] = None,
    open_browser: bool = True,
) -> Path:
    """Cash-flow margin chart: estimated rent minus full monthly ownership cost."""
    df = _load_data_with_costs(conn, zipcodes, model, blacklist or [])

    if df.empty:
        raise ValueError(f"No sale listings found for zip(s): {', '.join(zipcodes)}")

    zip_label = ", ".join(sorted(set(df["zipcode"])))
    n_with_estimate = df["estimated_rent"].notna().sum()
    n_with_zestimate = df["rent_zestimate"].notna().sum()
    print(f"[*] {len(df)} listings | {n_with_estimate} OLS estimates | {n_with_zestimate} Zillow zestimates")

    # Monthly cost components
    df["mortgage"] = df["price"].apply(lambda p: _monthly_mortgage(p, down_pct, rate, years))
    df["monthly_tax"] = df.apply(
        lambda r: r["annual_tax"] / 12 if pd.notna(r["annual_tax"]) and r["annual_tax"] > 0
                  else r["price"] * tax_rate / 12,
        axis=1,
    )
    df["monthly_insurance"] = df.apply(
        lambda r: r["annual_homeowners_insurance"] / 12
                  if pd.notna(r["annual_homeowners_insurance"]) and r["annual_homeowners_insurance"] > 0
                  else r["price"] * insurance_rate / 12,
        axis=1,
    )
    df["monthly_hoa"] = df["hoa_fee"].fillna(0)
    df["total_cost"] = df["mortgage"] + df["monthly_tax"] + df["monthly_insurance"] + df["monthly_hoa"]

    fig = go.Figure()

    # Break-even line
    price_range = [df["price"].min() * 0.9, df["price"].max() * 1.05]
    fig.add_trace(go.Scatter(
        x=price_range, y=[0, 0],
        mode="lines",
        name="Break-even",
        line=dict(dash="dash", width=2, color="red"),
        hoverinfo="skip",
    ))

    # OLS margin — one trace per bedroom count
    ols_df = df[df["estimated_rent"].notna()].copy()
    ols_df["margin"] = ols_df["estimated_rent"] - ols_df["total_cost"]

    for beds in sorted(ols_df["bedrooms"].unique()):
        sub = ols_df[ols_df["bedrooms"] == beds]
        color = BEDROOM_COLORS.get(int(beds), DEFAULT_COLOR)
        hover = (
            "<b>%{customdata[0]}</b><br>"
            "Price: $%{x:,.0f}<br>"
            "<b>Margin: $%{y:,.0f}/mo</b><br>"
            "---<br>"
            "Rent (OLS): $%{customdata[1]:,.0f}/mo<br>"
            "Mortgage: $%{customdata[2]:,.0f} | Tax: $%{customdata[3]:,.0f} | "
            "Ins: $%{customdata[4]:,.0f} | HOA: $%{customdata[5]:,.0f}<br>"
            "Total cost: $%{customdata[6]:,.0f}/mo<br>"
            "Beds: %{customdata[7]} | Baths: %{customdata[8]} | Sqft: %{customdata[9]:,}<br>"
            "Zip: %{customdata[10]} | Comps: %{customdata[11]}<br>"
            "Row ID: %{customdata[13]}<br>"
            "<i>Click to open Zillow listing</i>"
            "<extra></extra>"
        )
        fig.add_trace(go.Scatter(
            x=sub["price"],
            y=sub["margin"],
            mode="markers",
            name=f"{int(beds)} bed (OLS)",
            marker=dict(color=color, size=8, opacity=0.8),
            customdata=sub[[
                "address", "estimated_rent", "mortgage", "monthly_tax",
                "monthly_insurance", "monthly_hoa", "total_cost",
                "bedrooms", "bathrooms", "sqft", "zipcode", "n_comps", "detail_url", "rowid",
            ]].values,
            hovertemplate=hover,
        ))

    # Zillow zestimate margin
    zest_df = df[df["rent_zestimate"].notna()].copy()
    if not zest_df.empty:
        zest_df["margin_zest"] = zest_df["rent_zestimate"] - zest_df["total_cost"]
        hover_z = (
            "<b>%{customdata[0]}</b><br>"
            "Price: $%{x:,.0f}<br>"
            "<b>Margin: $%{y:,.0f}/mo</b><br>"
            "Rent (Zillow): $%{customdata[1]:,.0f}/mo<br>"
            "Total cost: $%{customdata[2]:,.0f}/mo<br>"
            "<extra></extra>"
        )
        fig.add_trace(go.Scatter(
            x=zest_df["price"],
            y=zest_df["margin_zest"],
            mode="markers",
            name="Zillow zestimate",
            marker=dict(symbol="diamond", color="gold", size=7, opacity=0.6,
                        line=dict(color="darkgoldenrod", width=1)),
            customdata=zest_df[["address", "rent_zestimate", "total_cost"]].values,
            hovertemplate=hover_z,
        ))

    subtitle = (
        f"{down_pct*100:.0f}% down, {rate*100:.2g}% rate, {years}yr fixed — "
        f"tax/ins from DB where available, else {tax_rate*100:.1f}%/{insurance_rate*100:.1f}% of price"
    )
    fig.update_layout(
        title=f"Monthly Cash Flow Margin — Zip {zip_label}<br><sup>{subtitle}</sup>",
        xaxis_title="Purchase Price ($)",
        yaxis_title="Monthly Margin: Rent − (Mortgage + HOA + Tax + Ins) ($/mo)",
        xaxis=dict(tickformat="$,.0f"),
        yaxis=dict(tickformat="$,.0f", zeroline=True, zerolinecolor="crimson", zerolinewidth=1),
        hovermode="closest",
        legend=dict(orientation="v", x=1.01, y=1),
        height=650,
        template="plotly_white",
    )

    if output_path is None:
        out_dir = Path(__file__).parent.parent / "data" / "charts"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = "_".join(sorted(zipcodes))
        output_path = out_dir / f"margin_{slug}.html"

    fig.write_html(str(output_path), include_plotlyjs="cdn", post_script=_CLICK_TO_OPEN_JS)
    print(f"[*] Margin chart saved to {output_path}")

    if open_browser:
        webbrowser.open(f"file://{output_path.resolve()}")

    return output_path
