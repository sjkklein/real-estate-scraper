"""Analysis tables: rent_estimates and investment_analysis."""

from __future__ import annotations

import sqlite3
from typing import Optional


def init_analysis_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rent_estimates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zpid        TEXT NOT NULL REFERENCES properties(zpid),
            model       TEXT NOT NULL,
            estimated_rent REAL,
            n_comps     INTEGER,
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(zpid, model)
        );

        CREATE INDEX IF NOT EXISTS idx_rent_estimates_zpid
            ON rent_estimates(zpid);

        CREATE TABLE IF NOT EXISTS investment_analysis (
            zpid                    TEXT PRIMARY KEY REFERENCES properties(zpid),
            estimated_rent          REAL,
            zillow_rent_zestimate   REAL,
            purchase_price          REAL,
            down_payment_pct        REAL,
            interest_rate           REAL,
            loan_term_years         INTEGER,
            monthly_mortgage        REAL,
            annual_tax              REAL,
            hoa_monthly             REAL,
            insurance_monthly       REAL,
            vacancy_rate_pct        REAL,
            maintenance_pct         REAL,
            monthly_cashflow        REAL,
            gross_yield             REAL,
            cash_on_cash            REAL,
            created_at              TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


def upsert_rent_estimate(
    conn: sqlite3.Connection,
    zpid: str,
    model: str,
    estimated_rent: Optional[float],
    n_comps: Optional[int],
):
    conn.execute(
        """INSERT INTO rent_estimates (zpid, model, estimated_rent, n_comps)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(zpid, model) DO UPDATE SET
               estimated_rent = excluded.estimated_rent,
               n_comps        = excluded.n_comps,
               created_at     = datetime('now')""",
        (zpid, model, estimated_rent, n_comps),
    )
