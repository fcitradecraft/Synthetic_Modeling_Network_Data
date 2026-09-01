"""
One-time data fix: 11 of 34 companies in Combined_Data - all legacy
Actimize-migrated customers - have never had a naics_code/naics_description,
leaving customer_info's type_of_business blank for them (including
Bene Gesserit Bistro, which can be a flagged/alerted account - a student
reviewing that alert has no documented business type to reason against).

NAICS codes below are real 3-digit sector titles, matching the format
already used elsewhere in the roster, and match each company's existing
company_segments archetype (config/payment_ranges.yaml) - no archetype
reassignment, just backfilling the classification that was always missing.

Run once: ./aml-env/bin/python3 scripts/backfill_legacy_naics.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# entity_id -> (naics_code, naics_description)
NAICS = {
    "COMP1024": (525.0, "Funds, Trusts, and Other Financial Vehicles"),          # TADECO LLC - fbo F. Markos Trust
    "COMP1025": (424.0, "Merchant Wholesalers, Nondurable Goods"),               # Spice Flow GmbH - US Operations
    "COMP1026": (722.0, "Food Services and Drinking Places"),                    # The Melange Lounge LLC
    "COMP1027": (722.0, "Food Services and Drinking Places"),                    # Arrakis Cantina & Bar
    "COMP1028": (722.0, "Food Services and Drinking Places"),                    # Giedi Prime Gastropub
    "COMP1029": (424.0, "Merchant Wholesalers, Nondurable Goods"),               # Fremen Food Distributors LLC
    "COMP1030": (423.0, "Merchant Wholesalers, Durable Goods"),                  # Dune Imports & Exports Inc
    "COMP1031": (722.0, "Food Services and Drinking Places"),                    # Bene Gesserit Bistro
    "COMP1032": (722.0, "Food Services and Drinking Places"),                    # Kwisatz Kitchen Inc
    "COMP1033": (523.0, "Securities, Commodity Contracts, and Other Financial Investments and Related Activities"),  # Corrino Capital Partners
    "COMP1034": (722.0, "Food Services and Drinking Places"),                    # Chani's Kitchen & Catering
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    for entity_id, (naics_code, naics_desc) in NAICS.items():
        mask = (df["type"] == "company") & (df["entity_id"] == entity_id)
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found among company rows - aborting.")
        if df.loc[mask, "naics_code"].notna().any():
            raise SystemExit(f"entity_id {entity_id} already has a naics_code - aborting to avoid overwriting.")
        df.loc[mask, "naics_code"] = naics_code
        df.loc[mask, "naics_description"] = naics_desc

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Backfilled naics_code/naics_description for {len(NAICS)} legacy companies.")


if __name__ == "__main__":
    main()
