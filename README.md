# real-estate-scraper

Zillow scraper for rental investment analysis in Colorado, with CDPHE EnviroScreen integration to identify properties in disproportionately and cumulatively impacted communities.

## Requirements

- Python 3.10+
- Dependencies: `httpx`, `beautifulsoup4`, `lxml`, `pyyaml`

```bash
pip install -r requirements.txt
```

## Quick Start

```bash
# 1. Configure your searches in config.yaml (see examples in the file)

# 2. Scrape Zillow listings
python main.py scrape <search-name>       # run one saved search
python main.py scrape-all                  # run all saved searches

# 3. Estimate rents for sale listings
python main.py analyze-rents --algo ols    # two-stage OLS model
python main.py analyze-rents --algo knn    # weighted KNN comps model

# 4. Generate charts
python main.py chart --zip 80903 80904
python main.py margin-chart --zip 80903 80904
```

## Commands

### Scraping

```bash
# Scrape a single saved search
python main.py scrape <name> [-t sale|rent|both] [-p PAGES] [-e] [--skip-recent DAYS]

# Run all saved searches from config.yaml
python main.py scrape-all [-t sale|rent|both] [-p PAGES] [-e] [--skip-recent DAYS]
```

Options:
- `-t, --type` — Listing type: `sale`, `rent`, or `both` (overrides config)
- `-p, --pages` — Max pages to scrape per type (default: 5, Zillow caps at 20)
- `-e, --enrich` — Fetch detail pages for rent Zestimates and cost data (slower)
- `--skip-recent DAYS` — Skip re-enriching properties updated within N days
- `-f, --filter-rentals` — Keep only SFH/duplexes from rental results

### Rent Estimation

```bash
python main.py analyze-rents [--algo ols|knn] [--model MODEL_TAG]
```

Two estimation algorithms:
- **`ols`** (default) — Two-stage OLS: zip-level median rent + OLS on log(rent/median) ~ log(sqft) + bedrooms
- **`knn`** — Weighted KNN comps: finds K=10 most similar rentals by sqft + geographic distance

### Charts

```bash
# Price vs. estimated rent scatter
python main.py chart --zip 80903 80904 [--model ols_v1] [-o FILE] [--no-browser]

# Cash-flow margin: rent minus mortgage + HOA + tax + insurance
python main.py margin-chart --zip 80903 80904 [--down PCT] [--rate PCT] [--years N]
```

Mortgage defaults are set in `config.yaml` under `chart:` and can be overridden with CLI flags.

### Blacklist

```bash
python main.py blacklist add <row-id>          # exclude a property from charts
python main.py blacklist remove <address>       # remove by address fragment
python main.py blacklist list                   # show all blacklisted
```

### Stats & Export

```bash
python main.py stats [--city CITY]
python main.py export [-o FILE] [-t sale|rent] [--city CITY] [--zipcode ZIP]
```

---

## EnviroScreen Integration

Cross-reference scraped properties with [CDPHE Colorado EnviroScreen 2.0](https://cdphe.colorado.gov/enviroscreen) data to identify properties in disproportionately impacted (DI) and cumulatively impacted (CI) communities.

EnviroScreen scores 1,443 Colorado census tracts across 35 environmental and health indicators including air quality, lead exposure, proximity to hazardous sites, climate vulnerability, and socioeconomic factors.

### Definitions

| Term | Criteria | Source |
|------|----------|--------|
| **DI Community** | EnviroScreen >= 80th percentile, OR low-income > 40%, OR people of color > 40% | Colorado HB 21-1266 |
| **CI Community** | EnviroScreen >= 75th percentile | Working threshold for cumulative impact |

### Setup

```bash
# Step 1: Load the EnviroScreen census tract data (ships with the repo)
python main.py enviroscreen load

# Step 2: Scrape properties (if not already done)
python main.py scrape-all

# Step 3: Geocode properties to census tracts via Census Bureau API
# (~0.25s per property to respect rate limits)
python main.py enviroscreen match
```

### Querying

```bash
# Find sale properties under $450k in DI communities
python main.py enviroscreen query --max-price 450000 --di

# Find properties $200k-$350k in CI communities in El Paso County
python main.py enviroscreen query --min-price 200000 --max-price 350000 --ci --county "El Paso"

# Filter by zip code
python main.py enviroscreen query --max-price 400000 --di --zipcode 80903

# Query rental listings instead of sales
python main.py enviroscreen query --max-price 2000 -t rent --di

# Export results to CSV
python main.py enviroscreen export --max-price 450000 --di -o di_properties.csv

# View summary statistics
python main.py enviroscreen stats
```

### Query Output

Each result includes:

- Property details (address, price, beds/baths, sqft)
- Census tract GEOID and county
- EnviroScreen percentile score
- DI/CI community flags
- Environmental highlights (lead exposure, air toxics, ozone, PM2.5, noise — shown when >= 75th percentile)
- Life expectancy, low-income percentage
- Rent Zestimate (if available from enrichment)

### Database Schema

The EnviroScreen integration adds two tables to the existing `data/properties.db`:

**`enviroscreen_tracts`** — One row per Colorado census tract (1,443 total). Key columns:

| Column | Description |
|--------|-------------|
| `census_tract_geoid` | 11-digit FIPS code (primary key) |
| `enviroscreen_percentile` | Overall EnviroScreen score (0-100) |
| `pollution_climate_percentile` | Pollution & climate burden score |
| `health_social_percentile` | Health & social factors score |
| `di_community` | 1 if disproportionately impacted, 0 otherwise |
| `ci_community` | 1 if cumulatively impacted, 0 otherwise |
| `lead_exposure_pctl` | Lead exposure risk percentile |
| `low_income` | Fraction of low-income population |
| `people_of_color` | Fraction of people of color |
| `life_expectancy_years` | Life expectancy in years |
| `total_population` | Census tract population |

Plus ~30 additional indicator percentiles (air toxics, diesel PM, ozone, wildfire risk, etc.).

**`property_enviroscreen`** — Maps each property to its census tract:

| Column | Description |
|--------|-------------|
| `property_rowid` | References `properties(rowid)` (primary key) |
| `census_tract_geoid` | References `enviroscreen_tracts(census_tract_geoid)` |
| `matched_at` | Timestamp of geocoding |

### How Matching Works

1. Each property's latitude/longitude is sent to the [Census Bureau Geocoder API](https://geocoding.geo.census.gov/geocoder/)
2. The API returns the 11-digit census tract GEOID for that coordinate
3. The GEOID is looked up in the `enviroscreen_tracts` table
4. If found (property is in Colorado), the mapping is stored in `property_enviroscreen`
5. Queries join all three tables: `properties` -> `property_enviroscreen` -> `enviroscreen_tracts`

### Data Source

The EnviroScreen CSV (`data/enviroscreen_tract.csv`) is downloaded from [CDPHE's Colorado EnviroScreen 2.0](https://cdphe.colorado.gov/enviroscreen) census tract level dataset. To refresh the data:

```bash
# Re-download from CDPHE
curl -L "https://docs.google.com/spreadsheets/d/1QMDB7temxjPuVww99ECqr6Pga0xxvI7L/export?format=csv&gid=1057987019" \
  -o data/enviroscreen_tract.csv

# Reload into the database
python main.py enviroscreen load
```

---

## Configuration

All settings live in `config.yaml`. See the comments in that file for full documentation.

### Saved Searches

```yaml
searches:
  my-search:
    url: "https://www.zillow.com/..."   # full Zillow search URL
    type: sale                           # sale, rent, or both
    pages: 20                            # max pages (Zillow caps at 20)
    enrich: true                         # fetch detail pages
    skip_recent: 7                       # skip re-enriching within N days
```

### Chart / Mortgage Defaults

```yaml
chart:
  down: 20            # down payment %
  rate: 6.5           # annual mortgage interest rate %
  years: 30           # loan term
  tax_rate: 0.4       # fallback property tax as % of price
  insurance_rate: 0.5  # fallback insurance as % of price
```

## Project Structure

```
real-estate-scraper/
  main.py                  # CLI entry point
  config.yaml              # saved searches, chart defaults, blacklist
  requirements.txt         # Python dependencies
  ANALYSIS.md              # rent model planning & status
  scraper/
    zillow.py              # Zillow scraper (search + detail enrichment)
    db.py                  # properties table, queries, upserts
    config.py              # config.yaml loader
    enviroscreen.py        # EnviroScreen tables, CSV loader, geocoder, queries
    zip_to_district.csv    # zip-to-school-district mapping
  analysis/
    db.py                  # rent_estimates & investment_analysis tables
    rent_model.py          # two-stage OLS rent estimation
    knn_model.py           # weighted KNN comps rent estimation
    chart.py               # Plotly chart generation
  data/
    properties.db          # SQLite database (gitignored)
    enviroscreen_tract.csv # CDPHE EnviroScreen 2.0 census tract data
```

## License

See [LICENSE](LICENSE).
