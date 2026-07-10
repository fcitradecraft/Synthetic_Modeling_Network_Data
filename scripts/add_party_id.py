"""
One-time schema extension: add party_id to agents/agent_profiles.xlsx's
Combined_Data sheet, grouping accounts into "customer relationship"
parties for the 3 party-group Alert Matrix rules (not built this round,
but the field is added now so it doesn't require another schema
migration later).

Modeling choice: a person and their employer share a party_id (a
plausible reason accounts get reviewed together - e.g. a business
owner's personal and business accounts). Everyone else (unlinked
persons, merchants, banks, BEnt) gets a singleton party_id equal to
their own entity_id. This is a synthetic-data plausibility choice, not
a KYC/beneficial-ownership determination.

Run once: ./aml-env/bin/python3 scripts/add_party_id.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if "party_id" in df.columns:
        raise SystemExit("party_id already present - schema already extended, aborting.")

    df["party_id"] = df["entity_id"]

    employed = df[(df["type"] == "person") & df["employer"].notna()]
    for idx, row in employed.iterrows():
        employer_matches = df.index[df["entity_id"] == row["employer"]]
        if len(employer_matches):
            employer_idx = employer_matches[0]
            shared_party = df.at[employer_idx, "entity_id"]
            df.at[idx, "party_id"] = shared_party
            df.at[employer_idx, "party_id"] = shared_party

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    n_parties = df["party_id"].nunique()
    n_multi = (df["party_id"].value_counts() > 1).sum()
    print(f"party_id added: {n_parties} distinct parties, {n_multi} multi-account parties.")


if __name__ == "__main__":
    main()
