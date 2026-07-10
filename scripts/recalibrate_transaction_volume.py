"""
One-time data recalibration: rescale merchant_frequency on person/company
rows toward researched real-world monthly transaction-volume targets, and
give company rows what they need to receive money (accepted_payment_methods,
average_expense, revenue_monthly_target) - previously 0/34 companies could
ever be a transaction target despite sends/receives already marking every
company Y/Y.

Targets (see CLAUDE session research, 2026-07-09):
  - Persons: ~70 transactions/month (Boston Fed Survey of Consumer Payment
    Choice, 2017), with real spread across the population rather than
    everyone landing near the mean.
  - Companies: 50-150 transactions/month total across both directions (Fed
    Payments Study 2015: businesses average ~54/month in checks+ACH alone;
    bank fee-tier data treats <100/mo as the smallest tier). Split roughly
    half outbound (existing merchant_frequency purchase pattern, rescaled)
    and half inbound (new revenue_monthly_target, consumed by
    generate_company_revenue_transactions in generator/transactions.py).
    Split size correlates with the existing transaction_scaler column
    (already varies 0.5x-5x across companies) so bigger-scaler companies
    land toward 150/mo and smaller ones toward 50/mo, not a flat number.

Run once: ./aml-env/bin/python3 scripts/recalibrate_transaction_volume.py
"""
import random

import pandas as pd

PROFILES_PATH = "agents/agent_profiles.xlsx"
SEED = 20260709  # fixed so re-running (after the guard is bypassed) reproduces the same values

PERSON_TARGET_MEAN = 70.0
PERSON_TARGET_STD = 20.0
PERSON_TARGET_FLOOR = 20.0

COMPANY_TARGET_MIN = 50.0
COMPANY_TARGET_MAX = 150.0
COMPANY_SCALER_MIN = 0.5
COMPANY_SCALER_MAX = 5.0

# Reuses payment-method tokens already present elsewhere in Combined_Data
# (see merchant rows) rather than inventing new ones - weighted toward B2B
# rails (ACH/Wire/Check) instead of merchants' cash/card-heavier mix.
COMPANY_PAYMENT_METHOD_OPTIONS = [
    "ACH, Wire, Check",
    "ACH, Check, Cash",
    "Wire, ACH, C_Check",
    "ACH, Check, Wire, Cash",
]


def rescale_frequency(freq_str, target_total, rng):
    freqs = [float(f.strip()) for f in freq_str.split(",") if f.strip()]
    current_total = sum(freqs)
    if current_total <= 0:
        return freq_str
    multiplier = target_total / current_total
    rescaled = [round(f * multiplier, 2) for f in freqs]
    return ", ".join(str(v) for v in rescaled)


def main():
    rng = random.Random(SEED)

    xl = pd.read_excel(PROFILES_PATH, sheet_name=None)
    df = xl["Combined_Data"]

    if "revenue_monthly_target" in df.columns:
        raise SystemExit("revenue_monthly_target already present - volume already recalibrated, aborting.")

    person_mask = df["type"] == "person"
    company_mask = df["type"] == "company"

    # --- Persons: rescale merchant_frequency to ~70/mo with spread ---
    for idx in df[person_mask].index:
        target = max(PERSON_TARGET_FLOOR, rng.gauss(PERSON_TARGET_MEAN, PERSON_TARGET_STD))
        df.at[idx, "merchant_frequency"] = rescale_frequency(df.at[idx, "merchant_frequency"], target, rng)

    # --- Companies: split target volume into outbound (existing purchase
    # pattern) and inbound revenue, sized off transaction_scaler ---
    df["revenue_monthly_target"] = pd.NA
    for idx in df[company_mask].index:
        scaler = float(df.at[idx, "transaction_scaler"] or 1.0)
        size_frac = (scaler - COMPANY_SCALER_MIN) / (COMPANY_SCALER_MAX - COMPANY_SCALER_MIN)
        size_frac = min(1.0, max(0.0, size_frac))
        target_total = COMPANY_TARGET_MIN + size_frac * (COMPANY_TARGET_MAX - COMPANY_TARGET_MIN)
        target_total += rng.gauss(0, 15)
        target_total = min(COMPANY_TARGET_MAX, max(COMPANY_TARGET_MIN, target_total))

        outbound_frac = rng.uniform(0.4, 0.6)
        outbound_target = target_total * outbound_frac
        revenue_target = target_total - outbound_target

        df.at[idx, "merchant_frequency"] = rescale_frequency(
            df.at[idx, "merchant_frequency"], outbound_target, rng
        )
        df.at[idx, "revenue_monthly_target"] = round(revenue_target, 2)

        if pd.isna(df.at[idx, "accepted_payment_methods"]):
            df.at[idx, "accepted_payment_methods"] = rng.choice(COMPANY_PAYMENT_METHOD_OPTIONS)
        if pd.isna(df.at[idx, "average_expense"]):
            df.at[idx, "average_expense"] = round(rng.uniform(300, 1500) * scaler, 2)

    xl["Combined_Data"] = df
    with pd.ExcelWriter(PROFILES_PATH, engine="openpyxl") as writer:
        for sheet_name, sheet_df in xl.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    n_persons = person_mask.sum()
    n_companies = company_mask.sum()
    print(f"Rescaled merchant_frequency on {n_persons} person rows toward ~{PERSON_TARGET_MEAN:.0f}/mo.")
    print(f"Rescaled outbound merchant_frequency + set revenue_monthly_target on {n_companies} company rows "
          f"toward {COMPANY_TARGET_MIN:.0f}-{COMPANY_TARGET_MAX:.0f}/mo total.")
    print("Populated accepted_payment_methods/average_expense on any company rows that were blank.")


if __name__ == "__main__":
    main()
