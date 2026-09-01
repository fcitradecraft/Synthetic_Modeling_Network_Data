"""
One-time data fix: five merchant rows in Combined_Data were all named
"Brittany's" (naics 458, Clothing/Jewelry) with average_expense values of
$12,500 / $3,000 / $2,500 / $30,000 / $25,000 - unrelated to the actual
"Brittany's" company row (COMP1014, average_expense $321.83) and far above
anything realistic for a routine walk-in clothing/jewelry purchase. Most
other duplicate-named merchants in the roster are legitimately multiple
locations of the same chain with consistent average_expense (USPS, Mobile,
Kwik-e-mart, etc.) - these five were the anomaly, not the pattern.

Rio's call, 2026-07-11: rename these five to distinct, sensible businesses
whose average_expense actually fits their category, rather than just
capping the number under the same name. Paired with a code-level ceiling
(config/payment_ranges.yaml's max_merchant_average_expense) so a similar
bad value elsewhere can't blow up a purchase amount the same way again.

Run once: ./aml-env/bin/python3 scripts/fix_brittanys_merchant_records.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# entity_id -> (new name, naics_code, naics_description, new average_expense)
# Each new average_expense sits comfortably within (or just above) the
# existing peer range already in that naics category, so none of these
# stand out as outliers anymore.
REPLACEMENTS = {
    "MER1013": ("Hallmark Jewelers", 458.0,
                "Clothing, Clothing Accessories, Shoe, and Jewelry Retailers", 1800.0),
    "MER1016": ("Thornwood Appliance Co.", 449.0,
                "Furniture, Home Furnishings, Electronics, and Appliance Retailers", 2200.0),
    "MER1017": ("Ridgeline Lumber & Supply", 444.0,
                "Building Material and Garden Equipment and Supplies Dealers", 650.0),
    "MER1018": ("Cascade Furniture Gallery", 449.0,
                "Furniture, Home Furnishings, Electronics, and Appliance Retailers", 4500.0),
    "MER1019": ("Meridian Electronics", 449.0,
                "Furniture, Home Furnishings, Electronics, and Appliance Retailers", 900.0),
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    for entity_id, (name, naics_code, naics_desc, avg_exp) in REPLACEMENTS.items():
        mask = (df["type"] == "merchant") & (df["entity_id"] == entity_id)
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found among merchant rows - aborting.")
        current_name = df.loc[mask, "name"].iloc[0]
        if current_name != "Brittany's":
            raise SystemExit(
                f"entity_id {entity_id} is '{current_name}', not \"Brittany's\" - "
                "data may have already been fixed, aborting."
            )
        df.loc[mask, "name"] = name
        df.loc[mask, "naics_code"] = naics_code
        df.loc[mask, "naics_description"] = naics_desc
        df.loc[mask, "average_expense"] = avg_exp

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Renamed {len(REPLACEMENTS)} broken \"Brittany's\" merchant rows to distinct businesses.")


if __name__ == "__main__":
    main()
