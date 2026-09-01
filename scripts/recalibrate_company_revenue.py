"""
Formulaic recalibration of average_expense/revenue_monthly_target for all
34 companies, replacing scripts/recalibrate_transaction_volume.py's role
for these two fields specifically (that script's merchant_frequency work
for persons/merchants is untouched).

Root problem this fixes: company revenue previously drew one amount from a
flat average_expense, then found which payment types' ranges contained it -
that architecture could never work for wire/check-heavy archetypes (a $50
card range and a $10,000+ wire range can't share one +/-30% band), and
consumer-facing archetypes' average_expense values ($300-$1,400+) were
~10x too high relative to real per-ticket sizes (restaurant cards average
$29-34, general retail ~$81 - Fed/consumer-payments research, 2026-07-14).

generator/transactions.py's generate_company_revenue_transactions now
picks payment_type FIRST (weighted from revenue_mix_weights), then draws
the amount from that specific type's own range - average_expense is no
longer the amount-drawing band, it's the archetype/size-tier's BLENDED
ticket (weighted average of every active rail's range midpoint), used by
generate_restocking_transactions and generate_daily_card_settlement to
reconstruct monthly revenue in dollars (revenue_monthly_target x
average_expense).

Each company's target monthly revenue = base_annual_revenue (per
archetype, researched/reasoned - see ARCHETYPE_BASE_ANNUAL_REVENUE below)
x its own transaction_scaler / 12. revenue_monthly_target is then derived
as target_monthly_revenue / blended_ticket, so total dollar volume stays
sane even though per-transaction size and transaction count both change
substantially from today's values.

Supersedes the one-off scripts/fix_arrakis_calibration.py patch - Arrakis
gets recalculated the same formulaic way as every other company now that
the systematic fix exists, rather than keeping a hand-tuned exception.

Run once: ./aml-env/bin/python3 scripts/recalibrate_company_revenue.py
"""
import pandas as pd
import yaml

PROFILES_PATH = "agents/agent_profiles.xlsx"
RANGES_PATH = "config/payment_ranges.yaml"

# Base annual revenue at transaction_scaler=1, researched/reasoned per
# archetype (see /Users/ufhm/.claude/plans/ethereal-foraging-harbor.md for
# the research this was grounded in). DRAFT, Rio's to retune.
ARCHETYPE_BASE_ANNUAL_REVENUE = {
    "RESTAURANT": 600_000,
    "RETAIL": 700_000,
    "GAS_STATION": 1_300_000,
    "MANUFACTURING": 2_000_000,
    "PROFESSIONAL_SERVICES": 700_000,
    "REAL_ESTATE": 1_200_000,
    "DELIVERY_LOGISTICS": 600_000,
    "FINANCIAL_SERVICES": 1_500_000,
    "IMPORT_EXPORT": 4_000_000,
    "WHOLESALE_DISTRIBUTION": 3_000_000,
    "FACILITY_SERVICES": 700_000,
}


def size_tier(scaler: float) -> str:
    """Matches generator/transactions.py's get_company_size_tier exactly -
    kept in sync by hand since this is a one-off script, not shared code."""
    if scaler <= 0.75:
        return "small"
    if scaler <= 1.5:
        return "mid"
    return "large"


# Matches generator/transactions.py's DAILY_SETTLEMENT_ARCHETYPES exactly
# - kept in sync by hand since this is a one-off script, not shared code.
DAILY_SETTLEMENT_ARCHETYPES = {"RESTAURANT", "RETAIL", "GAS_STATION"}


def blended_ticket(segment: str, tier: str, ranges: dict) -> float:
    """Weighted average of every active payment type's range midpoint for
    this archetype - wire/check use the size-tiered range for this
    company's tier EXCEPT for DAILY_SETTLEMENT_ARCHETYPES, where check is
    a rare incidental edge case (an occasional catering invoice) sized off
    the archetype's own modest range, not the business-size-driven B2B
    scale (matches generate_company_revenue_transactions's own logic -
    see its 2026-07-14 comment)."""
    weights = ranges["revenue_mix_weights"][segment]
    archetype_ranges = ranges["payment_type_ranges"]["companies"].get(segment, {})
    size_tiered = ranges["size_tiered_payment_ranges"]

    total = 0.0
    for pt, w in weights.items():
        if pt in ("wire", "check") and segment not in DAILY_SETTLEMENT_ARCHETYPES:
            lo, hi = size_tiered[pt][tier]
        else:
            lo, hi = archetype_ranges[pt]
        total += w * ((lo + hi) / 2)
    return total


def main():
    with open(RANGES_PATH, "r") as f:
        ranges = yaml.safe_load(f)
    company_segments = ranges["company_segments"]
    eid_to_segment = {eid: seg for seg, ids in company_segments.items() for eid in ids}

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    updated = 0
    for idx, row in df[df["type"] == "company"].iterrows():
        segment = eid_to_segment.get(row["entity_id"])
        if segment is None:
            continue
        scaler = float(row.get("transaction_scaler") or 1)
        tier = size_tier(scaler)
        ticket = blended_ticket(segment, tier, ranges)
        base_revenue = ARCHETYPE_BASE_ANNUAL_REVENUE[segment]
        target_monthly_revenue = base_revenue * scaler / 12
        new_count = target_monthly_revenue / ticket

        df.loc[idx, "average_expense"] = round(ticket, 2)
        df.loc[idx, "revenue_monthly_target"] = round(new_count, 2)
        updated += 1

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Recalibrated average_expense/revenue_monthly_target for {updated} companies.")


if __name__ == "__main__":
    main()
