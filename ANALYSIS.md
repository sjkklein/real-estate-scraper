# Rental Investment Analysis — Planning & Status

## Goal
Given the scraped property database, estimate rental value for any listing
and rank sale listings by investment margin (estimated rent minus carrying costs).

---

## Data Overview (as of first analysis session)

| Field        | Coverage (rentals) | Notes |
|---|---|---|
| price        | 849 / 851          | Rent price |
| sqft         | 802 / 851          | Missing ~6% |
| bedrooms     | 842 / 851          | |
| bathrooms    | 815 / 851          | |
| lat/lon      | 812 / 851          | Used for geographic proximity |
| year_built   | 0 / 851            | Not populated yet; skipped for now |
| zipcode      | 851 / 851          | 34 distinct zips |
| rent_zestimate | 901 total        | Includes many sale listings (from enrichment) |

Sale listings: 707 total. Most have sqft, beds, baths populated via enrichment.

---

## Architecture Decisions

### Separation of concerns
- `properties` table: raw scraped data, never written by analysis code
- `rent_estimates` table: model outputs, linked to properties by zpid
- `investment_analysis` table: per-property investment metrics, linked by zpid
- Analysis code lives in `analysis/` directory

### Rent estimation strategy
- **Phase 1 model:** OLS linear regression (scikit-learn)
  - Features: sqft, bedrooms, bathrooms, zip-code (target-encoded by median rent),
    plus lat/lon-weighted comps for sparse zips
  - Trained on SFH + duplex rental listings only
  - Store: estimated rent, n_comps (sample size used for encoding), model version
- **Zillow benchmark:** `rent_zestimate` from properties table stored side-by-side
  for comparison — not used in our model, used in final aggregated output
- Multiple models can coexist in `rent_estimates` (identified by `model` column)

### Geographic proximity for sparse zips
- When a zip has fewer than N rentals (threshold TBD, ~10), expand the training
  window using nearby rentals weighted by lat/lon distance
- Uses coordinates already in DB; no external geo data needed

### Property cost data (for investment analysis)
Fields to fetch/store per sale listing:
- `tax_assessed_value`: already present in Zillow search results (`homeInfo.taxAssessedValue`) — **not yet stored, needs DB column + scraper change**
- `annual_tax`: available on detail pages (`taxAnnualAmount` / `resoFacts.taxAnnualAmount`) — **not yet stored**
- `hoa_fee`: available on detail pages (`monthlyHoaFee` / `resoFacts.hoaFee`) — **not yet stored**
- `annual_homeowners_insurance`: available on detail pages — **not yet stored**
- These will be added to `properties` table via migration + enrichment update

### Mortgage inputs
User-supplied at analysis time (not per-property). Will be read from `config.yaml`
under a new `mortgage:` section:
```yaml
mortgage:
  down_payment_pct: 20
  interest_rate: 7.0      # annual %
  loan_term_years: 30
  vacancy_rate_pct: 5     # % of rent assumed vacant
  maintenance_pct: 1      # % of purchase price annually
```
Overridable via CLI flags.

### Investment scoring
Metrics computed per sale listing:
- `gross_yield`: (estimated_rent * 12) / purchase_price
- `monthly_mortgage`: standard amortization formula on (price * (1 - down_pct))
- `estimated_monthly_expenses`: mortgage + tax/12 + hoa + insurance + vacancy + maintenance
- `monthly_cashflow`: estimated_rent - estimated_monthly_expenses
- `cash_on_cash`: (monthly_cashflow * 12) / (price * down_pct)
- Final ranked output sorts by `cash_on_cash` descending (or user-chosen metric)

---

## DB Schema (planned)

### `rent_estimates`
```sql
CREATE TABLE rent_estimates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    zpid        TEXT NOT NULL REFERENCES properties(zpid),
    model       TEXT NOT NULL,   -- e.g. 'ols_v1', 'zillow_zestimate'
    estimated_rent REAL,
    n_comps     INTEGER,         -- training samples near this property
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(zpid, model)
);
```

### `investment_analysis`
```sql
CREATE TABLE investment_analysis (
    zpid                    TEXT PRIMARY KEY REFERENCES properties(zpid),
    estimated_rent          REAL,   -- from our best model
    zillow_rent_zestimate   REAL,   -- from properties.rent_zestimate
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
```

### New columns on `properties` (migration)
- `tax_assessed_value REAL`
- `annual_tax REAL`
- `hoa_fee REAL`
- `annual_homeowners_insurance REAL`

---

## Implementation Phases

### Phase 1 — Rent estimation model *(in progress)*
- [x] Add `tax_assessed_value`, `annual_tax`, `hoa_fee`, `annual_homeowners_insurance`
      columns to `properties` + update scraper to populate them
      — `tax_assessed_value` comes from search results (`homeInfo.taxAssessedValue`)
      — `annual_tax`, `hoa_fee`, `annual_homeowners_insurance` come from detail page
         (`taxAnnualAmount`, `hoaFee`, `annualHomeownersInsurance` / `resoFacts.*`)
- [x] Create `analysis/` package with `rent_model.py` (OLS) and `db.py` (analysis tables)
- [x] Train OLS model on rental listings; store results in `rent_estimates`
      — Two-stage ratio model: zip median (location) × exp(OLS on log-ratio)
      — Features: log(sqft), bedrooms. Training set: SFH/duplex rentals $500–$10k/mo.
      — In-sample MAE ~$218/mo, R² ~0.49 on 689 rentals across 34 zips.
      — Bug found & fixed: builder/new-construction listings were misclassified as rentals
        (91 records with prices $300k–$450k in listing_type='rent'). Fixed in scraper
        by checking homeStatus before falling back to URL-based detection.
- [x] CLI command: `python main.py analyze-rents`
- [ ] Evaluate: compare OLS estimates vs Zillow zestimate on held-out rentals

### Phase 2 — Investment analysis *(planned)*
- [ ] Add `mortgage:` section to `config.yaml`
- [ ] Create `analysis/investment.py` — mortgage math + metric computation
- [ ] Populate `investment_analysis` table for all sale listings
- [ ] CLI command: `python main.py analyze investments [--down 20] [--rate 7.0]`
- [ ] Output: ranked CSV/table of listings by cash-on-cash return

### Phase 3 — Multi-model evaluation *(future)*
- [ ] Try additional models (e.g. gradient boosting) stored under different `model` names
- [ ] Add comparison report showing model accuracy vs Zillow zestimate

---

## Open Questions
- Sparse zip threshold: how many rentals minimum before we fall back to geo-weighted?
- year_built: run enrichment pass to populate before Phase 2?
- Insurance estimate: use Zillow's figure or a fixed % of price as fallback?
