"""
Fetch the latest NAVs published by AMFI (https://www.amfiindia.com).

The endpoint is a free, unauthenticated pipe-separated daily file. Cached
locally per-day so we hit it at most once per session per day.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAV_CACHE = ROOT / "data" / "nav_cache.json"
AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def _fetch_amfi() -> dict[str, tuple[float, str]]:
    """Returns ISIN → (NAV, ISO date string). Pure HTTP, no auth."""
    req = urllib.request.Request(AMFI_URL, headers={"User-Agent": "mfportfolio/0.1"})
    text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", errors="ignore")
    nav: dict[str, tuple[float, str]] = {}
    for line in text.splitlines():
        if not line.strip() or ";" not in line or line.startswith("Scheme Code"):
            continue
        parts = line.split(";")
        # AMFI publishes 6 columns: code; isin_div; isin_growth; name; nav; date
        if len(parts) < 6:
            continue
        isin_div, isin_growth = parts[1], parts[2]
        nav_str, nav_date_str = parts[4].strip(), parts[5].strip()
        if not nav_str or nav_str.upper() in {"N.A.", "NA", "-"}:
            continue
        try:
            nav_val = float(nav_str)
            d = datetime.strptime(nav_date_str, "%d-%b-%Y").date()
        except ValueError:
            continue
        for isin in (isin_div.strip(), isin_growth.strip()):
            if isin and isin != "-":
                nav[isin] = (nav_val, d.isoformat())
    return nav


def get_latest_nav(force_refresh: bool = False) -> dict[str, tuple[float, date]]:
    """ISIN → (latest NAV, NAV date). Cached for the calendar day."""
    if not force_refresh and NAV_CACHE.exists():
        try:
            cached = json.loads(NAV_CACHE.read_text())
            if cached.get("fetched_on") == date.today().isoformat():
                return {
                    k: (float(v[0]), date.fromisoformat(v[1]))
                    for k, v in cached["nav"].items()
                }
        except Exception:
            pass

    raw = _fetch_amfi()
    NAV_CACHE.parent.mkdir(parents=True, exist_ok=True)
    NAV_CACHE.write_text(
        json.dumps({"fetched_on": date.today().isoformat(), "nav": raw})
    )
    return {k: (float(v[0]), date.fromisoformat(v[1])) for k, v in raw.items()}
