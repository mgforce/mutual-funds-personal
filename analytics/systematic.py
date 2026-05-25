"""
Detect active SIPs and STPs from parsed-CAS transactions.

CAS has no explicit "this mandate is active" flag — we infer it from the
transaction log: a recurring run of installments whose latest entry falls
inside ACTIVE_WINDOW_DAYS is treated as live. Frequency and the
next-expected date come from the median gap between recent installments.

casparser classifies tx type from the description: anything with "switch"
becomes SWITCH_IN/SWITCH_OUT, anything with "systematic" becomes
PURCHASE_SIP, and a units<0 line with neither becomes REDEMPTION. Most AMCs
label STP legs as "STP IN"/"STP OUT" or "Systematic Transfer In/Out", so
their STPs land as PURCHASE_SIP (in-leg) + REDEMPTION (out-leg) — only the
handful that use the word "switch" come through as SWITCH_IN/SWITCH_OUT.
We pair both flavours by (folio, date, |amount|), require recurrence to
rule out coincidental redeem+buy events, and then exclude those PURCHASE_SIP
transactions consumed as STP in-legs from the SIP list.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Iterable

from analytics.portfolio import SchemeRow

ACTIVE_WINDOW_DAYS = 45
DAYS_PER_MONTH = 30.4375
RECENT_INSTALLMENTS_FOR_CADENCE = 12
OUT_LEG_TYPES = {"SWITCH_OUT", "REDEMPTION"}
IN_LEG_TYPES = {"SWITCH_IN", "PURCHASE_SIP", "PURCHASE"}


@dataclass
class SipMandate:
    folio: str
    holder_name: str
    amc: str
    scheme: str
    isin: str
    amount: float          # most recent installment — current mandate amount
    frequency_days: int    # median gap between recent installments
    frequency_label: str
    monthly_amount: float  # amount normalised to a 30.44-day month
    installments: int
    first_date: date
    last_date: date
    next_expected: date
    total_invested: float  # sum of every detected installment, all-time


@dataclass
class StpMandate:
    source_folio: str
    target_folio: str
    holder_name: str
    amc: str
    source_scheme: str
    source_isin: str
    target_scheme: str
    target_isin: str
    amount: float
    frequency_days: int
    frequency_label: str
    monthly_amount: float
    installments: int
    first_date: date
    last_date: date
    next_expected: date
    total_amount: float


def _frequency_label(days: int) -> str:
    if days <= 2:
        return "Daily"
    if 5 <= days <= 9:
        return "Weekly"
    if 12 <= days <= 17:
        return "Fortnightly"
    if 25 <= days <= 35:
        return "Monthly"
    if 80 <= days <= 100:
        return "Quarterly"
    if 170 <= days <= 195:
        return "Half-yearly"
    if 350 <= days <= 380:
        return "Yearly"
    return f"Every ~{days} days"


def _median_gap_days(dates: list[date]) -> int | None:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    if not gaps:
        return None
    return int(round(median(gaps)))


def _monthly_amount_from_history(amount: float, recent_dates: list[date]) -> float:
    """Average elapsed-time between recent installments, scaled to a 30.44-day
    month. Mean (not median) so daily-on-business-days schedules naturally
    account for weekend gaps — median would say "1 day" and over-estimate by
    ~40%, mean lands at ~1.4 days which matches calendar reality."""
    if len(recent_dates) < 2:
        return 0.0
    span = (recent_dates[-1] - recent_dates[0]).days
    if span <= 0:
        return 0.0
    avg_gap = span / (len(recent_dates) - 1)
    return amount * DAYS_PER_MONTH / avg_gap


def _cluster_by_amount(txs: list[dict]) -> list[list[dict]]:
    """Group txs by integer-rupee amount. Concurrent SIPs in the same fund
    (e.g. ₹200/day + ₹3000/week into Invesco Small Cap) have distinct integer
    amounts and form separate clusters; a single SIP's installments share an
    amount and stay together."""
    clusters: dict[int, list[dict]] = {}
    for t in txs:
        bucket = round(float(t["amount"]))
        clusters.setdefault(bucket, []).append(t)
    return list(clusters.values())


def detect_sips(
    rows: Iterable[SchemeRow],
    today: date | None = None,
    stp_in_tx_ids: set[int] | None = None,
) -> list[SipMandate]:
    """One mandate per (folio, scheme, amount) whose latest installment is
    inside the active window. Schemes with fewer than two SIP installments
    in a given amount bucket are skipped — we can't infer a cadence from a
    single point. PURCHASE_SIP transactions consumed as STP in-legs (passed
    in stp_in_tx_ids) are excluded so STPs don't double-count as SIPs."""
    today = today or date.today()
    stp_in_tx_ids = stp_in_tx_ids or set()
    out: list[SipMandate] = []
    for r in rows:
        for f in r.folio_details:
            sip_tx_all = [
                t for t in f.transactions
                if t.get("type") == "PURCHASE_SIP"
                and t.get("amount") and t.get("date")
                and id(t) not in stp_in_tx_ids
            ]
            if not sip_tx_all:
                continue

            for cluster in _cluster_by_amount(sip_tx_all):
                sip_tx = sorted(cluster, key=lambda t: t["date"])
                if len(sip_tx) < 2:
                    continue

                last_date = sip_tx[-1]["date"]
                if (today - last_date).days > ACTIVE_WINDOW_DAYS:
                    continue

                recent = sip_tx[-RECENT_INSTALLMENTS_FOR_CADENCE:]
                freq_days = _median_gap_days([t["date"] for t in recent])
                if freq_days is None or freq_days <= 0:
                    continue

                amount = float(sip_tx[-1]["amount"])
                total = sum(float(t["amount"]) for t in sip_tx)

                out.append(SipMandate(
                    folio=f.folio,
                    holder_name=f.holder_name,
                    amc=r.amc,
                    scheme=r.scheme,
                    isin=r.isin,
                    amount=amount,
                    frequency_days=freq_days,
                    frequency_label=_frequency_label(freq_days),
                    monthly_amount=_monthly_amount_from_history(amount, [t["date"] for t in recent]),
                    installments=len(sip_tx),
                    first_date=sip_tx[0]["date"],
                    last_date=last_date,
                    next_expected=last_date + timedelta(days=freq_days),
                    total_invested=total,
                ))
    out.sort(key=lambda s: s.monthly_amount, reverse=True)
    return out


def _build_leg(folio: str, holder: str, scheme: str, isin: str, amc: str, t: dict) -> dict:
    return {
        "folio": folio,
        "holder_name": holder,
        "date": t["date"],
        "amount": abs(float(t["amount"])),
        "scheme": scheme,
        "isin": isin,
        "amc": amc,
        "tx_id": id(t),
    }


def _pair_stp_legs(rows: Iterable[SchemeRow]) -> dict[tuple[str, str], list[dict]]:
    """Pair STP legs by (date, |amount|) only — neither folio nor AMC are
    required to match. Out-legs come from SWITCH_OUT and REDEMPTION; in-legs
    from SWITCH_IN, PURCHASE_SIP, and PURCHASE — broad on type because
    casparser labels transactions by the description text, not intent, and
    different AMCs phrase STP legs differently ("STP IN", "Systematic
    Transfer In", "Switch In", etc.). Some AMCs (Canara, Quant) issue a
    separate folio per scheme; others print the AMC name slightly differently
    on each scheme's CAS page — both break folio- or AMC-strict matching.
    The recurrence requirement (≥2 same-amount pairs between the same source
    and target ISINs, applied later) is what rules out coincidental
    redeem+buy events. Returns a (source_isin, target_isin) → pair list."""
    outs: list[dict] = []
    ins: list[dict] = []
    for r in rows:
        for f in r.folio_details:
            for t in f.transactions:
                ttype = t.get("type")
                if not t.get("amount") or not t.get("date") or not r.isin:
                    continue
                if ttype in OUT_LEG_TYPES:
                    outs.append(_build_leg(f.folio, f.holder_name, r.scheme, r.isin, r.amc, t))
                elif ttype in IN_LEG_TYPES:
                    ins.append(_build_leg(f.folio, f.holder_name, r.scheme, r.isin, r.amc, t))

    pairs: dict[tuple[str, str], list[dict]] = {}
    used_in_idx: set[int] = set()
    for o in outs:
        for idx, i in enumerate(ins):
            if idx in used_in_idx:
                continue
            if i["date"] != o["date"]:
                continue
            if i["isin"] == o["isin"]:
                continue  # would be the same scheme — not a transfer
            if abs(i["amount"] - o["amount"]) > 1.0:
                continue
            key = (o["isin"], i["isin"])
            pairs.setdefault(key, []).append({
                "date": o["date"],
                "amount": o["amount"],
                "source_scheme": o["scheme"],
                "target_scheme": i["scheme"],
                "source_folio": o["folio"],
                "target_folio": i["folio"],
                "amc": o["amc"],
                "holder_name": o["holder_name"],
                "in_tx_id": i["tx_id"],
            })
            used_in_idx.add(idx)
            break
    return pairs


def detect_stps(
    rows: Iterable[SchemeRow], today: date | None = None,
) -> tuple[list[StpMandate], set[int]]:
    """Pair out/in legs and treat any (source, target) ISIN pair with ≥2
    matched legs and a recent last leg as an active STP. Returns the mandates
    plus the set of in-leg tx ids actually consumed by a recurring pattern —
    the SIP detector subtracts those so STP in-legs that casparser labels
    PURCHASE_SIP (most non-PPFAS AMCs) don't double-count as SIPs."""
    today = today or date.today()
    grouped = _pair_stp_legs(rows)

    out: list[StpMandate] = []
    consumed_in_ids: set[int] = set()
    for (src_isin, tgt_isin), all_legs in grouped.items():
        for legs in _cluster_by_amount(all_legs):
            legs.sort(key=lambda p: p["date"])
            if len(legs) < 2:
                continue

            last_date = legs[-1]["date"]
            if (today - last_date).days > ACTIVE_WINDOW_DAYS:
                continue

            recent = legs[-RECENT_INSTALLMENTS_FOR_CADENCE:]
            freq_days = _median_gap_days([p["date"] for p in recent])
            if freq_days is None or freq_days <= 0:
                continue

            amount = float(legs[-1]["amount"])
            total = sum(float(p["amount"]) for p in legs)

            out.append(StpMandate(
                source_folio=legs[-1]["source_folio"],
                target_folio=legs[-1]["target_folio"],
                holder_name=legs[-1]["holder_name"],
                amc=legs[-1]["amc"],
                source_scheme=legs[-1]["source_scheme"],
                source_isin=src_isin,
                target_scheme=legs[-1]["target_scheme"],
                target_isin=tgt_isin,
                amount=amount,
                frequency_days=freq_days,
                frequency_label=_frequency_label(freq_days),
                monthly_amount=_monthly_amount_from_history(amount, [p["date"] for p in recent]),
                installments=len(legs),
                first_date=legs[0]["date"],
                last_date=last_date,
                next_expected=last_date + timedelta(days=freq_days),
                total_amount=total,
            ))
            for leg in legs:
                consumed_in_ids.add(leg["in_tx_id"])
    out.sort(key=lambda s: s.monthly_amount, reverse=True)
    return out, consumed_in_ids
