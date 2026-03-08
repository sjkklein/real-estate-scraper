"""Zillow scraper for sale and rental listings."""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://www.zillow.com"

# Zillow's internal search API endpoint
SEARCH_API_URL = f"{BASE_URL}/async-create-search-page-state"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# Delay between requests in seconds (min, max)
REQUEST_DELAY = (2, 5)


@dataclass
class Property:
    zpid: str
    address: str
    city: str
    state: str
    zipcode: str
    price: Optional[float] = None
    rent_zestimate: Optional[float] = None
    zestimate: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    sqft: Optional[int] = None
    lot_sqft: Optional[int] = None
    year_built: Optional[int] = None
    property_type: Optional[str] = None
    listing_type: str = ""  # "sale" or "rent"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    detail_url: Optional[str] = None
    days_on_zillow: Optional[int] = None
    scraped_at: str = ""
    search_name: Optional[str] = None
    tax_assessed_value: Optional[float] = None
    annual_tax: Optional[float] = None
    hoa_fee: Optional[float] = None
    annual_homeowners_insurance: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ZillowScraper:
    def __init__(self, delay: tuple[float, float] = REQUEST_DELAY):
        self.delay = delay
        self.client = httpx.Client(
            headers=self._base_headers(),
            follow_redirects=True,
            timeout=30.0,
        )
        self._initialized = False

    def _ensure_session(self):
        """Visit Zillow homepage first to establish cookies/session."""
        if self._initialized:
            return
        try:
            self.client.get(BASE_URL + "/", headers={"User-Agent": random.choice(USER_AGENTS)})
            time.sleep(random.uniform(1, 2))
        except Exception:
            pass
        self._initialized = True

    def _base_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    def _throttle(self):
        delay = random.uniform(*self.delay)
        time.sleep(delay)

    def _search_url(self, location: str, listing_type: str) -> str:
        """Build a Zillow search URL (page 1 only).

        Args:
            location: city-state slug, e.g. "austin-tx" or zip code
            listing_type: "sale" or "rent"
        """
        if listing_type == "rent":
            return f"{BASE_URL}/{location}/rentals/"
        return f"{BASE_URL}/{location}/"

    def scrape_search_page(self, url: str) -> tuple[list[Property], int, str | None]:
        """Scrape a single Zillow search results page.

        Returns:
            Tuple of (list of Property objects, total result count, next page URL or None).
        """
        self._ensure_session()
        self._throttle()
        self.client.headers["User-Agent"] = random.choice(USER_AGENTS)

        resp = self.client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script_tag:
            print(f"[WARN] No __NEXT_DATA__ found at {url}")
            print(f"[DEBUG] Response status: {resp.status_code}, length: {len(resp.text)}")
            return [], 0, None

        try:
            data = json.loads(script_tag.string)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[WARN] Failed to parse __NEXT_DATA__: {e}")
            return [], 0, None

        self._save_raw_debug(data, url)

        properties, total_count, next_url = self._parse_search_data(data, url)
        return properties, total_count, next_url

    def _save_raw_debug(self, data: dict, label: str, force: bool = False):
        """Save raw JSON for debugging. Always saves on errors; requires SAVE_RAW env var otherwise."""
        import os
        if not force and not os.environ.get("SAVE_RAW"):
            return
        from pathlib import Path
        raw_dir = Path(__file__).parent.parent / "data" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        slug = label.replace("https://www.zillow.com/", "").replace("/", "_").rstrip("_")
        slug = slug[:120]  # cap length
        path = raw_dir / f"{slug}.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"[DEBUG] Saved raw response to {path}")

    def _parse_search_data(self, data: dict, url: str = "") -> tuple[list[Property], int, str | None]:
        """Extract property listings from Zillow's __NEXT_DATA__ JSON."""
        properties = []
        total_count = 0
        next_url = None

        try:
            # Navigate the nested JSON structure to find search results
            query_data = data.get("props", {}).get("pageProps", {})
            search_data = query_data.get("searchPageState") or {}

            # Zillow uses cat1 for sale results and cat2 for rental results.
            # Try all categories and pick the one with actual results.
            best_results = []
            total_count = 0
            is_rental = "rental" in url.lower()

            # Also check categoryTotals as a fallback for total count
            category_totals = search_data.get("categoryTotals", {})

            for cat_name in (["cat2", "cat1"] if is_rental else ["cat1", "cat2"]):
                cat = search_data.get(cat_name, {})
                sr = cat.get("searchResults", {})
                sl = cat.get("searchList", {})
                lr = sr.get("listResults", [])
                mr = sr.get("mapResults", [])

                # totalResultCount can be in searchList, searchResults, or categoryTotals
                tc = (
                    sl.get("totalResultCount")
                    or sr.get("totalResultCount")
                    or category_totals.get(cat_name, {}).get("totalResultCount")
                    or 0
                )

                # Extract next page URL from pagination
                pagination = sl.get("pagination") or {}
                raw_next = pagination.get("nextUrl")
                if raw_next:
                    next_url = BASE_URL + raw_next if raw_next.startswith("/") else raw_next

                if lr or mr:
                    best_results = lr or mr
                    total_count = tc
                    break

        except (AttributeError, TypeError) as e:
            top_keys = list(data.get("props", {}).get("pageProps", {}).keys()) if isinstance(data, dict) else []
            print(f"[WARN] Unexpected JSON structure in search data ({e}); pageProps keys: {top_keys}")
            self._save_raw_debug(data, url + "_error", force=True)
            return [], 0, None

        all_results = best_results

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        for result in all_results:
            try:
                # For multi-unit buildings, create one record per unit
                units = result.get("units", [])
                if len(units) > 1:
                    for i, unit in enumerate(units):
                        unit_result = dict(result)
                        # Override price and beds from unit data
                        unit_result["_unit_price"] = unit.get("price")
                        unit_result["_unit_beds"] = unit.get("beds")
                        unit_result["_unit_baths"] = unit.get("baths")
                        unit_result["_unit_index"] = i
                        prop = self._parse_listing(unit_result, now)
                        if prop:
                            properties.append(prop)
                else:
                    prop = self._parse_listing(result, now)
                    if prop:
                        properties.append(prop)
            except Exception as e:
                zpid = result.get("zpid", "unknown")
                print(f"[WARN] Failed to parse listing {zpid}: {e}")

        return properties, total_count, next_url

    def _parse_listing(self, result: dict, timestamp: str) -> Optional[Property]:
        """Parse a single listing result into a Property."""
        zpid = str(result.get("zpid", ""))
        if not zpid:
            return None

        # For multi-unit entries, use unit-specific overrides
        unit_price_str = result.pop("_unit_price", None)
        unit_beds = result.pop("_unit_beds", None)
        unit_baths = result.pop("_unit_baths", None)
        unit_index = result.pop("_unit_index", None)

        if unit_index is not None:
            zpid = f"{zpid}_u{unit_index}"

        # Address parsing
        address = result.get("address", "")
        address_data = result.get("addressStreet", "")
        city = result.get("addressCity", "")
        state = result.get("addressState", "")
        zipcode = result.get("addressZipcode", "")

        # If address fields are missing, try parsing from the full address string
        if not city and address:
            parts = address.rsplit(",", 2)
            if len(parts) >= 2:
                address = parts[0].strip()
                state_zip = parts[-1].strip().split()
                if len(parts) >= 3:
                    city = parts[1].strip()
                if state_zip:
                    state = state_zip[0] if state_zip else ""
                    zipcode = state_zip[1] if len(state_zip) > 1 else ""

        display_address = address_data or address

        # Pricing — handle various formats:
        #   sale: 450000 or "$450,000"
        #   rent: "$2,400+ 1 bd" or "$2,400/mo" or in units array
        price = unit_price_str or result.get("unformattedPrice") or result.get("price")
        if isinstance(price, str):
            # Extract the first dollar amount from the string
            match = re.search(r"[\$]?([\d,]+)", price)
            if match:
                price = float(match.group(1).replace(",", ""))
            else:
                price = None
        elif isinstance(price, (int, float)):
            price = float(price)

        # For multi-unit rental buildings, check for units array
        units = result.get("units", [])
        if units and not price:
            # Use the cheapest unit price
            unit_prices = []
            for unit in units:
                up = unit.get("price")
                if isinstance(up, str):
                    match = re.search(r"[\$]?([\d,]+)", up)
                    if match:
                        unit_prices.append(float(match.group(1).replace(",", "")))
                elif isinstance(up, (int, float)):
                    unit_prices.append(float(up))
            if unit_prices:
                price = min(unit_prices)

        # Determine listing type from status or URL
        status = result.get("statusType", "").lower()
        detail_url = result.get("detailUrl", "")
        home_status = result.get("hdpData", {}).get("homeInfo", {}).get("homeStatus", "").upper() \
                      if result.get("hdpData") else ""
        # Builder/new-construction listings have "FOR_SALE" homeStatus even when scraped
        # from a rental search URL — detect them by status field before falling back to URL.
        if "for_sale" in status or home_status in ("FOR_SALE", "NEW_CONSTRUCTION"):
            listing_type = "sale"
        elif "rent" in status or "for_rent" in status or "/homedetails/" not in str(detail_url):
            listing_type = "rent"
        else:
            listing_type = "sale"

        # HD fields (more detailed data sometimes available)
        hd_data = result.get("hdpData", {}).get("homeInfo", {}) if result.get("hdpData") else {}

        rent_zestimate = hd_data.get("rentZestimate") or result.get("rentZestimate")
        zestimate = hd_data.get("zestimate") or result.get("zestimate")

        bedrooms = result.get("beds") or hd_data.get("bedrooms")
        bathrooms = result.get("baths") or hd_data.get("bathrooms")
        sqft = result.get("area") or hd_data.get("livingArea")
        property_type = hd_data.get("homeType") or result.get("homeType", "")

        # Use unit-level overrides for multi-unit buildings
        if unit_beds is not None:
            bedrooms = unit_beds
        if unit_baths is not None:
            bathrooms = unit_baths

        # For single-unit rental entries, try extracting from the units array or price string
        if not bedrooms and units:
            for unit in units:
                if unit.get("beds"):
                    bedrooms = unit["beds"]
                    if not bathrooms and unit.get("baths"):
                        bathrooms = unit["baths"]
                    break
        if not bedrooms:
            price_str = str(result.get("price", ""))
            match = re.search(r"(\d+)\s*bd", price_str)
            if match:
                bedrooms = int(match.group(1))

        # For rentals, also try top-level fields that may differ from sale listings
        if not bedrooms:
            bedrooms = result.get("bedrooms")
        if not bathrooms:
            bathrooms = result.get("bathrooms")
        if not sqft:
            sqft = result.get("livingArea") or result.get("sqft")
        if not property_type:
            property_type = result.get("buildingName") and "MULTI_FAMILY" or ""

        # Lot size — hdpData has lotAreaValue in acres, convert to sqft
        lot_sqft = hd_data.get("lotSize")
        if not lot_sqft and hd_data.get("lotAreaValue"):
            lot_area = hd_data["lotAreaValue"]
            lot_unit = hd_data.get("lotAreaUnit", "").lower()
            if lot_unit == "acres":
                lot_sqft = int(lot_area * 43560)
            elif lot_unit == "sqft" or lot_unit == "square feet":
                lot_sqft = int(lot_area)
            else:
                lot_sqft = int(lot_area)  # assume sqft

        # Year built
        year_built = hd_data.get("yearBuilt") or result.get("yearBuilt")

        # Tax assessed value — available in homeInfo from search results
        tax_assessed_value = hd_data.get("taxAssessedValue") or result.get("taxAssessedValue")

        lat = result.get("latLong", {}).get("latitude") or hd_data.get("latitude") or result.get("latitude")
        lng = result.get("latLong", {}).get("longitude") or hd_data.get("longitude") or result.get("longitude")

        # Days on Zillow — prefer numeric field from hdpData, fall back to text parsing
        days_on_zillow = hd_data.get("daysOnZillow")
        if days_on_zillow is None:
            # Try computing from timeOnZillow (milliseconds)
            time_on = hd_data.get("timeOnZillow")
            if time_on and isinstance(time_on, (int, float)):
                days_on_zillow = int(time_on / (1000 * 60 * 60 * 24))
        if days_on_zillow is None:
            # Last resort: parse from variableData text like "5 days on Zillow"
            days_on = result.get("variableData", {}).get("text", "")
            if "day" in str(days_on).lower():
                match = re.search(r"(\d+)", str(days_on))
                if match:
                    days_on_zillow = int(match.group(1))

        if detail_url and not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url

        # Coerce types — Zillow sometimes returns strings for numeric fields
        try:
            bedrooms = int(bedrooms) if bedrooms is not None else None
        except (ValueError, TypeError):
            bedrooms = None
        try:
            bathrooms = float(bathrooms) if bathrooms is not None else None
        except (ValueError, TypeError):
            bathrooms = None
        try:
            sqft = int(sqft) if sqft is not None else None
        except (ValueError, TypeError):
            sqft = None

        return Property(
            zpid=zpid,
            address=display_address,
            city=city,
            state=state,
            zipcode=zipcode,
            price=price,
            rent_zestimate=rent_zestimate,
            zestimate=zestimate,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            sqft=sqft,
            lot_sqft=lot_sqft,
            year_built=year_built,
            property_type=property_type,
            listing_type=listing_type,
            latitude=lat,
            longitude=lng,
            detail_url=detail_url,
            days_on_zillow=days_on_zillow,
            scraped_at=timestamp,
            tax_assessed_value=tax_assessed_value,
        )

    def scrape_property_detail(self, url: str) -> dict:
        """Scrape additional details from a property detail page.

        Returns raw data dict with rent zestimate, tax history, etc.
        """
        self._throttle()
        self.client.headers["User-Agent"] = random.choice(USER_AGENTS)

        resp = self.client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script_tag:
            return {}

        try:
            data = json.loads(script_tag.string)
        except (json.JSONDecodeError, TypeError):
            return {}

        # Extract property detail from the nested structure
        try:
            props = data.get("props", {}).get("pageProps", {})
            detail = (
                props.get("componentProps", {}).get("gdpClientCache", {})
                or props.get("gdpClientCache", {})
            )
            # gdpClientCache is keyed by a query string; get the first value
            if isinstance(detail, str):
                detail = json.loads(detail)
            if isinstance(detail, dict):
                for key in detail:
                    inner = detail[key]
                    if isinstance(inner, str):
                        inner = json.loads(inner)
                    if isinstance(inner, dict) and "property" in inner:
                        prop_data = inner["property"]
                        # Merge resoFacts fields so callers can access them at top level
                        reso = prop_data.get("resoFacts") or {}
                        for field in ("yearBuilt", "lotSize", "livingArea", "bathrooms",
                                      "bathroomsFloat", "taxAnnualAmount", "hoaFee",
                                      "annualHomeownersInsurance"):
                            if field not in prop_data or prop_data[field] is None:
                                if reso.get(field) is not None:
                                    prop_data[field] = reso[field]
                        if "bathroomsFloat" in prop_data and prop_data.get("bathrooms") is None:
                            prop_data["bathrooms"] = prop_data["bathroomsFloat"]
                        return prop_data
        except Exception:
            pass

        return {}

    def enrich_properties(
        self,
        properties: list[Property],
        skip_addresses: set[str] | None = None,
        on_enrich=None,
    ) -> list[Property]:
        """Fetch detail pages for properties missing data.

        Fills in rent Zestimates, year built, and other detail-page-only fields.
        Uses exponential backoff on 403s and creates a fresh HTTP client after
        rate limiting to get a new session.

        Args:
            properties: List of Property objects to enrich.
            skip_addresses: Set of addresses to skip (e.g. recently updated).
        """
        skip_addresses = skip_addresses or set()
        missing = [
            p for p in properties
            if p.detail_url and p.address not in skip_addresses
        ]
        if not missing:
            print("[*] All properties already have full detail data.")
            return properties

        print(f"[*] Enriching {len(missing)} properties with detail data (rent Zestimate, year built, sqft, etc.)...")
        original_delay = self.delay
        consecutive_403s = 0
        base_delay = 10  # start at 10s between detail page requests

        for i, prop in enumerate(missing):
            # Adaptive delay: increases with consecutive 403s
            current_delay = base_delay * (2 ** consecutive_403s)
            current_delay = min(current_delay, 120)  # cap at 2 minutes
            self.delay = (current_delay, current_delay + 30)

            print(f"[*] Detail {i+1}/{len(missing)}: {prop.address} (delay ~{current_delay:.0f}s)")
            try:
                detail = self.scrape_property_detail(prop.detail_url)
                consecutive_403s = 0  # reset on success

                if not detail:
                    continue

                if detail.get("rentZestimate"):
                    prop.rent_zestimate = detail["rentZestimate"]
                if detail.get("zestimate") and not prop.zestimate:
                    prop.zestimate = detail["zestimate"]
                if detail.get("yearBuilt") and not prop.year_built:
                    prop.year_built = detail["yearBuilt"]
                if detail.get("lotSize") and not prop.lot_sqft:
                    prop.lot_sqft = detail["lotSize"]
                if detail.get("livingArea") and not prop.sqft:
                    prop.sqft = detail["livingArea"]
                if detail.get("bathrooms") and not prop.bathrooms:
                    prop.bathrooms = detail["bathrooms"]
                if detail.get("taxAnnualAmount") and not prop.annual_tax:
                    prop.annual_tax = detail["taxAnnualAmount"]
                if detail.get("hoaFee") and not prop.hoa_fee:
                    prop.hoa_fee = detail["hoaFee"]
                if detail.get("annualHomeownersInsurance") and not prop.annual_homeowners_insurance:
                    prop.annual_homeowners_insurance = detail["annualHomeownersInsurance"]

                if on_enrich:
                    on_enrich(prop)

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    consecutive_403s += 1
                    if consecutive_403s >= 3:
                        print(f"[WARN] 3 consecutive 403s — stopping enrichment.")
                        break
                    print(f"[WARN] 403 — backing off (attempt {consecutive_403s}/3, next delay ~{base_delay * (2 ** consecutive_403s):.0f}s)")
                    # Create a fresh client to get new session cookies
                    self.client.close()
                    self.client = httpx.Client(
                        headers=self._base_headers(),
                        follow_redirects=True,
                        timeout=30.0,
                    )
                    self._initialized = False
                else:
                    print(f"[WARN] HTTP {e.response.status_code} for {prop.detail_url}")
            except Exception as e:
                print(f"[WARN] Failed to enrich {prop.zpid}: {e}")

        self.delay = original_delay
        enriched = sum(1 for p in properties if p.rent_zestimate)
        with_year = sum(1 for p in properties if p.year_built)
        print(f"[*] Enrichment complete: {enriched}/{len(properties)} have rent Zestimate, {with_year}/{len(properties)} have year built")
        return properties

    # Property types considered SFH/duplex (i.e. not large apartment complexes)
    SFH_TYPES = {"SINGLE_FAMILY", "DUPLEX", "TOWNHOUSE", "MANUFACTURED", "LOT"}

    def enrich_and_filter_rentals(
        self,
        properties: list[Property],
        skip_addresses: set[str] | None = None,
        on_enrich=None,
    ) -> list[Property]:
        """Enrich rental listings with detail page data to get accurate property types,
        then filter to keep only single-family homes and duplexes.

        Args:
            properties: List of rental Property objects.
            skip_addresses: Set of addresses to skip (already have accurate property_type in DB).

        Returns:
            Filtered list containing only SFH/duplex rentals.
        """
        skip_addresses = skip_addresses or set()

        # Properties that already have a known good type (from DB pre-load) can be filtered immediately
        already_typed = [p for p in properties if p.address in skip_addresses]
        needs_enrichment = [
            p for p in properties
            if p.address not in skip_addresses and p.detail_url
        ]

        if not needs_enrichment:
            print("[*] All rental properties already have property type data.")
        else:
            print(f"[*] Enriching {len(needs_enrichment)} rentals to get property types...")
            original_delay = self.delay
            consecutive_403s = 0
            base_delay = 10

            for i, prop in enumerate(needs_enrichment):
                current_delay = base_delay * (2 ** consecutive_403s)
                current_delay = min(current_delay, 120)
                self.delay = (current_delay, current_delay + 30)

                print(f"[*] Detail {i+1}/{len(needs_enrichment)}: {prop.address} (delay ~{current_delay:.0f}s)")
                try:
                    detail = self.scrape_property_detail(prop.detail_url)
                    consecutive_403s = 0

                    if not detail:
                        continue

                    if detail.get("homeType"):
                        prop.property_type = detail["homeType"]
                    if detail.get("rentZestimate") and not prop.rent_zestimate:
                        prop.rent_zestimate = detail["rentZestimate"]
                    if detail.get("bedrooms") and not prop.bedrooms:
                        prop.bedrooms = detail["bedrooms"]
                    if detail.get("bathrooms") and not prop.bathrooms:
                        prop.bathrooms = detail["bathrooms"]
                    if detail.get("livingArea") and not prop.sqft:
                        prop.sqft = detail["livingArea"]
                    if detail.get("yearBuilt") and not prop.year_built:
                        prop.year_built = detail["yearBuilt"]
                    if detail.get("taxAnnualAmount") and not prop.annual_tax:
                        prop.annual_tax = detail["taxAnnualAmount"]
                    if detail.get("hoaFee") and not prop.hoa_fee:
                        prop.hoa_fee = detail["hoaFee"]
                    if detail.get("annualHomeownersInsurance") and not prop.annual_homeowners_insurance:
                        prop.annual_homeowners_insurance = detail["annualHomeownersInsurance"]

                    if on_enrich:
                        on_enrich(prop)

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        consecutive_403s += 1
                        if consecutive_403s >= 3:
                            print(f"[WARN] 3 consecutive 403s — stopping enrichment.")
                            break
                        print(f"[WARN] 403 — backing off (attempt {consecutive_403s}/3)")
                        self.client.close()
                        self.client = httpx.Client(
                            headers=self._base_headers(),
                            follow_redirects=True,
                            timeout=30.0,
                        )
                        self._initialized = False
                    else:
                        print(f"[WARN] HTTP {e.response.status_code} for {prop.detail_url}")
                except Exception as e:
                    print(f"[WARN] Failed to enrich {prop.zpid}: {e}")

            self.delay = original_delay

        all_props = already_typed + needs_enrichment
        kept = [p for p in all_props if p.property_type in self.SFH_TYPES]
        excluded = len(all_props) - len(kept)

        type_counts = {}
        for p in all_props:
            t = p.property_type or "(unknown)"
            type_counts[t] = type_counts.get(t, 0) + 1

        print(f"[*] Rental property types: {type_counts}")
        print(f"[*] Kept {len(kept)} SFH/duplex rentals, excluded {excluded}")
        return kept

    def _construct_next_searchquery_url(self, base_url: str, page: int) -> Optional[str]:
        """Build a page-N URL for searchQueryState-based Zillow URLs.

        Zillow encodes all search parameters (including pagination) as JSON in the
        `searchQueryState` query param.  We update `pagination.currentPage` to get
        the next page without losing any filter or map-bounds settings.
        """
        parsed = urllib.parse.urlparse(base_url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "searchQueryState" not in params:
            return None
        try:
            state = json.loads(params["searchQueryState"][0])
            if page <= 1:
                state.pop("pagination", None)
            else:
                state["pagination"] = {"currentPage": page}
            params["searchQueryState"] = [json.dumps(state, separators=(",", ":"))]
            new_query = urllib.parse.urlencode(params, doseq=True)
            return urllib.parse.urlunparse(parsed._replace(query=new_query))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def search(
        self,
        location: str,
        listing_type: str = "sale",
        max_pages: int = 5,
        start_url: Optional[str] = None,
        on_page=None,
    ) -> list[Property]:
        """Search Zillow for properties.

        Args:
            location: Zillow location slug (e.g. "austin-tx", "90210", "chicago-il").
                      Ignored when start_url is provided.
            listing_type: "sale" or "rent"
            max_pages: maximum number of result pages to scrape (Zillow caps at 20)
            start_url: full Zillow search URL to use instead of building one from location.
                       Pass a URL copied from the browser to use saved filter settings.
            on_page: optional callback called with each page's new Property list as it arrives.

        Returns:
            List of Property objects.
        """
        all_properties = []
        seen_zpids = set()
        original_url = start_url or self._search_url(location, listing_type)
        current_url = original_url
        total_count = 0
        # For searchQueryState URLs we always build pagination ourselves because
        # Zillow's provided nextUrl strips the searchQueryState and loses all filters.
        use_searchquery_pagination = "searchQueryState" in original_url

        max_retries = 3

        for page in range(1, max_pages + 1):
            print(f"[*] Scraping {listing_type} listings: {current_url}")

            properties = None
            next_url = None
            for attempt in range(max_retries):
                try:
                    properties, total_count, next_url = self.scrape_search_page(current_url)
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403 and attempt < max_retries - 1:
                        wait = (attempt + 1) * 15
                        print(f"[WARN] 403 on page {page} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                        self.client.close()
                        self.client = httpx.Client(
                            headers=self._base_headers(),
                            follow_redirects=True,
                            timeout=30.0,
                        )
                        self._initialized = False
                        time.sleep(wait)
                        continue
                    print(f"[ERROR] HTTP {e.response.status_code} on page {page}")
                    return all_properties
                except Exception as e:
                    print(f"[ERROR] {e}")
                    return all_properties

            if properties is None:
                break

            if not properties:
                print(f"[*] No results on page {page}, stopping.")
                break

            # Deduplicate by zpid
            new_properties = [p for p in properties if p.zpid not in seen_zpids]
            seen_zpids.update(p.zpid for p in new_properties)
            all_properties.extend(new_properties)

            dupes = len(properties) - len(new_properties)
            dupe_msg = f", {dupes} duplicates skipped" if dupes else ""
            print(f"[*] Found {len(new_properties)} listings (page {page}, {len(all_properties)}/{total_count} total{dupe_msg})")

            if on_page and new_properties:
                on_page(new_properties)

            if total_count and len(all_properties) >= total_count:
                break

            if use_searchquery_pagination:
                # Always construct next page from the original URL — Zillow's provided
                # nextUrl strips the searchQueryState and loses all filters.
                next_url = self._construct_next_searchquery_url(original_url, page + 1)
                if not next_url:
                    print(f"[*] Could not construct next page URL, stopping.")
                    break
            elif not next_url:
                print(f"[*] No next page URL, stopping.")
                break

            current_url = next_url

        return all_properties

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
