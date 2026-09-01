"""
One-time data fix: Arrakis Cantina & Bar (COMP1027) had average_expense
$5,818.62 and transaction_scaler 4.0 - both far outside its RESTAURANT
archetype's norms (peers run $311-$1,380 average_expense, 0.5-1.5 scaler).
$5,818 exceeds every card/cash/ach/check rail's ceiling for RESTAURANT
(config/payment_ranges.yaml's payment_type_ranges.companies.RESTAURANT tops
out ~$3,000), so pick_payment_type_for_amount's fallback logic picked ACH
for every single revenue transaction - not randomness, a deterministic
consequence of the bad input. Brought in line with its closest peer,
Bene Gesserit Bistro (average_expense=815.42, transaction_scaler=1.5) - a
similarly-sized single-location bistro/bar. DRAFT values, Rio's to retune.

Run once: ./aml-env/bin/python3 scripts/fix_arrakis_calibration.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"
ENTITY_ID = "COMP1027"
NEW_AVERAGE_EXPENSE = 850.00
NEW_TRANSACTION_SCALER = 1.25


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    mask = (df["type"] == "company") & (df["entity_id"] == ENTITY_ID)
    if not mask.any():
        raise SystemExit(f"entity_id {ENTITY_ID} not found among company rows - aborting.")

    old_avg = df.loc[mask, "average_expense"].iloc[0]
    old_scaler = df.loc[mask, "transaction_scaler"].iloc[0]

    df.loc[mask, "average_expense"] = NEW_AVERAGE_EXPENSE
    df.loc[mask, "transaction_scaler"] = NEW_TRANSACTION_SCALER

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(
        f"{ENTITY_ID}: average_expense {old_avg:.2f} -> {NEW_AVERAGE_EXPENSE:.2f}, "
        f"transaction_scaler {old_scaler} -> {NEW_TRANSACTION_SCALER}"
    )


if __name__ == "__main__":
    main()
