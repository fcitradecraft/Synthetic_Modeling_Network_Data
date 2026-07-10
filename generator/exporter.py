import os
import re

import pandas as pd


def ensure_directory_exists(filepath):
    directory = os.path.dirname(filepath)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)


def _safe_label(bank_name):
    """Turn a bank name into something safe for a filename or an Excel
    sheet name (sheet names: no : \\ / ? * [ ], max 31 chars)."""
    label = re.sub(r"[:\\/?*\[\]]", "", str(bank_name)).strip().replace(" ", "_")
    return label[:31] or "unknown_bank"


def export_to_csv_by_bank(transactions_by_bank, filepath):
    """Write one CSV per bank - each file is what that bank's own core
    system would show (its own accounts' legs only)."""
    ensure_directory_exists(filepath)
    base, ext = os.path.splitext(filepath)
    for bank, rows in transactions_by_bank.items():
        bank_path = f"{base}_{_safe_label(bank)}{ext}"
        df = pd.DataFrame(rows)
        df.to_csv(bank_path, index=False)
        print(f"[✔] Exported {len(df)} transactions to {bank_path}")


def export_to_excel_by_bank(transactions_by_bank, filepath):
    """Write one workbook with one sheet per bank."""
    ensure_directory_exists(filepath)
    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        for bank, rows in transactions_by_bank.items():
            df = pd.DataFrame(rows)
            df.to_excel(writer, sheet_name=_safe_label(bank), index=False)
    total = sum(len(rows) for rows in transactions_by_bank.values())
    print(f"[✔] Exported {total} transactions to {filepath} ({len(transactions_by_bank)} bank sheets)")
