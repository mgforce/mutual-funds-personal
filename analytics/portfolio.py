"""
Load the latest CAS, build per-scheme summaries (folios merged), and expose
helpers used by the Streamlit UI.
"""
from __future__ import annotations

import os
import pickle
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

from casparser import read_cas_pdf

from analytics import crypto
from analytics.accounts import AccountContext
from analytics.categorize import adjusted_type, subcategory
from analytics.folio_names import extract_folio_names

ROOT = Path(__file__).resolve().parent.parent

# Only include transactions that represent an actual investor cashflow (or its
# scheme-level proxy). DIVIDEND_REINVEST does not move cash; the small TAX
# lines are already netted into the corresponding purchase/redemption amount.
RELEVANT_TX_TYPES = {
    "PURCHASE",
    "PURCHASE_SIP",
    "REDEMPTION",
    "DIVIDEND_PAYOUT",
    "SWITCH_IN",
    "SWITCH_OUT",
}


@dataclass
class FolioEntry:
    """One scheme inside one folio — the smallest unit casparser exposes."""
    folio: str
    invested: float
    current_value: float
    units: float
    nav: float
    nav_date: date
    holder_name: str = ""
    xirr: float | None = None
    cashflows: list[tuple[date, float]] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return self.current_value - self.invested

    @property
    def gain_pct(self) -> float:
        return (self.gain / self.invested) if self.invested else 0.0


@dataclass
class SchemeRow:
    folios: list[str]
    amc: str
    scheme: str
    isin: str
    type: str
    sub_type: str
    invested: float
    current_value: float
    units: float
    nav: float
    nav_date: date
    xirr: float | None
    cashflows: list[tuple[date, float]] = field(default_factory=list)
    nav_source: str = "CAS"   # "AMFI" when overridden by latest NAV
    folio_details: list[FolioEntry] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return self.current_value - self.invested

    @property
    def gain_pct(self) -> float:
        return (self.gain / self.invested) if self.invested else 0.0

    @property
    def plan_type(self) -> str:
        # Pre-2013 schemes have neither keyword; they're all Regular.
        return "Direct" if "direct" in self.scheme.lower() else "Regular"


def latest_pdf_enc(ctx: AccountContext) -> Path:
    """Latest encrypted CAS file on disk for this account."""
    d = ctx.cas_dir
    files = sorted(d.glob("*.pdf.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"No CAS PDFs in {d}. Run CAS Refresh first.")
    return files[0]


@contextmanager
def _decrypted_pdf(enc_path: Path, data_key: bytes):
    """Decrypt the .pdf.enc into a private temp file, yield its path, then
    securely delete the temp file. The decrypted PDF stays password-protected
    (CAMS password) — we never write plaintext PDF content to disk."""
    plaintext = crypto.decrypt_bytes(enc_path.read_bytes(), data_key)
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="cas_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(plaintext)
        yield Path(tmp_path)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def parse_cas(ctx: AccountContext, force: bool = False) -> dict:
    """Parse the latest CAS PDF for this account. Caches the parsed dict
    (encrypted) to disk so repeat sessions don't re-run the parser when the
    source PDF is unchanged."""
    enc_pdf = latest_pdf_enc(ctx)
    pdf_size = enc_pdf.stat().st_size
    cache_path = ctx.parse_cache_path

    if not force and cache_path.exists():
        try:
            blob = crypto.decrypt_bytes(cache_path.read_bytes(), ctx.data_key)
            cache = pickle.loads(blob)
            if cache.get("pdf_name") == enc_pdf.name and cache.get("pdf_size") == pdf_size:
                return cache["data"]
        except Exception:
            pass  # corrupted / wrong key / version mismatch — re-parse

    with _decrypted_pdf(enc_pdf, ctx.data_key) as plain_pdf:
        raw = read_cas_pdf(str(plain_pdf), ctx.pdf_password, output="dict")
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        data["_folio_names"] = extract_folio_names(plain_pdf, ctx.pdf_password)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    blob = pickle.dumps({"pdf_name": enc_pdf.name, "pdf_size": pdf_size, "data": data})
    cache_path.write_bytes(crypto.encrypt_bytes(blob, ctx.data_key))
    return data


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Bisection XIRR. Cashflows: list of (date, amount). Investor outflows
    are negative, inflows positive. Returns annualised rate or None."""
    if len(cashflows) < 2:
        return None
    if not (any(a > 0 for _, a in cashflows) and any(a < 0 for _, a in cashflows)):
        return None

    base = min(d for d, _ in cashflows)
    days = [(d - base).days for d, _ in cashflows]
    amts = [float(a) for _, a in cashflows]

    def npv(rate: float) -> float:
        return sum(a / ((1 + rate) ** (t / 365.0)) for a, t in zip(amts, days))

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        hi = 100.0
        fhi = npv(hi)
        if flo * fhi > 0:
            return None

    for _ in range(200):
        mid = (lo + hi) / 2
        fmid = npv(mid)
        if abs(fmid) < 1e-3 or (hi - lo) < 1e-9:
            return mid
        if fmid * flo < 0:
            hi, fhi = mid, fmid
        else:
            lo, flo = mid, fmid
    return mid


def _tx_type(t: dict) -> str:
    val = t.get("type")
    if val is None:
        return ""
    if hasattr(val, "value"):
        val = val.value
    return str(val).upper()


def _scheme_cashflows(scheme: dict) -> list[tuple[date, float]]:
    flows: list[tuple[date, float]] = []
    for t in scheme.get("transactions") or []:
        if _tx_type(t) not in RELEVANT_TX_TYPES:
            continue
        amt = t.get("amount")
        if amt is None:
            continue
        flows.append((t["date"], -float(amt)))
    return flows


def _normalize_transactions(scheme: dict) -> list[dict]:
    """Flatten casparser's tx records to plain dicts the UI can render."""
    out: list[dict] = []
    for t in scheme.get("transactions") or []:
        out.append({
            "date": t.get("date"),
            "type": _tx_type(t),
            "description": t.get("description") or "",
            "amount": t.get("amount"),
            "units": t.get("units"),
            "nav": t.get("nav"),
            "balance": t.get("balance"),
        })
    return out


def to_scheme_rows(
    cas: dict,
    nav_lookup: dict[str, tuple[float, date]] | None = None,
) -> list[SchemeRow]:
    """Group by (scheme name, ISIN) so multiple folios in the same scheme/plan
    collapse to a single row. Direct vs Regular have different ISINs and stay
    separate. If nav_lookup is provided (ISIN → (NAV, date)), each scheme's
    current value is recalculated from its unit balance × latest NAV — gives
    XIRR a fresh terminal cashflow that matches what brokers show today."""
    grouped: dict[tuple[str, str], list[tuple[str, str, dict]]] = {}
    for folio in cas.get("folios", []) or []:
        amc = folio.get("amc") or ""
        folio_no = str(folio.get("folio") or "")
        for s in folio.get("schemes", []) or []:
            key = (str(s.get("scheme") or ""), str(s.get("isin") or ""))
            grouped.setdefault(key, []).append((amc, folio_no, s))

    nav_lookup = nav_lookup or {}
    folio_names = cas.get("_folio_names") or {}
    rows: list[SchemeRow] = []
    for (scheme_name, isin), entries in grouped.items():
        type_str = "OTHER"
        amc = entries[0][0]
        folio_entries: list[FolioEntry] = []

        for amc_x, folio_no, s in entries:
            val = s.get("valuation") or {}
            f_invested = float(val.get("cost") or 0)
            f_cas_value = float(val.get("value") or 0)
            f_units = float(s.get("close_calculated") or s.get("close") or 0)
            f_nav = float(val.get("nav") or 0)
            f_nav_date = val.get("date") if isinstance(val.get("date"), date) else date.today()
            type_str = str(s.get("type") or type_str).upper()

            folio_entries.append(FolioEntry(
                folio=folio_no,
                invested=f_invested,
                current_value=f_cas_value,
                units=f_units,
                nav=f_nav,
                nav_date=f_nav_date,
                holder_name=folio_names.get(folio_no, ""),
                cashflows=_scheme_cashflows(s),
                transactions=_normalize_transactions(s),
            ))

        invested = sum(f.invested for f in folio_entries)
        cas_value = sum(f.current_value for f in folio_entries)
        units = sum(f.units for f in folio_entries)
        nav = next((f.nav for f in folio_entries if f.nav), 0.0)
        nav_date = max((f.nav_date for f in folio_entries), default=date.today())
        nav_source = "CAS"

        amfi = nav_lookup.get(isin)
        if amfi and units > 0:
            amfi_nav, amfi_date = amfi
            if amfi_date >= nav_date:
                nav = amfi_nav
                nav_date = amfi_date
                nav_source = "AMFI"
                for f in folio_entries:
                    f.nav = amfi_nav
                    f.nav_date = amfi_date
                    f.current_value = round(f.units * amfi_nav, 2)
        current_value = sum(f.current_value for f in folio_entries)

        for f in folio_entries:
            f_full = list(f.cashflows)
            if f.current_value > 0:
                f_full.append((f.nav_date, f.current_value))
            try:
                f.xirr = xirr(f_full) if f_full else None
            except Exception:
                f.xirr = None

        full_cashflows: list[tuple[date, float]] = []
        for f in folio_entries:
            full_cashflows.extend(f.cashflows)
        if current_value > 0:
            full_cashflows.append((nav_date, current_value))
        try:
            rate = xirr(full_cashflows) if full_cashflows else None
        except Exception:
            rate = None

        final_type = adjusted_type(scheme_name, type_str)
        rows.append(SchemeRow(
            folios=sorted({f.folio for f in folio_entries}),
            amc=amc,
            scheme=scheme_name,
            isin=isin,
            type=final_type,
            sub_type=subcategory(scheme_name, final_type),
            invested=invested,
            current_value=current_value,
            units=units,
            nav=nav,
            nav_date=nav_date,
            xirr=rate,
            cashflows=full_cashflows,
            nav_source=nav_source,
            folio_details=folio_entries,
        ))

    rows.sort(key=lambda r: r.current_value, reverse=True)
    return rows


def combined_xirr(rows: Iterable[SchemeRow]) -> float | None:
    """Combined XIRR over an arbitrary subset of rows — uses cached cashflows."""
    flows: list[tuple[date, float]] = []
    for r in rows:
        flows.extend(r.cashflows)
    return xirr(flows) if flows else None


def filter_rows(rows: Iterable[SchemeRow], type_filter: str) -> list[SchemeRow]:
    if type_filter == "ALL":
        return list(rows)
    return [r for r in rows if r.type == type_filter]
