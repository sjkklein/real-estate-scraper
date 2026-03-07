"""Zillow scraper for sale and rental listings."""

from __future__ import annotations

import json
import random
import re
import time
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

    def _search_url(self, location: str, listing_type: str, page: int = 1) -> str:
        """Build a Zillow search URL.

        Args:
            location: city-state slug, e.g. "austin-tx" or zip code
            listing_type: "sale" or "rent"
            page: page number (1-indexed)
        """
        if listing_type == "rent":
            suffix = "rentals"
        else:
            suffix = ""

        url = f"{BASE_URL}/{location}/{suffix}" if suffix else f"{BASE_URL}/{location}/"
        if page > 1:
            url += f"{page}_p/"
        return url

    def scrape_search_page(self, url: str) -> tuple[list[Property], int]:
        """Scrape a single Zillow search results page.

        Returns:
            Tuple of (list of Property objects, total result count).
        """
        self._ensure_session()
        self._throttle()
        self.client.headers["User-Agent"] = random.choice(USER_AGENTS)

        resp = self.client.get(url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # Zillow embeds search data in a <script id="__NEXT_DATA__"> tag
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script_tag:
            print(f"[WARN] No __NEXT_DATA__ found at {url}")
            print(f"[DEBUG] Response status: {resp.status_code}, length: {len(resp.text)}")
            return [], 0

        try:
            data = json.loads(script_tag.string)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[WARN] Failed to parse __NEXT_DATA__: {e}")
            return [], 0

        # Save raw JSON for debugging when SAVE_RAW env var is set
        import os
        if os.environ.get("SAVE_RAW"):
            from pathlib import Path
            raw_dir = Path(__file__).parent.parent / "data" / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            slug = url.replace("https://www.zillow.com/", "").replace("/", "_").rstrip("_")
            with open(raw_dir / f"{slug}.json", "w") as f:
                json.dump(data, f, indent=2, default=str)

        return self._parse_search_data(data, url)

    def _parse_search_data(self, data: dict, url: str = "") -> tuple[list[Property], int]:
        """Extract property listings from Zillow's __NEXT_DATA__ JSON."""
        properties = []
        total_count = 0

        try:
            # Navigate the nested JSON structure to find search results
            query_data = data.get("props", {}).get("pageProps", {})
            search_data = query_data.get("searchPageState", {})

            # Zillow uses cat1 for sale results and cat2 for rental results.
            # Try all categories and pick the one with actual results.
            best_results = []
            total_count = 0
            is_rental = "rental" in url.lower()

            for cat_name in (["cat2", "cat1"] if is_rental else ["cat1", "cat2"]):
                cat = search_data.get(cat_name, {})
                sr = cat.get("searchResults", {})
                lr = sr.get("listResults", [])
                mr = sr.get("mapResults", [])
                tc = sr.get("totalResultCount", 0)

                if lr or mr:
                    best_results = lr or mr
                    total_count = tc
                    break

        except (AttributeError, TypeError):
            print("[WARN] Unexpected JSON structure in search data")
            return [], 0

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

        return properties, total_count

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
        if "rent" in status or "for_rent" in status or "/homedetails/" not in str(detail_url):
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
                        return inner["property"]
        except Exception:
            pass

        return {}

    def enrich_properties(self, properties: list[Property]) -> list[Property]:
        """Fetch detail pages for properties missing data.

        Fills in rent Zestimates, year built, and other detail-page-only fields.
        Uses exponential backoff on 403s and creates a fresh HTTP client after
        rate limiting to get a new session.
        """
        missing = [p for p in properties if not p.rent_zestimate and p.detail_url]
        if not missing:
            print("[*] All properties already have rent Zestimates.")
            return properties

        print(f"[*] Enriching {len(missing)} properties missing rent Zestimates...")
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
        print(f"[*] {enriched}/{len(properties)} properties now have rent Zestimates")
        return properties

    def search(
        self,
        location: str,
        listing_type: str = "sale",
        max_pages: int = 5,
    ) -> list[Property]:
        """Search Zillow for properties.

        Args:
            location: Zillow location slug (e.g. "austin-tx", "90210", "chicago-il")
            listing_type: "sale" or "rent"
            max_pages: maximum number of result pages to scrape

        Returns:
            List of Property objects.
        """
        all_properties = []

        for page in range(1, max_pages + 1):
            url = self._search_url(location, listing_type, page)
            print(f"[*] Scraping {listing_type} listings: {url}")

            try:
                properties, total_count = self.scrape_search_page(url)
            except httpx.HTTPStatusError as e:
                print(f"[ERROR] HTTP {e.response.status_code} for {url}")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                break

            if not properties:
                print(f"[*] No results on page {page}, stopping.")
                break

            all_properties.extend(properties)
            print(f"[*] Found {len(properties)} listings (page {page}, {len(all_properties)}/{total_count} total)")

            # Stop if we've gotten all results
            if len(all_properties) >= total_count:
                break

        return all_properties

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
