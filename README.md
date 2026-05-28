# mutual-funds-personal

A local, private mutual-fund portfolio tracker for Indian investors. Pulls your CAS (Consolidated Account Statement) from CAMS by email, parses it, and serves a Streamlit dashboard with holdings, gains, allocation, and tax info — entirely on your machine, no data ever leaves.

---

> ### 🧪 Try the live demo
>
> **[finance.twounderthesky.com](https://finance.twounderthesky.com)** — login `demo@example.com` / `demo1234`
>
> All data shown is fictional. The demo serves a hand-crafted sample CAS for investor "Baburao Ganpatrao Apte" with 6 schemes across Equity / Debt / Multi-Asset / Foreign, two active SIPs across two PPFAS folios, an HDFC Liquid → HDFC Mid Cap STP, and a current-FY LTCG redemption — enough to exercise every dashboard surface (filters, allocation donut, scheme detail pages, redemption calculator, SIPs & STPs tab). The Refresh-CAS / Process-inbox buttons are simulated on the demo path; no real CAMS or Gmail traffic is generated.

---

## What you get

- Auto-fetch the latest CAS PDF from your Gmail (encrypted at rest on disk)
- Holdings, current value, and unrealised gains using live AMFI NAV
- **XIRR** per scheme and combined portfolio XIRR
- Allocation donut by asset class / sub-category
- **Equity LTCG FY tracker** — realised gain this FY + room left under the ₹1.25L exemption
- Per-scheme redemption calculator (LTCG / STCG split, lot-by-lot FIFO breakdown)
- Per-folio holder names, scheme detail pages with persistent URLs
- **Multi-user**: admin invites others by token; each user owns one CAS account
- **View multiple accounts in one place** — switch between your own and any linked accounts (e.g. spouse, parents) from a sidebar dropdown; unlink anytime
- Everything encrypted under each user's login password — disk theft alone gets you nothing

## Setup

Requires Python 3.9+, a Gmail account, and a Gmail [App Password](https://myaccount.google.com/apppasswords).

```bash
git clone https://github.com/<you>/mutual-funds-personal.git
cd mutual-funds-personal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium       # ~150 MB; needed for the CAS request flow
cp config.example.yaml config.yaml
# edit config.yaml — set admin_email to your Gmail
```

## Running

You need **both** processes running side-by-side — the auth gateway can't render the dashboard without the Streamlit backend, and Streamlit can't authenticate users without the gateway.

```bash
# terminal 1: Streamlit dashboard backend (port 8501)
streamlit run ui/app.py

# terminal 2: FastAPI auth gateway (port 8000 — this is what you'll visit)
uvicorn auth_server.main:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** (use `0.0.0.0` so phones/laptops on the same Wi-Fi can reach it too). On first launch you'll be guided through:

1. **Admin signup** — the email you set as `admin_email` in `config.yaml` becomes the only account that can invite others.
2. **Setup** — Gmail App Password + a CAS PDF password.
3. **First CAS fetch** — the form is submitted to CAMS automatically and the dashboard polls Gmail until the statement lands.

To invite a second user (e.g. spouse), open **⚙️ Settings → 👥 Invite a user** in the sidebar — share the one-time link out of band.

> You never visit `:8501` directly. The dashboard reads its session from a signed header the gateway injects on every proxied request; direct access shows an error. Streamlit's port / XSRF / CORS settings are baked into `.streamlit/config.toml` so the command stays simple.

### Seeding a local demo account

If you're publicly hosting your instance and want a "kick the tyres" account visitors can use without seeing real data, run:

```bash
python scripts/seed_demo_account.py
```

This creates `demo@example.com` / `demo1234` (non-admin), writes a hand-crafted dummy CAS to `data/accounts/demo_example_com/`, and the dashboard automatically shows a "this is fictional" banner whenever the demo is the active account. The Refresh-CAS / Process-inbox buttons are stubbed on this path so visitors don't trigger real CAMS or Gmail traffic. Idempotent — re-run any time to refresh the sample data.

## How it works

| Layer | What it does |
|---|---|
| `auth_server/`           | FastAPI auth gateway: login, signup, invite, setup, change-password, delete-account, migration. HTTP-only signed cookies. Reverse-proxies authenticated requests to Streamlit |
| `ui/`                    | Streamlit dashboard split into focused modules: `app.py` (orchestrator), `sidebar.py`, `scheme_card.py`, `scheme_detail.py`, `cas_workflow.py`, `donut.py`, `format.py`, `query.py`, `auth_glue.py` |
| `ingest/cams_request.py` | Fills the CAMS Mailback form via Playwright |
| `ingest/gmail_fetch.py`  | Pulls the CAS PDF reply from Gmail over IMAP and writes it encrypted to disk |
| `analytics/auth.py`      | Argon2id login, KEK derivation, invite + linking, account CRUD |
| `analytics/crypto.py`    | Argon2id KDF, Fernet wrap/unwrap for data keys + CAS PDFs + parse cache |
| `analytics/db.py`        | SQLite store: users, cas_accounts, account_access, invites |
| `analytics/portfolio.py` | Parses the CAS PDF, computes positions, XIRR, AMFI NAV overlay |
| `analytics/tax.py`       | FIFO lot tracking, LTCG / STCG bucketing, FY exemption math |

All artefacts (encrypted PDFs, parsed data, SQLite store, server secret) live under `data/` — gitignored, never synced. Debug captures under `debug/` are also gitignored.

## License

Copyright © 2026 Kesha Shah. Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

**TL;DR:**

- ✅ Run it for yourself, your family, your hobby project
- ✅ Fork it, modify it, send pull requests, redistribute changes
- ✅ Use it for education, research, or in a non-profit / charity / government context
- ❌ Use it in any product, service, or workflow tied to revenue — commercial use of any kind requires a separate license

The full canonical text is in [LICENSE](LICENSE); see [polyformproject.org/licenses/noncommercial/1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) for the project's plain-English explanation.
