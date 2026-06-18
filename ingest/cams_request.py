"""
Submit the CAMS Consolidated Account Statement (CAS) request form.

The CAS PDF is emailed to the address registered in your MF folios.
This script only fills and submits the public form — no login involved.

The form requires a "PDF password" the user picks; CAMS uses it to encrypt
the emailed PDF. We default this to the account's login email (the user can
override in Settings).
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from analytics.accounts import AccountContext, app_config

ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT / "debug"

CAS_URL = "https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement"


def dump_debug(page, tag: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
    (DEBUG_DIR / f"{tag}.html").write_text(page.content())
    print(f"  [debug] saved debug/{tag}.png and debug/{tag}.html")


def _coerce_date(value, fallback_today: bool = False) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if fallback_today and value.strip().lower() == "today":
            return date.today()
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    raise ValueError(f"Cannot interpret date: {value!r}")


def _form_date(d: date) -> str:
    # CAMS form uses DD-MMM-YYYY (e.g. 04-May-2026)
    return d.strftime("%d-%b-%Y")


# ---------------------------------------------------------------------------
# Async version — used by submit_via_playwright on Windows
# ---------------------------------------------------------------------------

async def _async_submit_cas_request(page, ctx: AccountContext, *, dry_run: bool) -> None:
    email = ctx.email
    pdf_password = ctx.pdf_password
    from_date = _coerce_date(ctx.from_date)
    to_date = date.today()

    print(f"-> opening {CAS_URL}")
    await page.goto(CAS_URL, wait_until="networkidle")

    # Dismiss disclaimer modal
    try:
        await page.wait_for_selector("text=Disclaimer", timeout=5000)
        print("-> Disclaimer modal detected; accepting")
        await page.locator(
            'mat-radio-button:has(input[value="ACCEPT"]) .mat-radio-container'
        ).click()
        await page.wait_for_timeout(300)
        await page.get_by_role("button", name="PROCEED").click()
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # Close any chat widgets
    for sel in ["button[aria-label='Close']", ".close-chat", "#chat-close"]:
        try:
            await page.locator(sel).first.click(timeout=1500)
            break
        except Exception:
            continue

    # Click CAS tile if visible
    tile = page.get_by_text("CAS - CAMS+ KFintech", exact=False).first
    if await tile.is_visible():
        try:
            await tile.click(timeout=3000)
        except Exception:
            pass

    async def click_radio(value: str, description: str) -> None:
        print(f"-> selecting {description} (value={value})")
        await page.locator(
            f'mat-radio-button:has(input[value="{value}"])'
        ).click(force=True)

    await click_radio("detailed", "Detailed statement type")
    await page.wait_for_timeout(800)
    await click_radio("SP", "Specific Period")
    await page.wait_for_timeout(800)

    async def fill_date(input_id: str, value: str, description: str) -> None:
        print(f"-> filling {description}: {value}")
        await page.locator(f"input#{input_id}").evaluate(
            """(el, val) => {
                el.removeAttribute('readonly');
                el.removeAttribute('disabled');
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur',  { bubbles: true }));
            }""",
            value,
        )

    await fill_date("fromDate_new", _form_date(from_date), "From date")
    await fill_date("to-date-input", _form_date(to_date), "To date")
    await click_radio("N", "Without zero balance folios")

    print(f"-> filling email: {email}")
    await page.locator('input[formcontrolname="email_id"]').fill(email)
    print("-> filling password (twice)")
    await page.locator("#password").fill(pdf_password)
    await page.locator("#confirmPassword").fill(pdf_password)

    if dry_run:
        print("-> DRY RUN: form filled but Submit will NOT be clicked.")
        await page.wait_for_timeout(2000)
        return

    print("-> clicking Submit and waiting for /api/v1/camsonline response")
    async with page.expect_response(
        lambda r: "api/v1/camsonline" in r.url and r.request.method == "POST",
        timeout=90_000,
    ) as resp_info:
        await page.get_by_role("button", name="Submit").click(force=True)

    response = await resp_info.value
    print(f"   API status: {response.status}")
    try:
        body = await response.text() or ""
    except Exception:
        body = ""

    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "after_submit_response.txt").write_text(
        f"HTTP {response.status}\n\n{body}"
    )

    if response.status >= 400:
        raise RuntimeError(
            f"CAMS rejected the request (HTTP {response.status}). "
            "Usually a rate limit or transient error — wait and retry."
        )

    body_lower = body.lower()
    body_success_signals = (
        '"success":true', '"status":"success"', '"status":"ok"',
        "request has been", "email has been sent", "successfully submitted",
        "request received", "request submitted",
    )
    if not any(s in body_lower for s in body_success_signals):
        raise RuntimeError(
            "Couldn't confirm the CAS submission. "
            "Try clicking Refresh CAS again in a minute.\n\n"
            "If this keeps happening, submit the form yourself here:\n"
            f"  {CAS_URL}\n\n"
            "Use the same CAS PDF password set in this app's settings."
        )

    print("-> submit confirmed via API body")


# ---------------------------------------------------------------------------
# Sync version (kept for reference / non-Windows use)
# ---------------------------------------------------------------------------

def submit_cas_request(page: Page, ctx: AccountContext, *, dry_run: bool) -> None:
    email = ctx.email
    pdf_password = ctx.pdf_password
    from_date = _coerce_date(ctx.from_date)
    to_date = date.today()

    print(f"-> opening {CAS_URL}")
    page.goto(CAS_URL, wait_until="networkidle")
    dump_debug(page, "01_loaded")

    # Dismiss disclaimer
    try:
        page.wait_for_selector("text=Disclaimer", timeout=5000)
        print("-> Disclaimer modal detected; accepting")
        page.locator(
            'mat-radio-button:has(input[value="ACCEPT"]) .mat-radio-container'
        ).click()
        page.wait_for_timeout(300)
        page.get_by_role("button", name="PROCEED").click()
        try:
            page.wait_for_selector("text=Disclaimer", state="hidden", timeout=5000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
    except PWTimeout:
        pass

    for sel in ["button[aria-label='Close']", ".close-chat", "#chat-close"]:
        try:
            page.locator(sel).first.click(timeout=1500)
            break
        except Exception:
            continue

    tile = page.get_by_text("CAS - CAMS+ KFintech", exact=False).first
    if tile.is_visible():
        try:
            tile.click(timeout=3000)
        except PWTimeout:
            pass

    def click_radio(value: str, description: str) -> None:
        print(f"-> selecting {description} (value={value})")
        page.locator(f'mat-radio-button:has(input[value="{value}"])').click(force=True)

    click_radio("detailed", "Detailed statement type")
    page.wait_for_timeout(800)
    click_radio("SP", "Specific Period")
    page.wait_for_timeout(800)
    dump_debug(page, "after_sp")

    def fill_date(input_id: str, value: str, description: str) -> None:
        print(f"-> filling {description}: {value}")
        page.locator(f"input#{input_id}").evaluate(
            """(el, val) => {
                el.removeAttribute('readonly');
                el.removeAttribute('disabled');
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur',  { bubbles: true }));
            }""",
            value,
        )

    fill_date("fromDate_new", _form_date(from_date), "From date")
    fill_date("to-date-input", _form_date(to_date), "To date")
    click_radio("N", "Without zero balance folios")

    print(f"-> filling email: {email}")
    page.locator('input[formcontrolname="email_id"]').fill(email)
    print("-> filling password (twice)")
    page.locator("#password").fill(pdf_password)
    page.locator("#confirmPassword").fill(pdf_password)

    dump_debug(page, "before_submit")

    if dry_run:
        print("-> DRY RUN: form is filled but Submit will NOT be clicked.")
        page.wait_for_timeout(2000)
        return

    print("-> clicking Submit and waiting for /api/v1/camsonline response")
    with page.expect_response(
        lambda r: "api/v1/camsonline" in r.url and r.request.method == "POST",
        timeout=90_000,
    ) as resp_info:
        page.get_by_role("button", name="Submit").click(force=True)

    response = resp_info.value
    print(f"   API status: {response.status}")
    try:
        body = response.text() or ""
    except Exception:
        body = ""
    page.wait_for_timeout(2500)
    dump_debug(page, "after_submit")
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "after_submit_response.txt").write_text(
        f"HTTP {response.status}\n\n{body}"
    )

    if response.status >= 400:
        raise RuntimeError(
            f"CAMS rejected the request (HTTP {response.status}). "
            "Usually a rate limit or transient error — wait and retry."
        )

    body_lower = body.lower()
    body_success_signals = (
        '"success":true', '"status":"success"', '"status":"ok"',
        "request has been", "email has been sent", "successfully submitted",
        "request received", "request submitted",
    )
    has_body_success = any(s in body_lower for s in body_success_signals)

    if not has_body_success:
        raise RuntimeError(
            "Couldn't submit the CAS request to CAMS this time. "
            "Try clicking **Refresh CAS** again in a minute.\n\n"
            f"Submit manually at: {CAS_URL}"
        )
    print("-> submit confirmed via API body")


# ---------------------------------------------------------------------------
# Entry point called by the Streamlit UI
# Uses async_playwright + ProactorEventLoop to avoid Windows asyncio conflicts
# ---------------------------------------------------------------------------

def submit_via_playwright(
    ctx: AccountContext,
    *,
    dry_run: bool = False,
    headless: bool | None = None,
) -> dict:
    """Run the full CAMS form submission.
    Uses async Playwright on a ProactorEventLoop in a background thread
    to avoid asyncio conflicts with Streamlit on Windows.
    """
    result = {}

    def _run() -> None:
        import asyncio
        from playwright.async_api import async_playwright

        async def _async_run() -> None:
            pw_cfg = app_config().get("playwright") or {}
            is_headless = (
                pw_cfg.get("headless", True) if headless is None else headless
            )
            launch_args = dict(
                headless=is_headless,
                slow_mo=pw_cfg.get("slow_mo_ms", 0) if not is_headless else 0,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(channel="chrome", **launch_args)
                except Exception:
                    browser = await p.chromium.launch(**launch_args)

                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="en-IN",
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                try:
                    await _async_submit_cas_request(page, ctx, dry_run=dry_run)
                    result["value"] = {"ok": True, "submitted": not dry_run}
                except Exception as e:
                    result["value"] = {"ok": False, "error": str(e)}
                finally:
                    await browser.close()

        loop = asyncio.ProactorEventLoop()
        try:
            loop.run_until_complete(_async_run())
        except Exception as e:
            result["value"] = {"ok": False, "error": f"Async crash: {e}"}
        finally:
            loop.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join()

    print(f"DEBUG thread result: {result}")
    out = result.get("value")
    if out is None:
        return {"ok": False, "error": "Playwright thread returned no result"}
    return out