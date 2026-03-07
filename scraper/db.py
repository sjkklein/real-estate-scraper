"""SQLite storage for scraped property data."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Optional

from .zillow import Property

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "properties.db"
DISTRICT_CSV = Path(__file__).parent / "zip_to_district.csv"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = db_path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS properties (
            zpid TEXT PRIMARY KEY,
            address TEXT,
            city TEXT,
            state TEXT,
            zipcode TEXT,
            price REAL,
            rent_zestimate REAL,
            zestimate REAL,
            bedrooms INTEGER,
            bathrooms REAL,
            sqft INTEGER,
            lot_sqft INTEGER,
            year_built INTEGER,
            property_type TEXT,
            listing_type TEXT,
            latitude REAL,
            longitude REAL,
            detail_url TEXT,
            days_on_zillow INTEGER,
            scraped_at TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_properties_location
            ON properties(city, state, zipcode);
        CREATE INDEX IF NOT EXISTS idx_properties_listing_type
            ON properties(listing_type);
        CREATE INDEX IF NOT EXISTS idx_properties_price
            ON properties(price);

        CREATE TABLE IF NOT EXISTS zip_districts (
            zipcode TEXT PRIMARY KEY,
            school_district TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_zip_districts_district
            ON zip_districts(school_district);
    """)
    conn.commit()


def load_district_data(conn: sqlite3.Connection):
    """Load zip-to-school-district mapping from bundled CSV."""
    # Check if already loaded
    count = conn.execute("SELECT COUNT(*) FROM zip_districts").fetchone()[0]
    if count > 0:
        return

    if not DISTRICT_CSV.exists():
        print("[WARN] zip_to_district.csv not found, skipping district data")
        return

    with open(DISTRICT_CSV, newline="") as f:
        reader = csv.DictReader(f)
        rows = [(r["zipcode"], r["school_district"]) for r in reader]

    conn.executemany(
        "INSERT OR IGNORE INTO zip_districts (zipcode, school_district) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    print(f"[*] Loaded {len(rows)} zip-to-district mappings")


def get_district(conn: sqlite3.Connection, zipcode: str) -> Optional[str]:
    """Look up the school district for a zip code."""
    row = conn.execute(
        "SELECT school_district FROM zip_districts WHERE zipcode = ?", (zipcode,)
    ).fetchone()
    return row["school_district"] if row else None


def upsert_property(conn: sqlite3.Connection, prop: Property):
    d = prop.to_dict()
    columns = list(d.keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    update_str = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "zpid")

    conn.execute(
        f"""INSERT INTO properties ({col_str}) VALUES ({placeholders})
            ON CONFLICT(zpid) DO UPDATE SET {update_str}, updated_at=datetime('now')""",
        list(d.values()),
    )


def upsert_properties(conn: sqlite3.Connection, properties: list[Property]):
    for prop in properties:
        upsert_property(conn, prop)
    conn.commit()


def query_properties(
    conn: sqlite3.Connection,
    listing_type: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    zipcode: Optional[str] = None,
    district: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> list[dict]:
    """Query properties with optional filters."""
    sql = "SELECT p.* FROM properties p"
    joins = []
    where = ["1=1"]
    params = []

    if district:
        joins.append("JOIN zip_districts zd ON p.zipcode = zd.zipcode")
        where.append("LOWER(zd.school_district) LIKE LOWER(?)")
        params.append(f"%{district}%")

    if listing_type:
        where.append("p.listing_type = ?")
        params.append(listing_type)
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

    sql += " " + " ".join(joins) + " WHERE " + " AND ".join(where) + " ORDER BY p.price ASC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_rental_comps(
    conn: sqlite3.Connection,
    zipcode: str,
    bedrooms: Optional[int] = None,
) -> list[dict]:
    """Get rental listings in a zip code, optionally filtered by bedrooms."""
    sql = """
        SELECT * FROM properties
        WHERE listing_type = 'rent' AND zipcode = ? AND price IS NOT NULL AND price > 0
    """
    params = [zipcode]
    if bedrooms is not None:
        sql += " AND bedrooms = ?"
        params.append(bedrooms)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_stats(conn: sqlite3.Connection, city: Optional[str] = None, district: Optional[str] = None) -> dict:
    """Get summary statistics for stored properties."""
    joins = ""
    where_parts = []
    params = []

    if city:
        where_parts.append("LOWER(p.city) = LOWER(?)")
        params.append(city)
    if district:
        joins = "JOIN zip_districts zd ON p.zipcode = zd.zipcode"
        where_parts.append("LOWER(zd.school_district) LIKE LOWER(?)")
        params.append(f"%{district}%")

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    row = conn.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN p.listing_type='sale' THEN 1 ELSE 0 END) as for_sale,
            SUM(CASE WHEN p.listing_type='rent' THEN 1 ELSE 0 END) as for_rent,
            AVG(CASE WHEN p.listing_type='sale' THEN p.price END) as avg_sale_price,
            AVG(CASE WHEN p.listing_type='rent' THEN p.price END) as avg_rent,
            AVG(p.rent_zestimate) as avg_rent_zestimate,
            COUNT(DISTINCT p.city || p.state) as locations
        FROM properties p {joins} {where}
    """, params).fetchone()

    return dict(row)
