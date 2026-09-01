"""
One-time data fix: the 3 banks' swift_code values in Combined_Data were
malformed mixed-case strings ("KKqlLyGM", "yMxruLqs", "pLATCDye") - not
real BIC format (8 or 11 characters, uppercase letters/digits: 4-char bank
code + 2-char country code + 2-char location code, optionally + 3-char
branch code). Needed now that validate.py's check_bic_length/
check_bic_not_real_institution gates are about to actually run against
swift_code on exported wire rows instead of SKIPping.

Fictitious codes, structurally valid, don't match validate.py's
REAL_BIC_DENY_LIST or any real institution.

Run once: ./aml-env/bin/python3 scripts/fix_bank_swift_codes.py
"""
import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"

# entity_id -> new swift_code
SWIFT_CODES = {
    "BANK0138": "MSNLUS31",  # Misfits Savings and Loan
    "BANK0211": "WFQDUS22",  # Wells Farquod
    "BANK0513": "BOPLUS44",  # Bank of Pineland
}


def main():
    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    for entity_id, swift_code in SWIFT_CODES.items():
        mask = (df["type"] == "bank") & (df["entity_id"] == entity_id)
        if not mask.any():
            raise SystemExit(f"entity_id {entity_id} not found among bank rows - aborting.")
        df.loc[mask, "swift_code"] = swift_code

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"Fixed swift_code for {len(SWIFT_CODES)} banks.")


if __name__ == "__main__":
    main()
