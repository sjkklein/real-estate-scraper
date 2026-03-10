"""CDPHE Colorado EnviroScreen 2.0 integration.

Downloads census-tract-level environmental-justice data, geocodes properties
to their census tract via the Census Bureau API, and exposes queries for
disproportionately / cumulatively impacted communities.

Tables
------
enviroscreen_tracts   – one row per Colorado census tract (loaded from CSV)
property_enviroscreen – maps properties.rowid → census tract GEOID
"""

from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ENVIROSCREEN_CSV = Path(__file__).parent.parent / "data" / "enviroscreen_tract.csv"

# ---------------------------------------------------------------------------
# Thresholds (Colorado HB 21-1266 / HB 22-1012)
# ---------------------------------------------------------------------------

DI_PERCENTILE_THRESHOLD = 80.0  # >= 80th percentile on EnviroScreen overall
CI_PERCENTILE_THRESHOLD = 75.0  # cumulative impact — no statutory cutoff;
# 75th is a common working threshold

# ---------------------------------------------------------------------------
# Census Bureau Geocoder
# ---------------------------------------------------------------------------

_CENSUS_GEO_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"


def geocode_to_tract(lat: float, lon: float, *, timeout: float = 15.0) -> Optional[str]:
    """Return the 11-digit census tract GEOID for a lat/lon, or None on failure."""
    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    try:
        resp = httpx.get(_CENSUS_GEO_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        tracts = data["result"]["geographies"].get("Census Tracts", [])
        if tracts:
            return tracts[0]["GEOID"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


def init_enviroscreen_tables(conn: sqlite3.Connection):
    """Create the enviroscreen tables if they don't already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS enviroscreen_tracts (
            census_tract_geoid              TEXT PRIMARY KEY,
            county                          TEXT,

            /* top-level scores (percentiles) */
            enviroscreen_percentile         REAL,
            pollution_climate_percentile    REAL,
            health_social_percentile        REAL,

            /* sub-category percentiles */
            env_exposures_percentile        REAL,
            env_effects_percentile          REAL,
            climate_vuln_percentile         REAL,
            sensitive_pop_percentile        REAL,
            demographics_percentile         REAL,

            /* key exposure indicators (percentiles) */
            air_toxics_pctl                 REAL,
            diesel_pm_pctl                  REAL,
            lead_exposure_pctl              REAL,
            ozone_pctl                      REAL,
            fine_particle_pctl              REAL,
            traffic_pctl                    REAL,
            noise_pctl                      REAL,
            drinking_water_pctl             REAL,

            /* environmental effects (percentiles) */
            haz_waste_pctl                  REAL,
            mining_pctl                     REAL,
            superfund_pctl                  REAL,
            oil_gas_pctl                    REAL,
            rmp_sites_pctl                  REAL,
            impaired_water_pctl             REAL,
            wastewater_pctl                 REAL,

            /* climate (percentiles) */
            drought_pctl                    REAL,
            floodplain_pctl                 REAL,
            extreme_heat_pctl               REAL,
            wildfire_pctl                   REAL,

            /* health (percentiles) */
            asthma_pctl                     REAL,
            cancer_pctl                     REAL,
            diabetes_pctl                   REAL,
            heart_disease_pctl              REAL,
            life_expectancy_pctl            REAL,
            low_birth_weight_pctl           REAL,
            mental_health_pctl              REAL,

            /* demographics (raw values — decimal fractions) */
            housing_cost_burdened            REAL,
            disability                       REAL,
            less_than_hs_education           REAL,
            linguistic_isolation             REAL,
            low_income                       REAL,
            people_of_color                  REAL,

            /* population */
            total_population                 INTEGER,

            /* life expectancy in years (useful for display) */
            life_expectancy_years            REAL,

            /* derived flags */
            di_community                     INTEGER DEFAULT 0,
            ci_community                     INTEGER DEFAULT 0,

            loaded_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_es_tracts_di
            ON enviroscreen_tracts(di_community);
        CREATE INDEX IF NOT EXISTS idx_es_tracts_ci
            ON enviroscreen_tracts(ci_community);
        CREATE INDEX IF NOT EXISTS idx_es_tracts_county
            ON enviroscreen_tracts(county);

        /* Maps a property (by rowid) to its census tract. */
        CREATE TABLE IF NOT EXISTS property_enviroscreen (
            property_rowid          INTEGER PRIMARY KEY,
            census_tract_geoid      TEXT REFERENCES enviroscreen_tracts(census_tract_geoid),
            matched_at              TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_pe_tract
            ON property_enviroscreen(census_tract_geoid);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        s = str(val).strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    f = _safe_float(val)
    return int(f) if f is not None else None


def load_enviroscreen_csv(
    conn: sqlite3.Connection, csv_path: Optional[Path] = None
) -> int:
    """Load the CDPHE EnviroScreen 2.0 census-tract CSV into the DB.

    Returns the number of tracts loaded.
    """
    csv_path = csv_path or ENVIROSCREEN_CSV
    if not csv_path.exists():
        raise FileNotFoundError(f"EnviroScreen CSV not found: {csv_path}")

    init_enviroscreen_tables(conn)

    rows_loaded = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Strip whitespace/newlines from header keys (the CDPHE export has
        # a trailing \n on the last column header).
        if reader.fieldnames:
            reader.fieldnames = [k.strip() for k in reader.fieldnames]
        for row in reader:
            geoid = row.get("Census Tract GEOID", "").strip()
            if not geoid:
                continue

            es_pctl = _safe_float(row.get("EnviroScreen Percentile Score", ""))
            low_inc = _safe_float(row.get("Low Income (decimal percent)", ""))
            poc = _safe_float(row.get("People of Color (decimal percent)", ""))

            # DI: >= 80th percentile OR low-income > 40% OR POC > 40%
            di = 0
            if es_pctl is not None and es_pctl >= DI_PERCENTILE_THRESHOLD:
                di = 1
            elif low_inc is not None and low_inc > 0.40:
                di = 1
            elif poc is not None and poc > 0.40:
                di = 1

            # CI: >= 75th percentile (working threshold)
            ci = (
                1 if (es_pctl is not None and es_pctl >= CI_PERCENTILE_THRESHOLD) else 0
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO enviroscreen_tracts (
                    census_tract_geoid, county,
                    enviroscreen_percentile, pollution_climate_percentile,
                    health_social_percentile,
                    env_exposures_percentile, env_effects_percentile,
                    climate_vuln_percentile, sensitive_pop_percentile,
                    demographics_percentile,
                    air_toxics_pctl, diesel_pm_pctl, lead_exposure_pctl,
                    ozone_pctl, fine_particle_pctl, traffic_pctl,
                    noise_pctl, drinking_water_pctl,
                    haz_waste_pctl, mining_pctl, superfund_pctl,
                    oil_gas_pctl, rmp_sites_pctl, impaired_water_pctl,
                    wastewater_pctl,
                    drought_pctl, floodplain_pctl, extreme_heat_pctl,
                    wildfire_pctl,
                    asthma_pctl, cancer_pctl, diabetes_pctl,
                    heart_disease_pctl, life_expectancy_pctl,
                    low_birth_weight_pctl, mental_health_pctl,
                    housing_cost_burdened, disability,
                    less_than_hs_education, linguistic_isolation,
                    low_income, people_of_color,
                    total_population, life_expectancy_years,
                    di_community, ci_community
                ) VALUES (
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?
                )
            """,
                (
                    geoid,
                    row.get("County", "").strip(),
                    es_pctl,
                    _safe_float(
                        row.get("Pollution and Climate Burden Percentile Score")
                    ),
                    _safe_float(row.get("Health and Social Factors Percentile Score")),
                    _safe_float(row.get("Environmental Exposures Percentile Score")),
                    _safe_float(row.get("Environmental Effects Percentile Score")),
                    _safe_float(row.get("Climate Vulnerability Percentile Score")),
                    _safe_float(row.get("Sensitive Populations Percentile Score")),
                    _safe_float(row.get("Demographics Percentile Score")),
                    # exposures
                    _safe_float(row.get("Air Toxics Emissions (percentile)")),
                    _safe_float(row.get("Diesel Particulate Matter (percentile)")),
                    _safe_float(row.get("Lead Exposure Risk (percentile)")),
                    _safe_float(row.get("Ozone (percentile)")),
                    _safe_float(row.get("Fine Particle Pollution (percentile)")),
                    _safe_float(row.get("Traffic Proximity and Volume (percentile)")),
                    _safe_float(row.get("Noise (percentile)")),
                    _safe_float(row.get("Drinking Water Regulations (percentile)")),
                    # effects
                    _safe_float(
                        row.get("Proximity to Hazardous Waste Facilities (percentile)")
                    ),
                    _safe_float(row.get("Proximity to Mining Locations (percentile)")),
                    _safe_float(
                        row.get(
                            "Proximity to National Priorities List Sites (percentile)"
                        )
                    ),
                    _safe_float(row.get("Proximity to Oil and Gas (percentile)")),
                    _safe_float(
                        row.get("Proximity to Risk Management Plan Sites (percentile)")
                    ),
                    _safe_float(row.get("Impaired Streams and Rivers (percentile)")),
                    _safe_float(row.get("Wastewater Discharge Indicator (percentile)")),
                    # climate
                    _safe_float(row.get("Drought (percentile)")),
                    _safe_float(row.get("Floodplains (percentile)")),
                    _safe_float(row.get("Extreme Heat Days (percentile)")),
                    _safe_float(row.get("Wildfire Risk (percentile)")),
                    # health
                    _safe_float(row.get("Asthma Hospitalization Rate (percentile)")),
                    _safe_float(row.get("Cancer index (percentile)")),
                    _safe_float(row.get("Diabetes index (percentile)")),
                    _safe_float(row.get("Heart Disease in Adults (percentile)")),
                    _safe_float(row.get("Life Expectancy (percentile)")),
                    _safe_float(row.get("Low Birth Weight (percentile)")),
                    _safe_float(row.get("Mental Health Indicator (percentile)")),
                    # demographics (raw decimal values)
                    _safe_float(row.get("Housing Cost Burdened (decimal percent)")),
                    _safe_float(row.get("Disability (decimal percent)")),
                    _safe_float(
                        row.get("Less Than High School Education (decimal percent)")
                    ),
                    _safe_float(row.get("Linguistic Isolation (decimal percent)")),
                    low_inc,
                    poc,
                    # population & life expectancy
                    _safe_int(row.get("Total Population Estimate (ACS 2018-2022)")),
                    _safe_float(row.get("Life Expectancy (years)")),
                    # flags
                    di,
                    ci,
                ),
            )
            rows_loaded += 1

    conn.commit()
    return rows_loaded


# ---------------------------------------------------------------------------
# Geocode & match properties to tracts
# ---------------------------------------------------------------------------


def match_properties_to_tracts(
    conn: sqlite3.Connection,
    *,
    skip_matched: bool = True,
    delay: float = 0.25,
) -> dict:
    """Geocode every property with lat/lon and store the census tract mapping.

    Uses the Census Bureau Geocoder API (free, no key required).
    Rate-limits to ~4 req/s to be polite.

    Returns a summary dict: {matched, skipped, failed, total}.
    """
    init_enviroscreen_tables(conn)

    # Get properties that need matching
    if skip_matched:
        rows = conn.execute("""
            SELECT p.rowid, p.latitude, p.longitude, p.address
            FROM properties p
            WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
              AND p.rowid NOT IN (SELECT property_rowid FROM property_enviroscreen)
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT p.rowid, p.latitude, p.longitude, p.address
            FROM properties p
            WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
        """).fetchall()

    total = len(rows)
    matched = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(rows):
        rowid = row["rowid"]
        lat = row["latitude"]
        lon = row["longitude"]
        addr = row["address"] or f"rowid={rowid}"

        geoid = geocode_to_tract(lat, lon)

        if geoid is None:
            failed += 1
            print(f"  [{i + 1}/{total}] FAIL  {addr}")
        else:
            # Only insert if the tract exists in our enviroscreen data
            exists = conn.execute(
                "SELECT 1 FROM enviroscreen_tracts WHERE census_tract_geoid = ?",
                (geoid,),
            ).fetchone()

            if exists:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO property_enviroscreen
                        (property_rowid, census_tract_geoid)
                    VALUES (?, ?)
                """,
                    (rowid, geoid),
                )
                matched += 1
                print(f"  [{i + 1}/{total}] OK    {addr} -> tract {geoid}")
            else:
                # Property is outside Colorado (no enviroscreen data)
                skipped += 1
                print(
                    f"  [{i + 1}/{total}] SKIP  {addr} (tract {geoid} not in EnviroScreen)"
                )

        # Commit periodically and rate-limit
        if (i + 1) % 25 == 0:
            conn.commit()
        if delay > 0 and i < total - 1:
            time.sleep(delay)

    conn.commit()
    return {"matched": matched, "skipped": skipped, "failed": failed, "total": total}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def query_di_ci_properties(
    conn: sqlite3.Connection,
    *,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    listing_type: str = "sale",
    di_only: bool = False,
    ci_only: bool = False,
    county: Optional[str] = None,
    zipcode: Optional[str] = None,
) -> list[dict]:
    """Return properties in DI/CI communities, optionally filtered by budget.

    Each returned dict includes both property fields and enviroscreen scores.
    """
    sql = """
        SELECT
            p.rowid         AS property_rowid,
            p.address,
            p.city,
            p.state,
            p.zipcode,
            p.price,
            p.bedrooms,
            p.bathrooms,
            p.sqft,
            p.year_built,
            p.property_type,
            p.listing_type,
            p.detail_url,
            p.rent_zestimate,
            p.hoa_fee,
            p.annual_tax,
            pe.census_tract_geoid,
            e.county                        AS tract_county,
            e.enviroscreen_percentile,
            e.pollution_climate_percentile,
            e.health_social_percentile,
            e.di_community,
            e.ci_community,
            e.lead_exposure_pctl,
            e.air_toxics_pctl,
            e.ozone_pctl,
            e.fine_particle_pctl,
            e.noise_pctl,
            e.life_expectancy_years,
            e.low_income,
            e.people_of_color,
            e.housing_cost_burdened,
            e.total_population
        FROM properties p
        JOIN property_enviroscreen pe ON pe.property_rowid = p.rowid
        JOIN enviroscreen_tracts e    ON e.census_tract_geoid = pe.census_tract_geoid
    """

    where = []
    params: list = []

    if listing_type:
        where.append("p.listing_type = ?")
        params.append(listing_type)
    if max_price is not None:
        where.append("p.price <= ?")
        params.append(max_price)
    if min_price is not None:
        where.append("p.price >= ?")
        params.append(min_price)
    if di_only:
        where.append("e.di_community = 1")
    if ci_only and not di_only:
        where.append("e.ci_community = 1")
    if county:
        where.append("LOWER(e.county) LIKE LOWER(?)")
        params.append(f"%{county}%")
    if zipcode:
        where.append("p.zipcode = ?")
        params.append(zipcode)

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY e.enviroscreen_percentile DESC, p.price ASC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_enviroscreen_stats(conn: sqlite3.Connection) -> dict:
    """Summary stats for loaded enviroscreen data."""
    row = conn.execute("""
        SELECT
            COUNT(*)                                        AS total_tracts,
            SUM(di_community)                               AS di_tracts,
            SUM(ci_community)                               AS ci_tracts,
            ROUND(AVG(enviroscreen_percentile), 1)          AS avg_percentile,
            (SELECT COUNT(*) FROM property_enviroscreen)    AS matched_properties
        FROM enviroscreen_tracts
    """).fetchone()
    return dict(row) if row else {}
