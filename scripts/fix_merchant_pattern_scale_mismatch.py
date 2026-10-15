"""
One-time data fix: several small, consumer-facing companies had high-ticket
B2B merchant NAICS categories in their routine merchant_patterns rotation -
categories priced for enterprise-scale procurement (IT services $5,000-
$25,000, admin/staffing outsourcing $15,000-$30,000, general contracting up
to $30,000 within a mixed-price "238" bucket, a $3,200 bulk food-services
merchant visited 4-5x/week) showing up as routine, recurring purchases for
a bar or gas station. Traced this 2026-09-01 while diagnosing why several
companies' internal ledger balance (generator/ledger.py, Phase 2) went
deeply negative - e.g. Arrakis Cantina & Bar generated $604,765 of "check"
debits in one quarter (110 transactions, ~$5,498 avg) from patronizing a
$20,000-average admin-services merchant (NAICS 561) 13.79 times a month.

Rio's call, 2026-09-01: exclude these categories from small archetypes'
merchant_patterns entirely, rather than just capping price (the existing
max_merchant_average_expense ceiling already caps individual transaction
size but doesn't address the unrealistic FREQUENCY of "routinely" visiting
an enterprise-scale vendor). NAICS 524 (insurance, $300-$4,500) is
deliberately NOT touched - a business insurance premium is a normal
recurring expense at any company size, not a scale mismatch, even though
its ratio to these companies' own average_expense is similarly high.

Threshold used to find these: for every (company, merchant_patterns entry)
pair, ratio = (mean average_expense of merchants matching that NAICS
prefix) / (company's own average_expense). Every pair at ratio >= 15
(excluding NAICS 524) is removed below; everything under that threshold
left alone as plausible occasional-larger-vendor variation (e.g. a
restaurant's occasional appliance-repair bill at ~6x its own ticket size).

Run once: ./aml-env/bin/python3 scripts/fix_merchant_pattern_scale_mismatch.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# entity_id -> set of merchant_patterns NAICS prefixes to remove (along with
# their paired merchant_frequency value, keeping both lists aligned by index).
REMOVALS = {
    "COMP1012": {"518", "445"},  # Dinoco
    "COMP1011": {"518", "445"},  # Great Gas
    "COMP1027": {"561", "238", "532"},  # Arrakis Cantina & Bar
    "COMP1032": {"561", "238", "532"},  # Kwisatz Kitchen Inc
    "COMP1022": {"518", "445"},  # Mackeys
    "COMP1010": {"518", "445"},  # Sahara
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    updated = 0
    for entity_id, drop_patterns in REMOVALS.items():
        mask = df["entity_id"] == entity_id
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found - data may have changed, aborting.")
        row = df.loc[mask].iloc[0]
        patterns = [p.strip() for p in row["merchant_patterns"].split(",") if p.strip()]
        freqs = [f.strip() for f in row["merchant_frequency"].split(",") if f.strip()]
        if len(patterns) != len(freqs):
            raise SystemExit(f"entity_id {entity_id}: merchant_patterns/merchant_frequency length mismatch, aborting.")

        kept = [(p, f) for p, f in zip(patterns, freqs) if p not in drop_patterns]
        removed = [(p, f) for p, f in zip(patterns, freqs) if p in drop_patterns]
        if len(removed) != len(drop_patterns):
            raise SystemExit(
                f"entity_id {entity_id}: expected to remove {drop_patterns}, "
                f"only found {[p for p, _ in removed]} - data may have changed, aborting."
            )

        df.loc[mask, "merchant_patterns"] = ", ".join(p for p, _ in kept)
        df.loc[mask, "merchant_frequency"] = ", ".join(f for _, f in kept)
        updated += 1

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Removed scale-mismatched merchant_patterns entries for {updated} companies.")


if __name__ == "__main__":
    main()
