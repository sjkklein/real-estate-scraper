"""Price vs. estimated rent scatter chart (Plotly HTML)."""

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


def _load_data(conn: sqlite3.Connection, zipcodes: list[str], model: str) -> pd.DataFrame:
    placeholders = ",".join("?" * len(zipcodes))
    return pd.read_sql(
        f"""SELECT
                p.zpid, p.address, p.city, p.zipcode,
                p.price, p.sqft, p.bedrooms, p.bathrooms,
                p.detail_url, p.rent_zestimate,
                re.estimated_rent, re.n_comps
            FROM properties p
            LEFT JOIN rent_estimates re
                ON p.zpid = re.zpid AND re.model = ?
            WHERE p.listing_type = 'sale'
              AND p.zipcode IN ({placeholders})
              AND p.price IS NOT NULL AND p.price > 0
              AND p.sqft  IS NOT NULL AND p.sqft  > 0
              AND p.bedrooms IS NOT NULL
            ORDER BY p.price""",
        conn,
        params=[model] + zipcodes,
    )


def build(
    conn: sqlite3.Connection,
    zipcodes: list[str],
    model: str = "ols_v1",
    output_path: Optional[Path] = None,
    open_browser: bool = True,
) -> Path:
    df = _load_data(conn, zipcodes, model)

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
            "<a href='%{customdata[7]}'>Zillow listing ↗</a>"
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
                "sqft", "zipcode", "n_comps", "detail_url",
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

    fig.write_html(str(output_path), include_plotlyjs="cdn")
    print(f"[*] Chart saved to {output_path}")

    if open_browser:
        webbrowser.open(f"file://{output_path.resolve()}")

    return output_path
