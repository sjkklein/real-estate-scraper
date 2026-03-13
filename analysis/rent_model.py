"""OLS rent estimation model.

Two-stage approach to avoid multicollinearity between location and property features:

  Stage 1 — Location baseline:
    Compute a zip-level median rent (geo-weighted for sparse zips).
    This captures "how expensive is this neighbourhood."

  Stage 2 — Property ratio:
    For each rental: ratio = price / zip_median.
    Fit OLS on log(ratio) ~ log(sqft) + bedrooms.
    log-transform stabilises variance and captures the multiplicative nature of rent.

  Prediction:
    estimated_rent = zip_median × exp(predicted_log_ratio)

This decouples location from property characteristics, keeping each stage
well-conditioned and interpretable.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

MODEL_NAME = "ols_v1"
MIN_ZIP_SAMPLES = 5      # rentals needed to use a zip's own median
GEO_RADIUS_MILES = 15    # fallback radius for sparse zips
TRAIN_TYPES = {"SINGLE_FAMILY", "TOWNHOUSE", "DUPLEX"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_miles(lat1: float, lon1: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Vectorized Haversine distance from one point to an array of points."""
    R = 3958.8
    dlat = np.radians(lats - lat1)
    dlon = np.radians(lons - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lats)) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _geo_weighted_median(
    target_lat: float,
    target_lon: float,
    rentals: pd.DataFrame,
    radius_miles: float,
) -> tuple[Optional[float], int]:
    """Return distance-weighted mean rent and count from rentals within radius."""
    has_coords = rentals["latitude"].notna() & rentals["longitude"].notna()
    pool = rentals[has_coords]
    if pool.empty:
        return None, 0

    dists = _haversine_miles(target_lat, target_lon, pool["latitude"].values, pool["longitude"].values)
    mask = dists <= radius_miles
    nearby = pool[mask]

    # If nothing within radius, expand to 2x
    if nearby.empty:
        mask = dists <= radius_miles * 2
        nearby = pool[mask]

    if nearby.empty:
        return None, 0

    weights = 1.0 / (dists[mask] + 0.1)
    estimate = float(np.average(nearby["price"].values, weights=weights))
    return estimate, int(mask.sum())


def compute_zip_medians(rentals: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame with columns [zipcode, zip_median, n_comps, centroid_lat, centroid_lon].

    zip_median is the local market rent level for that zip. n_comps is how many
    rentals were used (lower = less confident).
    """
    records = []
    zip_groups = rentals.groupby("zipcode")

    for zipcode, group in zip_groups:
        n = len(group)
        if n >= MIN_ZIP_SAMPLES:
            median = float(group["price"].median())
            lat = group["latitude"].mean()
            lon = group["longitude"].mean()
            records.append({"zipcode": zipcode, "zip_median": median, "n_comps": n,
                             "centroid_lat": lat, "centroid_lon": lon})
        else:
            # Use geo-weighted fallback centered on this zip's centroid
            lat = group["latitude"].mean()
            lon = group["longitude"].mean()
            if pd.isna(lat):
                median = float(rentals["price"].median())
                n_used = len(rentals)
            else:
                median, n_used = _geo_weighted_median(lat, lon, rentals, GEO_RADIUS_MILES)
                if median is None:
                    median = float(rentals["price"].median())
                    n_used = len(rentals)
            records.append({"zipcode": zipcode, "zip_median": median, "n_comps": n_used,
                             "centroid_lat": lat, "centroid_lon": lon})

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def _load_rentals(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        """SELECT zpid, zipcode, city, price, sqft, bedrooms, bathrooms,
                  latitude, longitude, property_type
           FROM properties
           WHERE listing_type = 'rent'
             AND price > 0
             AND sqft > 0
             AND bedrooms IS NOT NULL
             AND bathrooms IS NOT NULL""",
        conn,
    )
    # SFH-only training set
    df = df[df["property_type"].isin(TRAIN_TYPES)].copy()
    # Drop clearly misclassified listings: new-construction sale prices filed as rent.
    # $10k/mo is a generous upper bound for residential rent in any market we target.
    df = df[(df["price"] >= 500) & (df["price"] <= 10_000)]
    return df


def _load_sale_listings(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT zpid, zipcode, city, sqft, bedrooms, latitude, longitude, rent_zestimate
           FROM properties
           WHERE listing_type = 'sale'
             AND sqft > 0
             AND bedrooms IS NOT NULL""",
        conn,
    )


def _estimate_manual_properties(
    conn: sqlite3.Connection,
    manual_props: list[dict],
    model,
    zip_medians: pd.DataFrame,
    zip_median_map: dict,
    rentals: pd.DataFrame,
    global_median: float,
) -> int:
    """Estimate total rent for manually configured multifamily properties.

    Each entry in manual_props should have:
      label, zipcode, sale_price, units (list of {sqft, bedrooms, bathrooms})
    and optionally: address, city, state, property_type.

    Estimates rent for each unit independently using the trained OLS model,
    sums them, and stores the total in rent_estimates under MODEL_NAME.
    Returns the number of properties saved.
    """
    import hashlib
    from analysis.db import upsert_rent_estimate

    if not manual_props:
        return 0

    saved = 0
    for prop_cfg in manual_props:
        label = str(prop_cfg.get("label") or prop_cfg.get("address") or "unknown")
        units = prop_cfg.get("units") or []
        if not units:
            print(f"[WARN] manual property '{label}' has no units — skipped")
            continue

        zipcode = str(prop_cfg.get("zipcode", "")).strip()

        # Get zip median for this property's location
        if zipcode and zipcode in zip_median_map:
            zm = zip_median_map[zipcode]["zip_median"]
            n_comps = zip_median_map[zipcode]["n_comps"]
        else:
            zm = global_median
            n_comps = len(rentals)
            if zipcode:
                print(f"[WARN] No zip data for '{label}' (zip: {zipcode}) — using global median ${global_median:,.0f}")

        # Estimate rent for each unit and sum
        unit_estimates = []
        for i, unit in enumerate(units):
            sqft = unit.get("sqft", 0)
            bedrooms = unit.get("bedrooms", 0)
            if sqft <= 0:
                print(f"[WARN] Unit {i + 1} of '{label}' missing sqft — skipped")
                continue
            log_sqft = np.log(max(sqft, 1))
            x = np.array([[log_sqft, bedrooms]])
            log_ratio = float(model.predict(x)[0])
            est = zm * np.exp(log_ratio)
            est = max(global_median * 0.3, min(est, global_median * 4))
            unit_estimates.append(est)

        if not unit_estimates:
            continue

        total_rent = sum(unit_estimates)
        unit_str = " + ".join(f"${e:,.0f}" for e in unit_estimates)
        print(f"[*] Manual '{label}': {len(unit_estimates)} units = {unit_str} = ${total_rent:,.0f}/mo total")

        # Stable synthetic zpid based on label
        zpid = "manual_" + hashlib.md5(label.encode()).hexdigest()[:10]

        # Ensure property exists in the properties table so FK constraint is satisfied
        sale_price = prop_cfg.get("sale_price")
        address = prop_cfg.get("address") or label
        city = prop_cfg.get("city", "")
        state = prop_cfg.get("state", "")
        prop_type = prop_cfg.get("property_type", "MULTI_FAMILY")
        total_sqft = sum(u.get("sqft", 0) for u in units)
        total_beds = sum(u.get("bedrooms", 0) for u in units)
        total_baths = sum(float(u.get("bathrooms", 0)) for u in units)

        conn.execute(
            """INSERT INTO properties
                   (zpid, address, city, state, zipcode, price, sqft,
                    bedrooms, bathrooms, property_type, listing_type, search_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sale', 'manual')
               ON CONFLICT(zpid) DO UPDATE SET
                   price        = excluded.price,
                   sqft         = excluded.sqft,
                   bedrooms     = excluded.bedrooms,
                   bathrooms    = excluded.bathrooms,
                   property_type = excluded.property_type,
                   updated_at   = datetime('now')""",
            (zpid, address, city, state, zipcode, sale_price,
             total_sqft, total_beds, total_baths, prop_type),
        )
        upsert_rent_estimate(conn, zpid, MODEL_NAME, round(total_rent, 2), n_comps)
        saved += 1

    conn.commit()
    return saved


def run(conn: sqlite3.Connection, manual_properties: Optional[list] = None) -> dict:
    """Train OLS on rentals, predict for all qualifying sale listings.

    Returns a summary dict with model stats.
    """
    from analysis.db import init_analysis_tables, upsert_rent_estimate

    init_analysis_tables(conn)

    rentals = _load_rentals(conn)
    if len(rentals) < 20:
        raise ValueError(f"Not enough rental training data ({len(rentals)} rows after filtering).")

    print(f"[*] Training on {len(rentals)} SFH/duplex rental listings across "
          f"{rentals['zipcode'].nunique()} zip codes.")

    # --- Location feature: zip median rent ---
    zip_medians = compute_zip_medians(rentals)
    rentals = rentals.merge(zip_medians[["zipcode", "zip_median"]], on="zipcode", how="left")
    # Any rentals with no zip_median (shouldn't happen) fall back to global median
    global_median = rentals["price"].median()
    rentals["zip_median"] = rentals["zip_median"].fillna(global_median)

    # --- Impute missing bathrooms with median for that bedroom count ---
    bath_medians = rentals.groupby("bedrooms")["bathrooms"].median()
    rentals["bathrooms"] = rentals.apply(
        lambda r: bath_medians.get(r["bedrooms"], rentals["bathrooms"].median())
        if pd.isna(r["bathrooms"]) else r["bathrooms"],
        axis=1,
    )

    # --- Stage 2: ratio model on log scale ---
    # ratio = rent / zip_median  (how much above/below the local baseline)
    # log(ratio) ~ b0 + b1*log(sqft) + b2*bedrooms
    rentals["ratio"] = rentals["price"] / rentals["zip_median"]
    rentals["log_ratio"] = np.log(rentals["ratio"].clip(lower=0.1))
    rentals["log_sqft"] = np.log(rentals["sqft"].clip(lower=1))

    features = ["log_sqft", "bedrooms"]
    X_train = rentals[features].values
    y_train = rentals["log_ratio"].values

    model = LinearRegression()
    model.fit(X_train, y_train)

    # --- In-sample evaluation (back-transform to rent $) ---
    log_ratio_pred = model.predict(X_train)
    y_pred_train = rentals["zip_median"].values * np.exp(log_ratio_pred)
    mae = mean_absolute_error(rentals["price"].values, y_pred_train)
    r2 = r2_score(rentals["price"].values, y_pred_train)
    print(f"[*] Training MAE: ${mae:,.0f}/mo  |  R²: {r2:.3f}")
    coefs = dict(zip(features, model.coef_))
    print(f"[*] log-ratio coefficients: {coefs}  |  intercept: {model.intercept_:.3f}")

    # --- Predict for sale listings ---
    sales = _load_sale_listings(conn)
    if sales.empty:
        print("[WARN] No qualifying sale listings found.")
        return {"trained_on": len(rentals), "mae": mae, "r2": r2, "predicted": 0}

    # Map zip medians; for unknown zips use geo-weighted fallback
    zip_median_map = zip_medians.set_index("zipcode")[["zip_median", "n_comps",
                                                         "centroid_lat", "centroid_lon"]].to_dict("index")

    saved = 0
    skipped = 0
    for _, row in sales.iterrows():
        zipcode = row["zipcode"]
        if zipcode in zip_median_map:
            zm = zip_median_map[zipcode]["zip_median"]
            n_comps = zip_median_map[zipcode]["n_comps"]
        else:
            # Unknown zip: geo-weighted from all rentals
            lat, lon = row.get("latitude"), row.get("longitude")
            if pd.notna(lat) and pd.notna(lon):
                zm, n_comps = _geo_weighted_median(lat, lon, rentals, GEO_RADIUS_MILES)
                if zm is None:
                    zm, n_comps = global_median, len(rentals)
            else:
                zm, n_comps = global_median, len(rentals)

        log_sqft = np.log(max(row["sqft"], 1))
        x = np.array([[log_sqft, row["bedrooms"]]])
        log_ratio = float(model.predict(x)[0])
        est = zm * np.exp(log_ratio)

        # Sanity bounds: clamp to [global_median * 0.3, global_median * 4]
        est = max(global_median * 0.3, min(est, global_median * 4))

        upsert_rent_estimate(conn, row["zpid"], MODEL_NAME, round(est, 2), n_comps)
        saved += 1

    conn.commit()
    print(f"[*] Saved {saved} rent estimates ({skipped} skipped) under model '{MODEL_NAME}'.")

    n_manual = _estimate_manual_properties(
        conn, manual_properties or [], model, zip_medians, zip_median_map, rentals, global_median
    )

    return {"trained_on": len(rentals), "mae": mae, "r2": r2, "predicted": saved, "manual": n_manual}
