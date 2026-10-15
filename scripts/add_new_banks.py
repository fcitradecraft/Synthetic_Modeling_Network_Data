"""
Adds two new full-peer banks to Combined_Data: "Widgets & Merchants" (a
commerce-side home, for re-homed merchants and future revenue-side
counterparty pools) and "First Bank of Expense and Overhead" (an overhead-
side home, for landlord/utility/supplier-type counterparty pools). Rio's
call, 2026-09-02, part of the "coherent virtual banking world" work -
every counterparty in this generator should actually bank somewhere,
including entities beyond the sampled person/company roster. Full plan at
/Users/ufhm/.claude/plans/parallel-drifting-aho.md.

Both banks get the same treatment as the existing 3 (Misfits Savings and
Loan/Wells Farquod/Bank of Pineland): a `type="bank"` row with its own
bank code/SWIFT/ABA routing number, plus BEnt branch/ATM rows (naming and
naics convention matched exactly to the existing rows - "ATM" must appear
in an ATM row's name, since utils/helpers.py's get_bent_type() classifies
purely by that substring). Bank/BEnt infrastructure is 100% data-driven
(confirmed by grep - no bank name is hardcoded anywhere in code), so this
is the entire change needed for both banks to appear correctly in per-bank
export partitioning, bank_name/swift_code resolution, and BEnt-driven cash
mechanics.

Run once: ./aml-env/bin/python3 scripts/add_new_banks.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

NEW_BANKS = [
    {
        "entity_id": "BANK0325", "type": "bank", "bank": 325, "sends": "N", "receives": "Y",
        "name": "Widgets & Merchants", "swift_code": "WDGMUS33", "aba_routing_number": 411287560.0,
        "address": "1200 Commerce Ave, Dallas, TX 75201", "bank_type": "Commercial", "party_id": "BANK0325",
    },
    {
        "entity_id": "BANK0647", "type": "bank", "bank": 647, "sends": "N", "receives": "Y",
        "name": "First Bank of Expense and Overhead", "swift_code": "FBEOUS44", "aba_routing_number": 552910734.0,
        "address": "88 Overhead Plaza, Columbus, OH 43215", "bank_type": "Commercial", "party_id": "BANK0647",
    },
]

NEW_BENTS = [
    # Widgets & Merchants (bank 325)
    {"entity_id": "BENT1019", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants ATM 325100001", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1019"},
    {"entity_id": "BENT1020", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants ATM 325100002", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1020"},
    {"entity_id": "BENT1021", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants ATM 325100003", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1021"},
    {"entity_id": "BENT1022", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants ATM 325100004", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1022"},
    {"entity_id": "BENT1023", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants Branch 325200001", "naics_code": 522.0, "naics_description": "Bank Teller",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1023"},
    {"entity_id": "BENT1024", "type": "BEnt", "bank": 325, "sends": "N", "receives": "N",
     "name": "Widgets & Merchants Branch 325200002", "naics_code": 522.0, "naics_description": "Bank Teller",
     "address": "1200 Commerce Ave, Dallas, TX 75201", "party_id": "BENT1024"},
    # First Bank of Expense and Overhead (bank 647)
    {"entity_id": "BENT1025", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead ATM 647100001", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1025"},
    {"entity_id": "BENT1026", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead ATM 647100002", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1026"},
    {"entity_id": "BENT1027", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead ATM 647100003", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1027"},
    {"entity_id": "BENT1028", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead ATM 647100004", "naics_code": 522.0, "naics_description": "Automated Teller Machine",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1028"},
    {"entity_id": "BENT1029", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead Branch 647200001", "naics_code": 522.0, "naics_description": "Bank Teller",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1029"},
    {"entity_id": "BENT1030", "type": "BEnt", "bank": 647, "sends": "N", "receives": "N",
     "name": "First Bank of Expense and Overhead Branch 647200002", "naics_code": 522.0, "naics_description": "Bank Teller",
     "address": "88 Overhead Plaza, Columbus, OH 43215", "party_id": "BENT1030"},
]


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    for new_id in [b["entity_id"] for b in NEW_BANKS] + [b["entity_id"] for b in NEW_BENTS]:
        if (df["entity_id"] == new_id).any():
            raise SystemExit(f"entity_id {new_id} already exists - data may have changed, aborting.")

    new_rows = pd.DataFrame(NEW_BANKS + NEW_BENTS)
    df = pd.concat([df, new_rows], ignore_index=True)

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Added {len(NEW_BANKS)} banks and {len(NEW_BENTS)} BEnt rows.")


if __name__ == "__main__":
    main()
