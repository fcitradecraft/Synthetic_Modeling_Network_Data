"""
One-time data fix: 22 account_number values in Combined_Data are each shared
by two different entities (21 company/merchant pairs, 1 merchant/person
pair) - found while verifying main.py's in_package column, which reads
purely off account_number and so misclassified the merchant side of every
collision as "in the package." Pre-existing issue, not introduced by any
of this session's generator changes.

Fix: for each duplicate group, keep the number on the person/company row
(the identity that matters for sampling/ownership/in_package) and assign a
fresh, globally-unique 9-digit number to the other row(s) - almost always
a merchant.

Run once: ./aml-env/bin/python3 scripts/fix_account_number_duplicates.py
"""
import random

import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"
SEED = 20260710  # fixed so re-running (after the guard is bypassed) reproduces the same values

# Priority for which row keeps a contested account_number - lower wins.
TYPE_PRIORITY = {"person": 0, "company": 1, "merchant": 2, "BEnt": 3, "bank": 4}


def new_unique_account_number(rng: random.Random, existing: set) -> int:
    while True:
        candidate = rng.randint(100_000_000, 999_999_999)
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def main():
    rng = random.Random(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    has_acct = df["account_number"].notna()
    dupe_mask = has_acct & df["account_number"].duplicated(keep=False)
    dupe_numbers = df.loc[dupe_mask, "account_number"].unique()

    if len(dupe_numbers) == 0:
        raise SystemExit("No duplicate account_number values found - already fixed, aborting.")

    existing_numbers = set(df.loc[has_acct, "account_number"].astype(int))
    reassigned = 0

    for number in dupe_numbers:
        group = df[df["account_number"] == number]
        ranked = group.assign(_priority=group["type"].map(TYPE_PRIORITY).fillna(99))
        keeper_idx = ranked.sort_values("_priority").index[0]

        for idx in group.index:
            if idx == keeper_idx:
                continue
            new_number = new_unique_account_number(rng, existing_numbers)
            df.at[idx, "account_number"] = new_number
            reassigned += 1

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Resolved {len(dupe_numbers)} duplicate account_number values by reassigning "
          f"{reassigned} rows to fresh, unique numbers.")


if __name__ == "__main__":
    main()
