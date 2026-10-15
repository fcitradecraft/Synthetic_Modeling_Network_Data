"""
One-time data fix: the six "High" income_level persons (Chad Harrison,
Lauren Kennedy, David Caladan, Vladimir H. Baron, James Turner, Sarah J.
Sietch) had NAICS 525 (Funds, Trusts, and Other Financial Vehicles -
$350-$45,000 per transaction) in their routine merchant_patterns, generating
"WIRE - Purchase via ... Bank" transactions in the $13,000-$17,000+ range as
ordinary consumer spending - implausible at any income level, since 525 is
an institutional/business category (trust and investment-vehicle services),
not personal retail spending. Same root cause, same fix approach, and same
ratio>=15-style reasoning as scripts/fix_merchant_pattern_scale_mismatch.py
(the company-side version, 2026-09-01) - traced while diagnosing the
person-side half of Phase 2's negative-balance finding: all six of these
persons were also the six worst negative-balance accounts in the roster.
Also removes NAICS 238 (mixed $250-$30,000 contractor category, same
too-coarse-a-category issue as the company-side fix) from the same six -
present only at very low frequency (0.14-0.31/month) but still thematically
a business/contractor category, not personal spending, and capable of
producing one implausible $25,000+ "purchase" a quarter on its own.

NAICS 449 (furniture/electronics, $450-$7,000) is deliberately left alone
for every person, including these six - it's widespread across the whole
roster at similarly low frequency and represents a genuinely plausible
occasional big-ticket purchase (furniture, a TV, an appliance), not a scale
mismatch.

Run once: ./aml-env/bin/python3 scripts/fix_person_merchant_pattern_scale_mismatch.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

REMOVALS = {
    "PERS1006": {"525", "238"},  # Chad Harrison
    "PERS1001": {"525", "238"},  # Lauren Kennedy
    "PERS1017": {"525", "238"},  # David Caladan
    "PERS1020": {"525", "238"},  # Vladimir H. Baron
    "PERS1003": {"525", "238"},  # James Turner
    "PERS1018": {"525", "238"},  # Sarah J. Sietch
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

    print(f"Removed scale-mismatched merchant_patterns entries for {updated} persons.")


if __name__ == "__main__":
    main()
