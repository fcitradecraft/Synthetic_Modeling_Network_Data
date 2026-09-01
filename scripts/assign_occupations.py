"""
One-time data addition: assign an `occupation` to every person row in
Combined_Data, so customer_info's type_of_business field can show a
person's occupation instead of sitting blank (companies already show
naics_description there). Rio's call, 2026-07-10.

Titles are informed by each person's actual data, not generic:
- Employed persons get a title consistent with their employer's industry
  archetype (config/payment_ranges.yaml's company_segments) - varied by
  income_level within an archetype (e.g. Manufacturing-employed persons
  span Production Line Worker up to Production Supervisor), not one
  generic label per archetype.
- The 6 self-employed persons all already own a RESTAURANT-archetype
  company (scripts/assign_business_ownership.py) - "Restaurant Owner" (or
  "Bar Owner" for the two lounge/bar-flavored ones).
- The 2 Unemployed/High-income persons (Chad Harrison, David Caladan) both
  co-own Kwisatz Kitchen Inc - "Retired" fits both employment_status and
  the passive-ownership-income narrative already established for them.
- One deliberate name-driven exception: Zachary Munoz **DVM** gets
  "Veterinarian" rather than a title matching his (Manufacturing) employer
  - the name is clearly an intentional detail in the existing roster.

Run once: ./aml-env/bin/python3 scripts/assign_occupations.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# person entity_id -> occupation
OCCUPATIONS = {
    # Employed - RETAIL (owner-operators)
    "PERS1001": "Retail Store Owner",     # Lauren Kennedy - Couture Designs
    "PERS1003": "Retail Store Owner",     # James Turner - Sahara
    "PERS1018": "Retail Store Owner",     # Sarah J. Sietch - Couture Designs (joint)
    "PERS1020": "Retail Store Owner",     # Vladimir H. Baron - Couture Designs (joint)
    # Employed - DELIVERY_LOGISTICS
    "PERS1004": "Delivery Service Owner",  # Melanie Martinez - Mr. Delivery (joint owner)
    "PERS1011": "Delivery Service Owner",  # Feyd R. Harkonnen - Mr. Delivery (joint owner)
    "PERS1014": "Delivery Driver",         # Feyd Harkonnen - employed at Mr. Delivery, not an owner there
    # Employed - MANUFACTURING (varied by income_level, not one generic label)
    "PERS1005": "Veterinarian",              # Zachary Munoz DVM - name-driven exception
    "PERS1007": "Production Line Worker",    # Brian Hancock - Renewal by Gerald, Low income
    "PERS1015": "Warehouse Associate",       # Grisela Lopes - Renewal by Gerald, Low income
    "PERS1009": "Quality Control Inspector", # Stephanie Brown - Graham-Potts, Medium income
    "PERS1021": "Production Supervisor",     # Lila R. Sandoval - Graham-Potts, Medium income
    "PERS1019": "Machine Operator",          # Paul A. Muaddib - Charmont Paper Products, Medium income
    "PERS1022": "Plant Maintenance Technician",  # Thufir M. Hawat - Charmont Paper Products, Medium income
    # Self-employed - all 6 already own a RESTAURANT-archetype company
    "PERS1002": "Restaurant Owner",  # Craig Jackson - Mackeys
    "PERS1008": "Bar Owner",         # Debbie Walters - The Melange Lounge LLC
    "PERS1010": "Restaurant Owner",  # Jesse Vaughn - Arrakis Cantina & Bar (joint)
    "PERS1012": "Restaurant Owner",  # Franklin R. Henderson - Arrakis Cantina & Bar (joint)
    "PERS1013": "Restaurant Owner",  # Helen Gaia - Giedi Prime Gastropub
    "PERS1016": "Restaurant Owner",  # Marcus T. Stilwell - Bene Gesserit Bistro
    # Unemployed/High-income - co-own Kwisatz Kitchen Inc, passive income
    "PERS1006": "Retired",  # Chad Harrison
    "PERS1017": "Retired",  # David Caladan
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if "occupation" in df.columns:
        raise SystemExit("occupation column already present - already assigned, aborting.")

    df["occupation"] = pd.NA
    for entity_id, occupation in OCCUPATIONS.items():
        mask = (df["type"] == "person") & (df["entity_id"] == entity_id)
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found among person rows - check OCCUPATIONS mapping.")
        df.loc[mask, "occupation"] = occupation

    unassigned = df[(df["type"] == "person") & df["occupation"].isna()]
    if not unassigned.empty:
        raise SystemExit(f"{len(unassigned)} person rows left without an occupation: "
                          f"{unassigned['entity_id'].tolist()}")

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Assigned occupations to all {len(OCCUPATIONS)} persons in the roster.")


if __name__ == "__main__":
    main()
