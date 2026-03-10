"""Load and parse the YAML scraper configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

CHART_DEFAULTS: dict = {
    "down": 20.0,
    "rate": 7.0,
    "years": 30,
    "tax_rate": 1.2,
    "insurance_rate": 0.5,
}

# Hardcoded defaults — used when neither CLI nor config provides a value
SCRAPE_DEFAULTS: dict = {
    "type": "both",
    "pages": 5,
    "enrich": False,
    "skip_recent": None,
    "filter_rentals": False,
}


def load_config(path: Optional[Path] = None) -> dict:
    """Load config.yaml, returning an empty dict if not found or if pyyaml is missing."""
    p = path or CONFIG_PATH
    if not p.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print("[WARN] pyyaml not installed — config.yaml ignored. Run: pip install pyyaml")
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def resolve_search_entry(config: dict, key: str) -> tuple[Optional[str], dict]:
    """Look up a search key, returning (url, options_dict).

    The entry in config.yaml can be a plain URL string or a dict. Recognised
    option keys: url, type, pages, enrich, skip_recent, filter_rentals.
    """
    entry = config.get("searches", {}).get(key)
    if entry is None:
        return None, {}
    if isinstance(entry, str):
        return entry, {}
    url = entry.get("url")
    opts = {k: v for k, v in entry.items() if k != "url" and v is not None}
    return url, opts


def infer_listing_type(url: str) -> str:
    """Infer 'rent' or 'sale' from a Zillow URL."""
    return "rent" if "rental" in url.lower() else "sale"


def resolve_chart_options(config: dict, args) -> dict:
    """Merge config chart section + CLI args into final chart options dict.

    Priority: CLI args (when not None) > config.yaml [chart] > CHART_DEFAULTS.
    Returns a dict with keys: down, rate, years, tax_rate, insurance_rate.
    """
    opts = dict(CHART_DEFAULTS)
    opts.update({k: v for k, v in config.get("chart", {}).items() if v is not None})
    for key in ("down", "rate", "years", "tax_rate", "insurance_rate"):
        cli_val = getattr(args, key, None)
        if cli_val is not None:
            opts[key] = cli_val
    return opts


def resolve_scrape_options(config: dict, args) -> dict:
    """Merge config entry + CLI args into a final options dict.

    Priority (highest to lowest):
      1. CLI args (when explicitly provided, i.e. not None)
      2. config.yaml entry for the search key
      3. SCRAPE_DEFAULTS

    Returns a dict with keys: location, start_url, type, pages,
    enrich, skip_recent, filter_rentals.
    """
    location = args.location
    start_url = None

    # Start from hardcoded defaults
    opts = dict(SCRAPE_DEFAULTS)

    if location.startswith("http"):
        start_url = location
    else:
        cfg_url, cfg_opts = resolve_search_entry(config, location)
        if cfg_url:
            start_url = cfg_url
            # Apply config options over defaults
            opts.update(cfg_opts)
        elif config.get("searches") is not None and location not in config.get("searches", {}):
            # Location looks like a saved search name (not a plain city slug) but wasn't found
            available = list(config.get("searches", {}).keys())
            if available:
                print(f"[WARN] '{location}' not found in config.yaml searches. Available: {', '.join(available)}")
            else:
                print(f"[WARN] '{location}' not found in config.yaml (searches is empty). Add it or use a URL directly.")

    # Infer listing type from URL when config/CLI left it as "both"
    if start_url and opts.get("type") == "both":
        opts["type"] = infer_listing_type(start_url)

    # CLI args override everything — but only when not None (i.e. explicitly passed)
    for key in ("type", "pages", "enrich", "skip_recent", "filter_rentals"):
        cli_val = getattr(args, key.replace("-", "_"), None)
        if cli_val is not None:
            opts[key] = cli_val

    opts["location"] = location
    opts["start_url"] = start_url
    return opts
