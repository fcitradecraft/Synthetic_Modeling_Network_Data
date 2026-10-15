"""
Adds bounded, PER-OWNER customer pools to Combined_Data - "revenue_customer"
(8 per company, 34 companies = 272 rows) and "self_employment_client" (8 per
self-employed/unemployed-high-income person, 8 such persons = 64 rows).
These are the last two "ephemeral per-transaction" counterparty sites
(generate_company_revenue_transactions/generate_self_employment_income_
transactions - a fresh fake identity every single transaction) converted to
real, bank-homed, ledger-tracked, customer_info-listed accounts, same
"coherent virtual banking world" work as Checkpoints 1-2, Rio's call,
2026-09-02. Full plan at /Users/ufhm/.claude/plans/parallel-drifting-aho.md,
Checkpoint 3.

Unlike Checkpoint 2's shared pools (any payer can draw any landlord), these
are scoped PER OWNER via a new `owner_entity_id` column - the same synthetic
"customer" recurring identically across unrelated companies would be its
own realism problem, so each company/person gets its own small pool drawn
from repeatedly, never shared with another owner's pool.

These entities are PAYERS (the source of the transaction, paying the
company/self-employed person), not receive-only - same lesson as
Checkpoint 2's card_processor finding: they need a real starting balance to
fund their own payments, not $0 (config's customer_pool_opening_balance).

owner_type is drawn once per pool row (not per transaction) - revenue_
customer mixes Person/Company (50/50, matching the prior ephemeral logic's
random.choice(["Person", "Company"])); self_employment_client is always
Company (matching the prior logic exactly - a client or unearned-income
distributor is always a business).

Run once, after add_new_banks.py:
    ./aml-env/bin/python3 scripts/add_customer_pools.py
"""
import random

import pandas as pd
from faker import Faker

PROFILES_PATH = "agents/agent_profiles.xlsx"
SEED = 20260902

WIDGETS_AND_MERCHANTS = 325
ALL_BANKS = [138, 211, 325, 513, 647]
LEAN_FRACTION = 0.75
POOL_SIZE_PER_OWNER = 8


def new_unique_account_number(rng: random.Random, existing: set) -> int:
    while True:
        candidate = rng.randint(100_000_000, 999_999_999)
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def draw_bank(rng: random.Random) -> int:
    if rng.random() < LEAN_FRACTION:
        return WIDGETS_AND_MERCHANTS
    return rng.choice(ALL_BANKS)


def main():
    rng = random.Random(SEED)
    fake = Faker()
    Faker.seed(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if not (df["entity_id"] == "BANK0325").any():
        raise SystemExit("Widgets & Merchants (BANK0325) not found - run scripts/add_new_banks.py first, aborting.")
    for prefix in ("REVC", "SECL"):
        if df["entity_id"].str.startswith(prefix).any():
            raise SystemExit(f"entity_id prefix {prefix} already in use - data may have changed, aborting.")
    if "owner_entity_id" not in df.columns:
        df["owner_entity_id"] = pd.NA

    existing_accounts = set(df["account_number"].dropna().astype(int))
    existing_customer_ids = df["customer_id"].dropna()
    existing_customer_ids = existing_customer_ids[existing_customer_ids.str.startswith("CUST-")]
    next_customer_id = existing_customer_ids.str.replace("CUST-", "", regex=False).astype(int).max() + 1

    new_rows = []

    companies = df[df["type"] == "company"]
    for _, company in companies.iterrows():
        for i in range(1, POOL_SIZE_PER_OWNER + 1):
            owner_type = "Person" if rng.random() < 0.5 else "Company"
            entity_id = f"REVC{next_customer_id}"  # unique regardless of owner, avoids per-owner prefix collisions
            new_rows.append({
                "entity_id": entity_id,
                "type": "revenue_customer",
                "owner_entity_id": company["entity_id"],
                "bank": draw_bank(rng),
                "sends": "N", "receives": "Y",
                "name": fake.name() if owner_type == "Person" else fake.company(),
                "address": fake.address().replace("\n", ", "),
                "account_number": new_unique_account_number(rng, existing_accounts),
                "naics_code": 999.0,
                "naics_description": "Customer",
                "customer_id": f"CUST-{next_customer_id:06d}",
                "party_id": entity_id,
            })
            next_customer_id += 1

    persons_no_employer = df[(df["type"] == "person") & (df["employer"].isna())]
    for _, person in persons_no_employer.iterrows():
        for i in range(1, POOL_SIZE_PER_OWNER + 1):
            entity_id = f"SECL{next_customer_id}"
            new_rows.append({
                "entity_id": entity_id,
                "type": "self_employment_client",
                "owner_entity_id": person["entity_id"],
                "bank": draw_bank(rng),
                "sends": "N", "receives": "Y",
                "name": fake.company(),
                "address": fake.address().replace("\n", ", "),
                "account_number": new_unique_account_number(rng, existing_accounts),
                "naics_code": 999.0,
                "naics_description": "Customer",
                "customer_id": f"CUST-{next_customer_id:06d}",
                "party_id": entity_id,
            })
            next_customer_id += 1

    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    n_revenue = sum(1 for r in new_rows if r["type"] == "revenue_customer")
    n_self_emp = sum(1 for r in new_rows if r["type"] == "self_employment_client")
    print(f"Added {n_revenue} revenue_customer rows across {len(companies)} companies.")
    print(f"Added {n_self_emp} self_employment_client rows across {len(persons_no_employer)} persons.")


if __name__ == "__main__":
    main()
