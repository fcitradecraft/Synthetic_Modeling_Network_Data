"""
One-time schema extension: add customer_id, customer_since, account_type
to agents/agent_profiles.xlsx's Combined_Data sheet, matching the field
conventions used in the Actimize spec workbook's Customer Accounts sheet.

customer_id numbering starts at CUST-200001 to leave CUST-100101-100123
free for the 23 named Actimize customers folded in by
migrate_actimize_customers.py (step 2).

Run once: ./aml-env/bin/python3 scripts/extend_customer_schema.py
"""
import random
from datetime import datetime, timedelta

import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"
CUSTOMER_TYPES = {"person": "personal", "company": "business", "merchant": "business"}
CUSTOMER_ID_START = 200001
SEED = 20260708  # fixed so re-running reproduces the same static values


def random_customer_since(rng, today):
    days_back = rng.randint(365, 365 * 8)
    return today - timedelta(days=days_back)


def main():
    rng = random.Random(SEED)
    today = datetime(2026, 7, 8)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if "customer_id" in df.columns:
        raise SystemExit("customer_id already present — schema already extended, aborting.")

    df["customer_id"] = pd.NA
    df["customer_since"] = pd.NaT
    df["account_type"] = pd.NA

    next_id = CUSTOMER_ID_START
    for idx, row in df.iterrows():
        account_type = CUSTOMER_TYPES.get(row["type"])
        if account_type is None:
            continue
        df.at[idx, "customer_id"] = f"CUST-{next_id}"
        df.at[idx, "customer_since"] = random_customer_since(rng, today)
        df.at[idx, "account_type"] = account_type
        next_id += 1

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    populated = df["customer_id"].notna().sum()
    print(f"Populated customer_id/customer_since/account_type on {populated} of {len(df)} rows.")
    print(f"customer_id range: CUST-{CUSTOMER_ID_START} .. CUST-{next_id - 1}")


if __name__ == "__main__":
    main()
