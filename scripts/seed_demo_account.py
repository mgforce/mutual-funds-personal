"""
Seed (or re-seed) the bundled demo account so visitors can poke around the
dashboard without supplying real CAS data.

Idempotent — running again wipes the demo user/files and re-creates them.

Creates:
  - sqlite user demo@example.com / demo1234 (non-admin)
  - data/accounts/demo_example_com/cas/DEMO_CAS_<date>.pdf.enc  (1-page stub)
  - data/accounts/demo_example_com/parsed_cache.pkl.enc          (hand-built CAS)
  - data/accounts/demo_example_com/state.json

The dashboard reads the parse cache directly, so the placeholder PDF is
never re-parsed by casparser. The CAS PDF / Gmail App passwords stored for
the account are non-functional placeholders; the sidebar's "Refresh CAS" /
"Process inbox" buttons will fail if a visitor clicks them, which is fine —
the demo is read-only by intent.

Run from the repo root with the project's venv active:

    python scripts/seed_demo_account.py
"""
from __future__ import annotations

import json
import pickle
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import auth, crypto, db
from analytics.accounts import ACCOUNTS_DIR
from analytics.demo import DEMO_EMAIL, DEMO_PASSWORD, DEMO_SLUG


# ---------------------------------------------------------------------------
# Dummy CAS data
# ---------------------------------------------------------------------------

def _add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, min(d.day, 28))


def _purchase_tx(d: date, amount: float, nav: float, *, sip: bool) -> dict:
    units = round(amount / nav, 3)
    return {
        "date": d,
        "description": "Purchase - via SIP" if sip else "Purchase",
        "amount": round(amount, 2),
        "units": units,
        "nav": round(nav, 4),
        "balance": 0.0,  # filled by _finalize_scheme
        "type": "PURCHASE_SIP" if sip else "PURCHASE",
        "dividend_rate": None,
    }


def _redemption_tx(d: date, units: float, nav: float) -> dict:
    return {
        "date": d,
        "description": "Redemption",
        "amount": -round(units * nav, 2),
        "units": -round(units, 3),
        "nav": round(nav, 4),
        "balance": 0.0,
        "type": "REDEMPTION",
        "dividend_rate": None,
    }


def _sip_history(
    *, start: date, end: date, amount: float, day_of_month: int,
    nav_seed: float, monthly_growth: float,
) -> list[dict]:
    """Monthly SIP installments from `start` to `end`, geometric NAV growth."""
    out: list[dict] = []
    nav = nav_seed
    cur = date(start.year, start.month, min(day_of_month, 28))
    if cur < start:
        cur = _add_months(cur, 1)
    while cur <= end:
        out.append(_purchase_tx(cur, amount, nav, sip=True))
        nav *= (1 + monthly_growth)
        cur = _add_months(cur, 1)
    return out


def _stp_legs(
    *, start: date, end: date, amount: float, day_of_month: int,
    source_nav_seed: float, source_growth: float,
    target_nav_seed: float, target_growth: float,
    source_scheme_name: str, target_scheme_name: str,
) -> tuple[list[dict], list[dict]]:
    """Build matched STP redemption/purchase legs.

    Descriptions include the literal "STP" token so analytics.systematic's
    ``_is_transfer_leg`` flags both sides as STP legs — without that the
    pairing logic ignores them and they'd be misread as a standalone
    redeem + purchase pair."""
    out_txs: list[dict] = []
    in_txs: list[dict] = []
    src_nav = source_nav_seed
    tgt_nav = target_nav_seed
    cur = date(start.year, start.month, min(day_of_month, 28))
    if cur < start:
        cur = _add_months(cur, 1)
    while cur <= end:
        # Three decimals on units * four-decimal NAV produces rupee-level
        # rounding noise (~₹2 at NAV ~4720). The STP-pairing tolerance is 1.0,
        # so we record the *intent* amount on the tx and back the unit count
        # out from there — the few-paise residual the broker eats in reality.
        out_units = round(amount / src_nav, 5)
        out_txs.append({
            "date": cur,
            "description": f"STP Out - To {target_scheme_name}",
            "amount": -round(amount, 2),
            "units": -out_units,
            "nav": round(src_nav, 4),
            "balance": 0.0,
            "type": "REDEMPTION",
            "dividend_rate": None,
        })
        in_units = round(amount / tgt_nav, 3)
        in_txs.append({
            "date": cur,
            "description": f"STP In - From {source_scheme_name}",
            "amount": round(amount, 2),
            "units": in_units,
            "nav": round(tgt_nav, 4),
            "balance": 0.0,
            "type": "PURCHASE",
            "dividend_rate": None,
        })
        src_nav *= (1 + source_growth)
        tgt_nav *= (1 + target_growth)
        cur = _add_months(cur, 1)
    return out_txs, in_txs


def _finalize_scheme(
    *, name: str, isin: str, amfi: str, fund_type: str,
    rta_code: str, rta: str, txs: list[dict],
    current_nav: float, valuation_date: date,
) -> dict:
    """Compute running balance, close units, FIFO cost basis, and value
    consistent with what casparser's pipeline produces for a real scheme."""
    txs = sorted(txs, key=lambda t: t["date"])
    balance = 0.0
    lots: list[list[float]] = []
    for t in txs:
        u = float(t["units"])
        balance += u
        t["balance"] = round(balance, 3)
        if u > 0:
            lots.append([u, float(t["nav"])])
        elif u < 0:
            to_remove = -u
            while to_remove > 1e-6 and lots:
                if lots[0][0] <= to_remove + 1e-6:
                    to_remove -= lots[0][0]
                    lots.pop(0)
                else:
                    lots[0][0] -= to_remove
                    to_remove = 0
    close = round(balance, 3)
    cost = round(sum(l[0] * l[1] for l in lots), 2)
    value = round(close * current_nav, 2)
    return {
        "scheme": name,
        "advisor": "INA000004251",
        "rta_code": rta_code,
        "rta": rta,
        "type": fund_type,
        "isin": isin,
        "amfi": amfi,
        "nominees": [],
        "open": 0.0,
        "close": close,
        "close_calculated": close,
        "valuation": {
            "date": valuation_date,
            "nav": round(current_nav, 4),
            "cost": cost,
            "value": value,
        },
        "transactions": txs,
    }


INVESTOR_NAME = "Baburao Ganpatrao Apte (Demo)"
# Same person, but CAMS sometimes stored the names in a different order across
# folios — surface both so the per-folio "holder name" column has real variety.
SECOND_HOLDER_NAME = "Apte Baburao Ganpatrao (Demo)"


def build_dummy_cas(today: date) -> dict:
    """A creative-but-plausible dummy CAS for the demo account.

    Investor "Chintu Crorepati Chaturvedi" with six folios spanning equity
    SIPs (across two PPFAS folios with different holder-name orderings), a
    lumpsum + partial redemption (so the LTCG tracker has something to
    show), a parked liquid lumpsum, an active HDFC Liquid → HDFC Mid Cap
    STP, a multi-asset lumpsum, and a Nasdaq-100 FoF SIP — enough surface
    to cover every dashboard section."""

    # === STP plan: HDFC Liquid → HDFC Mid Cap, ₹15k/month for the last
    # five-ish months, finishing within 45 days of today so the systematic
    # detector marks it active. ===
    stp_target = "HDFC Mid Cap Opportunities Fund - Direct Plan - Growth"
    stp_source = "HDFC Liquid Fund - Direct Plan - Growth"
    # Last installment must land inside ACTIVE_WINDOW_DAYS (45) for the
    # detector to flag the STP as live — let the end float to today and
    # rely on the day-of-month gate inside _stp_legs to only emit legs
    # whose scheduled day has already passed.
    stp_start = _add_months(today, -5)
    stp_end = today
    stp_outs, stp_ins = _stp_legs(
        start=stp_start, end=stp_end,
        amount=15000.0, day_of_month=5,
        source_nav_seed=4720.0, source_growth=0.003,
        target_nav_seed=117.0, target_growth=0.012,
        source_scheme_name=stp_source,
        target_scheme_name=stp_target,
    )

    folios = [
        # === PPFAS folio #1 — primary holder, active ₹10k monthly SIP into Flexi Cap ===
        {
            "folio": "1234567 / 89",
            "amc": "PPFAS Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name="Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
                    isin="INF879O01019DEMO",
                    amfi="122639",
                    fund_type="EQUITY",
                    rta_code="PPFAS",
                    rta="KFINTECH",
                    txs=_sip_history(
                        start=date(2022, 1, 5), end=today,
                        amount=10000.0, day_of_month=5,
                        nav_seed=42.0, monthly_growth=0.0125,
                    ),
                    current_nav=round(42.0 * (1.0125 ** 52), 2),
                    valuation_date=today,
                ),
            ],
        },

        # === PPFAS folio #2 — same investor, holder-name field stored in
        # last/first/middle order. Smaller ₹3k SIP into the same Flexi Cap
        # scheme so the dashboard's per-folio breakdown shows both names
        # under one consolidated row. ===
        {
            "folio": "2345678 / 90",
            "amc": "PPFAS Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name="Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
                    isin="INF879O01019DEMO",
                    amfi="122639",
                    fund_type="EQUITY",
                    rta_code="PPFAS",
                    rta="KFINTECH",
                    txs=_sip_history(
                        start=date(2024, 6, 7), end=today,
                        amount=3000.0, day_of_month=7,
                        nav_seed=68.0, monthly_growth=0.0125,
                    ),
                    current_nav=round(42.0 * (1.0125 ** 52), 2),
                    valuation_date=today,
                ),
            ],
        },

        # === Mirae — 2020 lumpsum + 2024 partial redemption (LTCG demo) ===
        {
            "folio": "98765432",
            "amc": "Mirae Asset Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name="Mirae Asset Large Cap Fund - Direct Plan - Growth",
                    isin="INF769K01AX2DEMO",
                    amfi="118834",
                    fund_type="EQUITY",
                    rta_code="MIRAE",
                    rta="KFINTECH",
                    # Redemption is placed inside the *current* FY so the
                    # dashboard's "Realized Equity LTCG (FY …)" card has a
                    # non-zero number to display. The lot is >12 months old
                    # so the gain lands in the LTCG bucket.
                    txs=[
                        _purchase_tx(date(2020, 7, 15), 200000.0, 50.0, sip=False),
                        _redemption_tx(
                            _add_months(date(today.year, 4, 15), 0 if today >= date(today.year, 4, 15) else -12),
                            1500.0, 92.0,
                        ),
                    ],
                    current_nav=98.5,
                    valuation_date=today,
                ),
            ],
        },

        # === HDFC — stopped Mid-Cap SIP (active vs total demo), liquid
        # parking, AND an active STP that redeems from Liquid into Mid Cap. ===
        {
            "folio": "21345600 / 00",
            "amc": "HDFC Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name=stp_target,
                    isin="INF179K01YV8DEMO",
                    amfi="118989",
                    fund_type="EQUITY",
                    rta_code="HDFC",
                    rta="CAMS",
                    txs=_sip_history(
                        start=date(2023, 3, 1), end=date(2024, 12, 1),
                        amount=5000.0, day_of_month=1,
                        nav_seed=80.0, monthly_growth=0.012,
                    ) + stp_ins,
                    current_nav=132.5,
                    valuation_date=today,
                ),
                _finalize_scheme(
                    name=stp_source,
                    isin="INF179K01YR6DEMO",
                    amfi="100193",
                    fund_type="DEBT",
                    rta_code="HDFC",
                    rta="CAMS",
                    txs=[
                        _purchase_tx(date(2024, 11, 1), 250000.0, 4600.5, sip=False),
                    ] + stp_outs,
                    current_nav=4800.25,
                    valuation_date=today,
                ),
            ],
        },

        # === ICICI — Multi-Asset lumpsum ===
        {
            "folio": "55667788 / 90",
            "amc": "ICICI Prudential Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name="ICICI Prudential Multi-Asset Fund - Direct Plan - Growth",
                    # categorize.py routes "multi asset" in the scheme name to MULTI_ASSET
                    # regardless of the casparser-assigned type, so OTHER here is fine.
                    isin="INF109K016L0DEMO",
                    amfi="120586",
                    fund_type="OTHER",
                    rta_code="ICICI",
                    rta="CAMS",
                    txs=[
                        _purchase_tx(date(2023, 2, 10), 150000.0, 350.50, sip=False),
                    ],
                    current_nav=540.25,
                    valuation_date=today,
                ),
            ],
        },

        # === Motilal Oswal — active Nasdaq-100 SIP (FOREIGN bucket) ===
        {
            "folio": "77889966 / 22",
            "amc": "Motilal Oswal Mutual Fund",
            "PAN": "AABCP1234F",
            "KYC": "OK",
            "PANKYC": "OK",
            "schemes": [
                _finalize_scheme(
                    name="Motilal Oswal NASDAQ 100 Fund of Fund - Direct Plan - Growth",
                    isin="INF247L01882DEMO",
                    amfi="146855",
                    fund_type="EQUITY",
                    rta_code="MOTILAL",
                    rta="KFINTECH",
                    txs=_sip_history(
                        start=date(2023, 6, 5), end=today,
                        amount=3000.0, day_of_month=5,
                        nav_seed=22.5, monthly_growth=0.014,
                    ),
                    current_nav=round(22.5 * (1.014 ** 36), 2),
                    valuation_date=today,
                ),
            ],
        },
    ]

    # The second PPFAS folio carries the holder name in last/first/middle
    # order — same investor, different order on the application form.
    second_ppfas_folio = "2345678 / 90"
    folio_names = {
        f["folio"]: (SECOND_HOLDER_NAME if f["folio"] == second_ppfas_folio else INVESTOR_NAME)
        for f in folios
    }

    return {
        "statement_period": {"from": "01-Jan-2014", "to": today.strftime("%d-%b-%Y")},
        "cas_type": "DETAILED",
        "file_type": "CAMS",
        "investor_info": {
            "name": INVESTOR_NAME,
            "email": DEMO_EMAIL,
            "address": "DEMO ACCOUNT — all data shown is fictional.",
            "mobile": "98XXXXXX12",
        },
        "folios": folios,
        "_folio_names": folio_names,
    }


# ---------------------------------------------------------------------------
# Placeholder PDF — never re-parsed by casparser, only exists so the
# parsed-cache filename/size check has something to point at.
# ---------------------------------------------------------------------------

def _make_placeholder_pdf() -> bytes:
    body = (
        b"BT /F1 18 Tf 72 720 Td (DEMO CAS - NOT REAL DATA) Tj "
        b"0 -28 Td (Generated by scripts/seed_demo_account.py) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_start).encode() + b"\n%%EOF\n"
    return bytes(out)


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def _wipe_demo() -> None:
    with db.connect() as c:
        c.execute("DELETE FROM account_access WHERE account_slug = ?", (DEMO_SLUG,))
        c.execute("DELETE FROM cas_accounts WHERE slug = ?", (DEMO_SLUG,))
        c.execute("DELETE FROM users WHERE email = ?", (DEMO_EMAIL,))
    target = ACCOUNTS_DIR / DEMO_SLUG
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def _create_demo_user() -> bytes:
    """Insert the demo user + cas_account + access row. Returns the data
    key so the caller can encrypt the placeholder PDF and parsed cache
    without re-deriving the KEK."""
    pwd_hash = crypto.hash_password(DEMO_PASSWORD)
    kek_salt = crypto.new_kek_salt()
    kek = crypto.derive_kek(DEMO_PASSWORD, kek_salt)
    data_key = crypto.new_data_key()
    wrapped = crypto.wrap_key(data_key, kek)
    # Placeholder creds — pass needs_setup() so the dashboard renders without
    # routing the user to /setup. The dashboard's Refresh-CAS buttons will
    # fail if anyone clicks them; that's intentional for the demo.
    pdf_pw_enc = crypto.encrypt_str("Demo1234", data_key)
    app_pw_enc = crypto.encrypt_str("demo-no-real-gmail", data_key)
    now = auth._now()
    with db.connect() as c:
        c.execute(
            "INSERT INTO users (email, password_hash, kek_salt, is_admin, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (DEMO_EMAIL, pwd_hash, kek_salt, now),
        )
        c.execute(
            "INSERT INTO cas_accounts (slug, email, from_date, enc_pdf_password, enc_app_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (DEMO_SLUG, DEMO_EMAIL, "2014-01-01", pdf_pw_enc, app_pw_enc, now),
        )
        c.execute(
            "INSERT INTO account_access (user_email, account_slug, wrapped_data_key, is_owner, granted_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (DEMO_EMAIL, DEMO_SLUG, wrapped, now),
        )
    return data_key


def _write_files(data_key: bytes, today: date) -> Path:
    base = ACCOUNTS_DIR / DEMO_SLUG
    cas_dir = base / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = _make_placeholder_pdf()
    enc_pdf = crypto.encrypt_bytes(pdf_bytes, data_key)
    pdf_name = f"DEMO_CAS_01012014-{today.strftime('%d%m%Y')}.pdf.enc"
    pdf_path = cas_dir / pdf_name
    pdf_path.write_bytes(enc_pdf)

    cas = build_dummy_cas(today)
    cache = {
        "pdf_name": pdf_path.name,
        "pdf_size": pdf_path.stat().st_size,
        "data": cas,
    }
    cache_blob = crypto.encrypt_bytes(pickle.dumps(cache), data_key)
    (base / "parsed_cache.pkl.enc").write_bytes(cache_blob)

    (base / "state.json").write_text(json.dumps({
        "last_fetched_pdf": str(pdf_path),
        "last_fetched_at": today.strftime("%Y-%m-%dT00:00:00"),
    }, indent=2))
    return pdf_path


def seed() -> None:
    db.init_schema()
    _wipe_demo()
    data_key = _create_demo_user()
    pdf_path = _write_files(data_key, today=date.today())
    print("OK · demo account seeded.")
    print(f"  login : {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"  slug  : {DEMO_SLUG}")
    print(f"  pdf   : {pdf_path}")


if __name__ == "__main__":
    seed()
