"""
Re-homes ~75% of the 171 existing merchant rows to the new "Widgets &
Merchants" bank (added by scripts/add_new_banks.py, run this after that
one) - Rio's call, 2026-09-02, part of the "coherent virtual banking
world" work. The remaining ~25% stay at whichever of the 3 original banks
they were already at, which naturally preserves today's existing skew
(Wells Farquod 83, Bank of Pineland 77, Misfits Savings and Loan 11) for
the "stays" group rather than inventing a new distribution - least
disruptive, no reason to redesign something that wasn't broken. Mutates
only the `bank` column; `account_number` is untouched (merchant purchase
eligibility is NAICS-based only - bank plays no role in it, confirmed by
reading generator/transactions.py's merchant-matching logic).

Seeded for reproducibility of this one-time fix, same as
scripts/fix_account_number_duplicates.py's convention.

Run once, after add_new_banks.py:
    ./aml-env/bin/python3 scripts/rehome_merchants_to_new_banks.py
"""
import random

import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"
WIDGETS_AND_MERCHANTS_BANK_CODE = 325
REHOME_FRACTION = 0.75
SEED = 20260902


def main():
    random.seed(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if not (df["entity_id"] == "BANK0325").any():
        raise SystemExit("Widgets & Merchants (BANK0325) not found - run scripts/add_new_banks.py first, aborting.")

    merchant_mask = df["type"] == "merchant"
    merchant_idx = df[merchant_mask].index.tolist()
    n_rehome = round(len(merchant_idx) * REHOME_FRACTION)
    rehome_idx = set(random.sample(merchant_idx, n_rehome))

    before = df.loc[merchant_mask, "bank"].value_counts().to_dict()
    df.loc[df.index.isin(rehome_idx), "bank"] = WIDGETS_AND_MERCHANTS_BANK_CODE
    after = df.loc[merchant_mask, "bank"].value_counts().to_dict()

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Re-homed {n_rehome} of {len(merchant_idx)} merchants to Widgets & Merchants (bank 325).")
    print(f"Before: {before}")
    print(f"After:  {after}")


if __name__ == "__main__":
    main()
