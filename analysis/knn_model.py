"""KNN rent estimation model.

For each sale listing, find the K most similar rental comps using a
combined distance metric over two dimensions:

  1. Sqft difference — normalized by SQFT_SCALE (default 300 sqft ≈ 1 unit)
  2. Geographic distance — normalized by GEO_SCALE (default 3 miles ≈ 1 unit)

Combined distance: sqrt((sqft_diff / SQFT_SCALE)^2 + (geo_miles / GEO_SCALE)^2)

Bedroom matching:
  - First pass: exact bedroom count match
  - Second pass: ±1 bedroom if fewer than MIN_K comps found
  - Third pass: all bedroom counts within MAX_MILES if still insufficient

Estimate: distance-weighted average rent of the top-K comps (weight = 1 / dist).
Stored under model name 'knn_v1' in rent_estimates.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score

MODEL_NAME = "knn_v1"

K = 10              # comps to use for the weighted average
MIN_K = 3           # minimum acceptable comps before falling back
MAX_MILES = 25      # hard cap on search radius
SQFT_SCALE = 300.0  # sqft difference treated as 1 distance unit
GEO_SCALE = 3.0     # miles treated as 1 distance unit
TRAIN_TYPES = {"SINGLE_FAMILY", "TOWNHOUSE", "DUPLEX"}


def _haversine_miles(lat1: float, lon1: float,
                     lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    R = 3958.8
    dlat = np.radians(lats - lat1)
    dlon = np.radians(lons - lon1)
    a = (np.sin(dlat / 2) ** 2
         + np.cos(np.radians(lat1)) * np.cos(np.radians(lats))
         * np.sin(dlon / 2) ** 2)
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _weighted_avg_rent(
    target_sqft: float,
    target_lat: float | None,
    target_lon: float | None,
    comps: pd.DataFrame,
    k: int,
) -> tuple[float, int]:
    """Distance-weighted average rent from comps DataFrame.

    Returns (estimated_rent, n_comps_used).
    """
    sqft_dist = np.abs(comps["sqft"].values - target_sqft) / SQFT_SCALE

    if (target_lat is not None and target_lon is not None
            and comps["latitude"].notna().any()):
        lats = comps["latitude"].fillna(target_lat).values
        lons = comps["longitude"].fillna(target_lon).values
        geo_dist = _haversine_miles(target_lat, target_lon, lats, lons) / GEO_SCALE
    else:
        geo_dist = np.zeros(len(comps))

    combined = np.sqrt(sqft_dist ** 2 + geo_dist ** 2)

    # Take top-K closest
    idx = np.argsort(combined)[:k]
    top_comps = comps.iloc[idx]
    top_dists = combined[idx]

    weights = 1.0 / (top_dists + 0.01)
    estimate = float(np.average(top_comps["price"].values, weights=weights))
    return estimate, len(idx)


def _estimate_for_sale(row: pd.Series, rentals: pd.DataFrame,
                       global_median: float) -> tuple[float, int]:
    """Find comps and return (estimated_rent, n_comps)."""
    target_beds = row["bedrooms"]
    target_sqft = row["sqft"]
    target_lat = row.get("latitude") if pd.notna(row.get("latitude")) else None
    target_lon = row.get("longitude") if pd.notna(row.get("longitude")) else None

    # Geo filter: only use rentals within MAX_MILES (when coords available)
    if target_lat is not None and target_lon is not None:
        has_coords = rentals["latitude"].notna() & rentals["longitude"].notna()
        coords_pool = rentals[has_coords]
        if not coords_pool.empty:
            dists = _haversine_miles(target_lat, target_lon,
                                     coords_pool["latitude"].values,
                                     coords_pool["longitude"].values)
            within_radius = coords_pool[dists <= MAX_MILES]
            no_coords = rentals[~has_coords]
            geo_filtered = pd.concat([within_radius, no_coords])
        else:
            geo_filtered = rentals
    else:
        geo_filtered = rentals

    # Pass 1: exact bedroom match
    exact = geo_filtered[geo_filtered["bedrooms"] == target_beds]
    if len(exact) >= MIN_K:
        return _weighted_avg_rent(target_sqft, target_lat, target_lon, exact, K)

    # Pass 2: ±1 bedroom
    relaxed = geo_filtered[abs(geo_filtered["bedrooms"] - target_beds) <= 1]
    if len(relaxed) >= MIN_K:
        return _weighted_avg_rent(target_sqft, target_lat, target_lon, relaxed, K)

    # Pass 3: all rentals in geo radius (any bedroom count)
    if len(geo_filtered) >= MIN_K:
        return _weighted_avg_rent(target_sqft, target_lat, target_lon, geo_filtered, K)

    # Last resort: global median
    return global_median, 0


def _load_rentals(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        """SELECT zpid, zipcode, price, sqft, bedrooms, bathrooms,
                  latitude, longitude, property_type
           FROM properties
           WHERE listing_type = 'rent'
             AND price > 0 AND sqft > 0
             AND bedrooms IS NOT NULL""",
        conn,
    )
    df = df[df["property_type"].isin(TRAIN_TYPES)].copy()
    df = df[(df["price"] >= 500) & (df["price"] <= 10_000)]
    return df


def _load_sale_listings(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT zpid, zipcode, sqft, bedrooms, latitude, longitude, rent_zestimate
           FROM properties
           WHERE listing_type = 'sale'
             AND sqft > 0 AND bedrooms IS NOT NULL""",
        conn,
    )


def run(conn: sqlite3.Connection) -> dict:
    """Estimate rent for all sale listings using KNN comps.

    Returns a summary dict with model stats.
    """
    from analysis.db import init_analysis_tables, upsert_rent_estimate

    init_analysis_tables(conn)

    rentals = _load_rentals(conn)
    if len(rentals) < 20:
        raise ValueError(f"Not enough rental training data ({len(rentals)} rows).")

    print(f"[*] KNN: {len(rentals)} rental comps across "
          f"{rentals['zipcode'].nunique()} zip codes.")

    global_median = float(rentals["price"].median())

    # --- In-sample evaluation: LOO-style on rentals with coords ---
    eval_rentals = rentals[
        rentals["latitude"].notna() & rentals["longitude"].notna()
    ].copy()

    actuals, predictions = [], []
    for i, (_, row) in enumerate(eval_rentals.iterrows()):
        # Exclude the rental itself from its own comps
        others = rentals.drop(index=row.name) if row.name in rentals.index else rentals
        est, _ = _estimate_for_sale(row, others, global_median)
        actuals.append(row["price"])
        predictions.append(est)

    mae = mean_absolute_error(actuals, predictions)
    r2 = r2_score(actuals, predictions)
    print(f"[*] Leave-one-out MAE: ${mae:,.0f}/mo  |  R²: {r2:.3f}  "
          f"(on {len(actuals)} rentals with coordinates)")

    # --- Predict for all sale listings ---
    sales = _load_sale_listings(conn)
    if sales.empty:
        print("[WARN] No qualifying sale listings found.")
        return {"trained_on": len(rentals), "mae": mae, "r2": r2, "predicted": 0}

    saved = 0
    fallback_count = 0
    for _, row in sales.iterrows():
        est, n_comps = _estimate_for_sale(row, rentals, global_median)
        if n_comps == 0:
            fallback_count += 1
        # Sanity bounds
        est = max(global_median * 0.3, min(est, global_median * 4))
        upsert_rent_estimate(conn, row["zpid"], MODEL_NAME, round(est, 2), n_comps)
        saved += 1

    conn.commit()
    print(f"[*] Saved {saved} KNN estimates under model '{MODEL_NAME}' "
          f"({fallback_count} used global median fallback).")

    return {"trained_on": len(rentals), "mae": mae, "r2": r2, "predicted": saved}
