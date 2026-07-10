"""
One-time migration: fold the 23 named customers from the Actimize spec
workbook's Customer Accounts sheet into agents/agent_profiles.xlsx's
Combined_Data sheet as ordinary rows, indistinguishable in schema from
the ~204 already there.

Identity fields (name, address, phone_number, email, customer_id,
customer_since, account_type, account_number) come from the workbook
as-is. Behavioral fields (bank, merchant_patterns, merchant_frequency,
transaction_scaler, employer, employment_status, income_level) are
sampled from an existing same-type row so these customers actually
generate transaction activity instead of sitting inert.

alert_reason is intentionally never read from the workbook — it is an
answer-key column and must not reach Combined_Data or any generated
output.

Run once: ./aml-env/bin/python3 scripts/migrate_actimize_customers.py
"""
import random

import openpyxl
import pandas as pd

WORKBOOK_PATH = "Actimize Rules_Alert Clearing Ex.xlsx"
PROFILES_PATH = "agents/agent_profiles.xlsx"
SEED = 20260708

TYPE_BY_ACCOUNT_TYPE = {"business": "company", "personal": "person"}
BEHAVIORAL_FIELDS = [
    "bank", "sends", "receives", "merchant_patterns", "merchant_frequency",
    "transaction_scaler", "employment_status", "employer", "income_level",
]


def load_workbook_customers():
    wb = openpyxl.load_workbook(WORKBOOK_PATH, read_only=True)
    ws = wb["Customer Accounts"]
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    header = rows[0]
    customers = [dict(zip(header, row)) for row in rows[1:]]
    for c in customers:
        c.pop("alert_reason", None)  # answer-key column, never migrated
    return customers


def next_entity_id(df, prefix, type_value):
    existing = df[df["type"] == type_value]["entity_id"].str.extract(r"(\d+)").astype(int)
    start = int(existing.max().iloc[0]) + 1 if not existing.empty else 1001
    n = start
    while True:
        yield f"{prefix}{n:04d}"
        n += 1


def main():
    rng = random.Random(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if (df["customer_id"].astype("string").str.startswith("CUST-1001", na=False)).any():
        raise SystemExit("Actimize customer_ids already present — migration already run, aborting.")

    customers = load_workbook_customers()

    person_id_gen = next_entity_id(df, "PERS", "person")
    company_id_gen = next_entity_id(df, "COMP", "company")

    new_rows = []
    for cust in customers:
        entity_type = TYPE_BY_ACCOUNT_TYPE[cust["account_type"]]
        entity_id = next(company_id_gen) if entity_type == "company" else next(person_id_gen)

        template = df[df["type"] == entity_type].sample(1, random_state=rng.randint(0, 10**6)).iloc[0]

        row = {col: pd.NA for col in df.columns}
        for field in BEHAVIORAL_FIELDS:
            row[field] = template[field]

        row.update({
            "entity_id": entity_id,
            "type": entity_type,
            "name": cust["name"],
            "address": cust["address"],
            "phone_number": cust["phone_number"],
            "email": cust["email"],
            "account_number": cust["account_number"],
            "customer_id": cust["customer_id"],
            "customer_since": cust["customer_since"],
            "account_type": cust["account_type"],
        })
        new_rows.append(row)

    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    xl["Combined_Data"] = df

    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Added {len(new_rows)} customers ({sum(1 for r in new_rows if r['type']=='company')} company, "
          f"{sum(1 for r in new_rows if r['type']=='person')} person). Combined_Data now {len(df)} rows.")


if __name__ == "__main__":
    main()
