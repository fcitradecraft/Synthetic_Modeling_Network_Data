"""
Adds four bounded pools of persistent counterparty entities to Combined_Data
- landlord, utility_provider, supplier, card_processor - so the "persistent
per-relationship" counterparties generator/transactions.py already creates
once per payer (a landlord reused across all of someone's rent payments, a
utility provider reused across all their bills, a restocking supplier
reused across all of a company's payments, a card-network settlement
counterpart) can be real, bank-homed, ledger-tracked accounts instead of a
disposable `fake.company()` identity with no account and no bank. Part of
the "coherent virtual banking world" work, Rio's call, 2026-09-02 - full
plan at /Users/ufhm/.claude/plans/parallel-drifting-aho.md, Checkpoint 2.

Bank assignment is a probabilistic LEAN, not a rigid rule (Rio: "in real
life it is not a full population by code, just a type of company or
entity primarily served") - landlord/utility_provider/supplier lean
~75% to First Bank of Expense and Overhead (BANK0647), card_processor
leans ~75% to Widgets & Merchants (BANK0325); the remainder spreads evenly
across all 5 banks. Run add_new_banks.py first.

Pool sizes (~40-60/~30-40/~30-40/~20-30 per the approved plan): landlord
50, utility_provider 35, supplier 35, card_processor 25 - each entity gets
a real account_number/customer_id (same "CUST-######" convention as
scripts/extend_customer_schema.py) and a NAICS classification so
customer_info's type_of_business shows something sensible.

Run once, after add_new_banks.py:
    ./aml-env/bin/python3 scripts/add_counterparty_pools.py
"""
import random

import pandas as pd
from faker import Faker

PROFILES_PATH = "agents/agent_profiles.xlsx"
SEED = 20260902

FIRST_BANK_OF_EXPENSE_AND_OVERHEAD = 647
WIDGETS_AND_MERCHANTS = 325
ALL_BANKS = [138, 211, 325, 513, 647]
LEAN_FRACTION = 0.75

POOL_SPECS = {
    "landlord": {"prefix": "LAND", "count": 50, "lean_bank": FIRST_BANK_OF_EXPENSE_AND_OVERHEAD,
                 "naics_code": 531.0, "naics_description": "Real Estate Rental and Leasing"},
    "utility_provider": {"prefix": "UTIL", "count": 35, "lean_bank": FIRST_BANK_OF_EXPENSE_AND_OVERHEAD,
                          "naics_code": 221.0, "naics_description": "Utilities"},
    "supplier": {"prefix": "SUPL", "count": 35, "lean_bank": FIRST_BANK_OF_EXPENSE_AND_OVERHEAD,
                 "naics_code": 424.0, "naics_description": "Wholesale Trade Suppliers"},
    "card_processor": {"prefix": "CARD", "count": 25, "lean_bank": WIDGETS_AND_MERCHANTS,
                        "naics_code": 522.0, "naics_description": "Financial Transactions Processing"},
}


def new_unique_account_number(rng: random.Random, existing: set) -> int:
    while True:
        candidate = rng.randint(100_000_000, 999_999_999)
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def draw_bank(rng: random.Random, lean_bank: int) -> int:
    if rng.random() < LEAN_FRACTION:
        return lean_bank
    return rng.choice(ALL_BANKS)


def main():
    rng = random.Random(SEED)
    fake = Faker()
    Faker.seed(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if not (df["entity_id"] == "BANK0647").any() or not (df["entity_id"] == "BANK0325").any():
        raise SystemExit("New banks not found - run scripts/add_new_banks.py first, aborting.")
    for spec in POOL_SPECS.values():
        prefix = spec["prefix"]
        if df["entity_id"].str.startswith(prefix).any():
            raise SystemExit(f"entity_id prefix {prefix} already in use - data may have changed, aborting.")

    existing_accounts = set(df["account_number"].dropna().astype(int))
    existing_customer_ids = df["customer_id"].dropna()
    existing_customer_ids = existing_customer_ids[existing_customer_ids.str.startswith("CUST-")]
    next_customer_id = existing_customer_ids.str.replace("CUST-", "", regex=False).astype(int).max() + 1

    new_rows = []
    for entity_type, spec in POOL_SPECS.items():
        for i in range(1, spec["count"] + 1):
            entity_id = f"{spec['prefix']}{1000 + i}"
            new_rows.append({
                "entity_id": entity_id,
                "type": entity_type,
                "bank": draw_bank(rng, spec["lean_bank"]),
                "sends": "N", "receives": "Y",
                "name": fake.company(),
                "address": fake.address().replace("\n", ", "),
                "account_number": new_unique_account_number(rng, existing_accounts),
                "naics_code": spec["naics_code"],
                "naics_description": spec["naics_description"],
                "customer_id": f"CUST-{next_customer_id:06d}",
                # Own-entity singleton party (same convention as merchants/
                # BEnt/bank rows) - required now that these are eligible
                # rule-injection targets too (Rio's call, 2026-09-02):
                # leaving party_id NaN would collapse every pool entity
                # into one shared party-group bucket in main.py's
                # accounts_by_party_id.
                "party_id": entity_id,
            })
            next_customer_id += 1

    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    for entity_type, spec in POOL_SPECS.items():
        print(f"Added {spec['count']} {entity_type} rows.")


if __name__ == "__main__":
    main()
