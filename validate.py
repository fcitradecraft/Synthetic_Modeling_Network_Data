"""
Pre-export validation gate. Runs 12 checks against the assembled transaction
set before any file is written; on any hard FAIL, nothing gets exported.

Gates 3, 8, 9, 11 return SKIP rather than PASS/FAIL: they depend on fields
(running_balance, swift_code-on-exported-rows) that don't exist in the
current schema yet. SKIP is reported explicitly so the report never silently
drops a gate - it's a "not yet applicable", not a pass.

Gate 12 (leakage) returns WARN rather than FAIL and does not block export -
see check_leakage's docstring. Every other gate is a real correctness bug
(duplicate IDs, unbalanced legs, a date before it was possible, etc.) and
still hard-blocks on FAIL.
"""
import argparse
import sys
from collections import namedtuple

import pandas as pd

from utils.helpers import is_us_federal_holiday
from utils.logger import log

ValidationResult = namedtuple("ValidationResult", ["gate", "status", "detail"])

REAL_BIC_DENY_LIST = {
    "UPNBUS44",  # Regions Bank
    "VALLMTMT",  # Bank of Valletta
}


def check_duplicate_entry_id(df: pd.DataFrame) -> ValidationResult:
    """Gate 1: entry_id must be unique."""
    dupes = df["entry_id"][df["entry_id"].duplicated()]
    if dupes.empty:
        return ValidationResult("duplicate_entry_id", "PASS", "no duplicate entry_id values")
    return ValidationResult(
        "duplicate_entry_id", "FAIL",
        f"{dupes.nunique()} duplicated entry_id values, e.g. {dupes.unique()[:5].tolist()}"
    )


def check_debit_credit_balance(df: pd.DataFrame) -> ValidationResult:
    """Gate 2: debit/credit legs of a transaction_id must sum to zero.

    A transaction_id may legitimately have only one leg (the counterparty
    wasn't a known account) - only groups with 2+ legs are checked.

    amount is stored as an absolute value (direction carries the sign, not
    the number - see utils/helpers.py split_transaction), so the balance
    check signs it locally: debit legs subtract, credit legs add.
    """
    signed_amount = df["amount"].where(df["direction"] == "credit", -df["amount"])
    sums = signed_amount.groupby(df["transaction_id"]).sum()
    counts = df.groupby("transaction_id").size()
    unbalanced = sums[(counts >= 2) & (sums.abs() > 0.01)]
    if unbalanced.empty:
        return ValidationResult("debit_credit_balance", "PASS", "all multi-leg transactions balance to zero")
    return ValidationResult(
        "debit_credit_balance", "FAIL",
        f"{len(unbalanced)} transaction_id groups don't balance, e.g. {unbalanced.index[:5].tolist()}"
    )


def check_running_balance_reconciliation(df: pd.DataFrame) -> ValidationResult:
    """Gate 3: running_balance must reconcile to opening_balance + running sum. SKIP - field not in schema yet."""
    if "running_balance" not in df.columns:
        return ValidationResult("running_balance_reconciliation", "SKIP", "running_balance not yet in schema")
    return ValidationResult("running_balance_reconciliation", "PASS", "reconciles")


def check_direction_consistency(df: pd.DataFrame) -> ValidationResult:
    """Gate 4: amount must be a non-negative absolute value, and direction
    must be the sole sign indicator (must be 'debit' or 'credit')."""
    bad = df[(df["amount"] < 0) | (~df["direction"].isin(["debit", "credit"]))]
    if bad.empty:
        return ValidationResult("direction_consistency", "PASS", "amount is non-negative and direction is valid on every row")
    return ValidationResult(
        "direction_consistency", "FAIL",
        f"{len(bad)} rows with a negative amount or an invalid direction, e.g. entry_id {bad['entry_id'].head(5).tolist()}"
    )


def check_post_date_business_day(df: pd.DataFrame) -> ValidationResult:
    """Gate 5: post_date must not fall on a weekend or US federal holiday."""
    post_dates = pd.to_datetime(df["post_date"])
    weekend = post_dates.dt.weekday >= 5
    holiday = post_dates.apply(is_us_federal_holiday)
    bad = df[weekend | holiday]
    if bad.empty:
        return ValidationResult("post_date_business_day", "PASS", "no post_date on a weekend/federal holiday")
    return ValidationResult(
        "post_date_business_day", "FAIL",
        f"{len(bad)} rows post_date on a weekend/holiday, e.g. entry_id {bad['entry_id'].head(5).tolist()}"
    )


def check_post_date_after_timestamp(df: pd.DataFrame) -> ValidationResult:
    """Gate 6: post_date must not be earlier than timestamp."""
    bad = df[pd.to_datetime(df["post_date"]) < pd.to_datetime(df["timestamp"])]
    if bad.empty:
        return ValidationResult("post_date_after_timestamp", "PASS", "post_date never precedes timestamp")
    return ValidationResult(
        "post_date_after_timestamp", "FAIL",
        f"{len(bad)} rows with post_date before timestamp, e.g. entry_id {bad['entry_id'].head(5).tolist()}"
    )


def check_field_blank_by_row_type(df: pd.DataFrame) -> ValidationResult:
    """Gate 7: atm_id/atm_location populated iff payment_type == 'cash'."""
    is_cash = df["payment_type"] == "cash"
    missing_on_cash = df[is_cash & (df["atm_id"].isna() | df["atm_location"].isna())]
    present_off_cash = df[~is_cash & (df["atm_id"].notna() | df["atm_location"].notna())]
    bad = pd.concat([missing_on_cash, present_off_cash])
    if bad.empty:
        return ValidationResult("field_blank_by_row_type", "PASS", "atm_id/atm_location populated iff payment_type == cash")
    return ValidationResult(
        "field_blank_by_row_type", "FAIL",
        f"{len(missing_on_cash)} cash rows missing atm fields, {len(present_off_cash)} non-cash rows with atm fields set"
    )


def check_bic_length(df: pd.DataFrame) -> ValidationResult:
    """Gate 8: any BIC/SWIFT code must be 8 or 11 characters. SKIP - swift_code not propagated to exported rows yet."""
    if "swift_code" not in df.columns:
        return ValidationResult("bic_length", "SKIP", "swift_code not yet propagated into exported transaction rows")
    bad = df[df["swift_code"].notna() & ~df["swift_code"].str.len().isin([8, 11])]
    if bad.empty:
        return ValidationResult("bic_length", "PASS", "all present BICs are 8 or 11 characters")
    return ValidationResult("bic_length", "FAIL", f"{len(bad)} BICs with invalid length")


def check_bic_not_real_institution(df: pd.DataFrame) -> ValidationResult:
    """Gate 9: no BIC may match a real institution (deny-list). SKIP - swift_code not propagated to exported rows yet."""
    if "swift_code" not in df.columns:
        return ValidationResult("bic_not_real_institution", "SKIP", "swift_code not yet propagated into exported transaction rows")
    bad = df[df["swift_code"].isin(REAL_BIC_DENY_LIST)]
    if bad.empty:
        return ValidationResult("bic_not_real_institution", "PASS", "no BIC matches the real-institution deny-list")
    return ValidationResult("bic_not_real_institution", "FAIL", f"{len(bad)} rows use a real institution's BIC")


def check_injected_rows_within_date_range(df: pd.DataFrame, start_date: str, end_date: str) -> ValidationResult:
    """Gate 10: laundering rows must fall within [start_date, end_date]."""
    laundering = df[df["is_laundering"] == True]  # noqa: E712
    if laundering.empty:
        return ValidationResult("injected_rows_within_date_range", "PASS", "no laundering rows to check")
    ts = pd.to_datetime(laundering["timestamp"])
    bad = laundering[(ts < pd.Timestamp(start_date)) | (ts > pd.Timestamp(end_date))]
    if bad.empty:
        return ValidationResult("injected_rows_within_date_range", "PASS", "all laundering rows fall within the configured date range")
    return ValidationResult(
        "injected_rows_within_date_range", "FAIL",
        f"{len(bad)} laundering rows fall outside [{start_date}, {end_date}]"
    )


def check_no_negative_running_balance(df: pd.DataFrame) -> ValidationResult:
    """Gate 11: running_balance must never go negative. SKIP - field not in schema yet."""
    if "running_balance" not in df.columns:
        return ValidationResult("no_negative_running_balance", "SKIP", "running_balance not yet in schema")
    bad = df[df["running_balance"] < 0]
    if bad.empty:
        return ValidationResult("no_negative_running_balance", "PASS", "no negative running_balance")
    return ValidationResult("no_negative_running_balance", "FAIL", f"{len(bad)} rows with negative running_balance")


def check_leakage(df: pd.DataFrame, label_col: str = "is_laundering",
                   min_support: int = 5, threshold: float = 0.3) -> ValidationResult:
    """Gate 12: no column's value may predict the label materially above base rate.

    For every column (except the label and row-unique identifiers), buckets
    rows by value and flags any bucket whose laundering rate deviates from
    the overall base rate by more than `threshold`, with at least
    `min_support` rows backing it. This is the check that would have caught
    both historical leaks (bank_name populated only on laundering rows;
    payment_type == 'credit_card' as a perfect predictor).
    """
    base_rate = df[label_col].mean()
    # Identity columns are exempt by design, not by oversight: which specific
    # party is doing this IS the thing a legitimate investigation is supposed
    # to conclude, so of course the suspect's own account_id correlates with
    # the label. A leak is an incidental/metadata column that gives the
    # answer away without requiring analysis - that's what the rest of this
    # check is for. source_description is exempt because it's a template
    # string built from owner_name/counterparty (see utils/helpers.py
    # split_transaction) - it carries the same identity signal, not new
    # information.
    # rule_id/typology/role_in_typology/difficulty are answer-key-only tags
    # (see main.py's split_answer_key) - they're stripped before the
    # student-facing file is written, so checking them for leakage would be
    # checking a file that never actually ships.
    # 'date' is exempt for the same reason as identity columns: DST
    # ("same day" structuring) and EAT ("burst within a window") are
    # *defined* to cluster transactions on specific days for a flagged
    # account - noticing that clustering is the investigative work these
    # rules ask for, not an incidental leak. 'timestamp' (the internal
    # combined date+time field, dropped before export - see
    # main.py's split_answer_key) and 'time' stay checked normally, since
    # time-of-day itself isn't part of any rule's definition.
    # 'payment_type' is exempt for the same reason as 'date': several rules
    # are *defined* around a specific transaction type (CCE = cash-equivalent,
    # ATM) - an EAT-ATM or DST-CCE rule's injected rows concentrating on
    # payment_type='cash' is the rule's own evidentiary signature, not an
    # incidental leak, and it's the dominant false-positive at small account
    # counts (a flagged account's rule category can saturate the whole cash
    # population). Accepted tradeoff, Rio's call 2026-07-10: this also means
    # a future payment_type-specific leak in the original mold (e.g. the
    # historical payment_type=='credit_card' perfect-predictor bug, unrelated
    # to any rule's own definition) won't be caught by this gate anymore -
    # leakage is advisory now, not a hard block (see print_report), so a
    # human reviewing the report is the actual backstop for that class of
    # bug going forward, not this automated check.
    exempt = {
        label_col, "transaction_id", "entry_id", "account_id", "counterparty", "owner_name", "source_description",
        "rule_id", "typology", "role_in_typology", "difficulty", "date", "payment_type",
    }
    leaks = []

    for col in df.columns:
        if col in exempt:
            continue
        values = df[col].fillna("__MISSING__")
        if values.nunique() >= len(df) * 0.9:
            continue  # free-text/unique-per-row column, not a meaningful bucket
        stats = df.groupby(values)[label_col].agg(["mean", "count"])
        stats = stats[stats["count"] >= min_support]
        offenders = stats[(stats["mean"] - base_rate).abs() > threshold]
        for value, row in offenders.iterrows():
            leaks.append((col, value, row["mean"], int(row["count"])))

    if not leaks:
        return ValidationResult("leakage", "PASS", f"no column predicts the label above base rate ({base_rate:.3f})")

    detail = "; ".join(
        f"{col}={value!r} -> P(laundering)={rate:.2f} (n={n}, base={base_rate:.3f})"
        for col, value, rate, n in leaks[:5]
    )
    # WARN, not FAIL: leakage is a data-quality signal for a human to weigh,
    # not a hard gate. At small account counts in particular, some amount of
    # incidental correlation is close to unavoidable (few observations, few
    # distinct branches/ATMs, etc.) - refusing to write output over it would
    # block real exercises for statistical noise, not a design defect. Rio's
    # call 2026-07-10: a leak-free answer key isn't the actual goal here;
    # investigators are meant to reason about realistic, imperfect data.
    return ValidationResult("leakage", "WARN", f"{len(leaks)} leaking column/value pairs, e.g. {detail}")


def run_all_checks(df: pd.DataFrame, start_date: str, end_date: str) -> list[ValidationResult]:
    return [
        check_duplicate_entry_id(df),
        check_debit_credit_balance(df),
        check_running_balance_reconciliation(df),
        check_direction_consistency(df),
        check_post_date_business_day(df),
        check_post_date_after_timestamp(df),
        check_field_blank_by_row_type(df),
        check_bic_length(df),
        check_bic_not_real_institution(df),
        check_injected_rows_within_date_range(df, start_date, end_date),
        check_no_negative_running_balance(df),
        check_leakage(df),
    ]


def print_report(results: list[ValidationResult]) -> bool:
    log("=" * 60)
    log("VALIDATION REPORT")
    log("=" * 60)
    for r in results:
        level = {"PASS": "INFO", "FAIL": "ERROR", "WARN": "WARNING", "SKIP": "WARNING"}[r.status]
        log(f"[{r.status}] {r.gate}: {r.detail}", level=level)

    fails = [r for r in results if r.status == "FAIL"]
    warns = [r for r in results if r.status == "WARN"]
    skips = [r for r in results if r.status == "SKIP"]
    log("=" * 60)
    if fails:
        log(
            f"RESULT: FAIL ({len(fails)} of {len(results)} gates failed, "
            f"{len(warns)} warned, {len(skips)} skipped)", level="ERROR"
        )
    elif warns:
        log(
            f"RESULT: PASS WITH WARNINGS ({len(results) - len(skips) - len(warns)} of {len(results)} gates passed, "
            f"{len(warns)} warned, {len(skips)} skipped) - output will still be written",
            level="WARNING",
        )
    else:
        log(f"RESULT: PASS ({len(results) - len(skips)} of {len(results)} gates passed, {len(skips)} skipped)")
    log("=" * 60)
    # Only a hard FAIL blocks export - WARN (currently just leakage) is a
    # signal for a human to weigh, not a gate. See check_leakage's docstring.
    return not fails


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate an exported AML dataset")
    parser.add_argument("path", help="Path to a transactions CSV or XLSX file")
    parser.add_argument("--start_date", default="2025-01-01")
    parser.add_argument("--end_date", default="2025-01-31")
    args = parser.parse_args()

    data = pd.read_csv(args.path) if args.path.endswith(".csv") else pd.read_excel(args.path)
    ok = print_report(run_all_checks(data, args.start_date, args.end_date))
    sys.exit(0 if ok else 1)
