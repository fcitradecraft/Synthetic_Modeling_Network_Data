"""
One-time data addition: assign business ownership (`owners` column) on
company rows in Combined_Data, linking persons to businesses they own -
sometimes jointly. Previously the only person-company link was `employer`
(a W-2 relationship); there was no ownership relationship in the data at
all, so no scenario could show a person receiving distributions from a
business they own, and companies had no traceable owner.

Scope (Rio's call, 2026-07-10): only the ~18 smaller/local-business
archetypes get an owner from the 22-person roster (RESTAURANT, RETAIL,
GAS_STATION, DELIVERY_LOGISTICS, FACILITY_SERVICES, PROFESSIONAL_SERVICES -
see config/payment_ranges.yaml's company_segments). MANUFACTURING,
FINANCIAL_SERVICES, IMPORT_EXPORT, WHOLESALE_DISTRIBUTION, and REAL_ESTATE
stay unowned by any roster person - institutional/unknown ownership, which
is itself realistic for larger entities.

Every one of the 22 persons owns (solely or jointly) at least one company -
several as an owner-operator of the same company they're also employed by
(e.g. Lauren Kennedy owns and works at Couture Designs/COMP1013), several
as a side business separate from their day job, and four pairs as joint
owners (Rio specifically asked for "sometimes jointly").

Format: `owners` is a comma-separated list of `entity_id:pct` pairs, e.g.
"PERS1002:100" (sole) or "PERS1010:50,PERS1012:50" (joint, must sum to 100).

Run once: ./aml-env/bin/python3 scripts/assign_business_ownership.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# company entity_id -> owners string
OWNERSHIP = {
    # RESTAURANT
    "COMP1022": "PERS1002:100",             # Mackeys - Craig Jackson
    "COMP1026": "PERS1008:100",             # The Melange Lounge LLC - Debbie Walters
    "COMP1027": "PERS1010:50,PERS1012:50",  # Arrakis Cantina & Bar - Jesse Vaughn / Franklin R. Henderson (joint)
    "COMP1028": "PERS1013:100",             # Giedi Prime Gastropub - Helen Gaia
    "COMP1031": "PERS1016:100",             # Bene Gesserit Bistro - Marcus T. Stilwell
    "COMP1032": "PERS1006:60,PERS1017:40",  # Kwisatz Kitchen Inc - Chad Harrison / David Caladan (joint)
    "COMP1034": "PERS1021:100",             # Chani's Kitchen & Catering - Lila R. Sandoval
    # RETAIL
    "COMP1010": "PERS1003:100",             # Sahara - James Turner (also employed there)
    "COMP1013": "PERS1001:100",             # Couture Designs - Lauren Kennedy (also employed there)
    "COMP1014": "PERS1009:100",             # Brittany's - Stephanie Brown
    "COMP1015": "PERS1018:50,PERS1020:50",  # Couture Designs - Sarah J. Sietch / Vladimir H. Baron (joint, both employed there)
    # GAS_STATION
    "COMP1011": "PERS1005:100",             # Great Gas - Zachary Munoz DVM
    "COMP1012": "PERS1022:100",             # Dinoco - Thufir M. Hawat
    # DELIVERY_LOGISTICS
    "COMP1016": "PERS1019:100",             # Speedee Delivery - Paul A. Muaddib
    "COMP1017": "PERS1004:50,PERS1011:50",  # Mr. Delivery - Melanie Martinez / Feyd R. Harkonnen (joint, both employed there)
    # FACILITY_SERVICES
    "COMP1021": "PERS1007:100",             # Office Waste Retrieval - Brian Hancock
    "COMP1008": "PERS1015:100",             # Seaside Dock Management - Grisela Lopes
    # PROFESSIONAL_SERVICES
    "COMP1002": "PERS1014:100",             # Hopkins, Branch and Flores - Feyd Harkonnen
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if "owners" in df.columns:
        raise SystemExit("owners column already present - ownership already assigned, aborting.")

    df["owners"] = pd.NA
    for entity_id, owners_str in OWNERSHIP.items():
        mask = (df["type"] == "company") & (df["entity_id"] == entity_id)
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found among company rows - check OWNERSHIP mapping.")
        df.loc[mask, "owners"] = owners_str

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Assigned owners to {len(OWNERSHIP)} companies, covering all 22 persons in the roster.")


if __name__ == "__main__":
    main()
