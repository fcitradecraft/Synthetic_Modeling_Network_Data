import argparse
import random
import sys
import os
from datetime import datetime

import numpy as np
import pandas as pd
import yaml
from faker import Faker

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from generator.transactions import generate_profile_transactions, ProfileAccount
from generator.exporter import export_to_csv_by_bank, export_to_excel_by_bank
from generator.rule_taxonomy import parse_rule_id
from generator.rule_injectors import inject_dst, inject_eat, inject_cty, inject_mbd
from utils.logger import log
from validate import run_all_checks, print_report

ANSWER_KEY_FIELDS = ["is_laundering", "rule_id", "typology", "role_in_typology", "difficulty"]


def build_bents_by_bank(profile_df: pd.DataFrame) -> dict:
    bent_df = profile_df[profile_df["type"] == "BEnt"]
    return {
        str(bank): g[["name", "address"]].to_dict("records")
        for bank, g in bent_df.groupby("bank")
    }


def dispatch_injector(parsed, account, rule_id, rule_def, window_start, window_end,
                       known_accounts_set, bents_by_bank, high_risk_countries_cache):
    """Call the injector matching parsed.subtype for one account/rule_id.

    high_risk_countries_cache is a single-item list used as a mutable box so
    the CTY country list is loaded at most once per caller, across many calls.
    Returns the injected rows, or None if no injector exists for this subtype.
    """
    if parsed.subtype in ("DST", "EAT", "MBD"):
        bank = str(getattr(account, "bank", "")) or None
        pool = bents_by_bank.get(bank) or [b for branches in bents_by_bank.values() for b in branches]

    if parsed.subtype == "DST":
        return inject_dst(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, direction=parsed.direction, bent_pool=pool)
    elif parsed.subtype == "EAT":
        return inject_eat(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, tran_type=parsed.tran_type, direction=parsed.direction, bent_pool=pool)
    elif parsed.subtype == "CTY":
        if not high_risk_countries_cache:
            with open(rule_def["high_risk_country_list"], "r") as f:
                high_risk_countries_cache.append(yaml.safe_load(f)["countries"])
        return inject_cty(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, high_risk_countries_cache[0])
    elif parsed.subtype == "MBD":
        return inject_mbd(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, pool)
    else:
        log(f"⚠️ No injector implemented for subtype {parsed.subtype} ({rule_id}), skipping", level="WARNING")
        return None


def run_rule_injections(rule_config_path, thresholds_path, pattern_accounts,
                         bents_by_bank, known_accounts_set, start_date, end_date):
    """Power-user path: exact rule_id + count mix from a rule_config YAML."""
    with open(thresholds_path, "r") as f:
        thresholds = yaml.safe_load(f)
    with open(rule_config_path, "r") as f:
        rule_config = yaml.safe_load(f)

    high_risk_countries_cache = []  # loaded lazily, only if a CTY instance is requested
    window_start = datetime.strptime(start_date, "%Y-%m-%d")
    window_end = datetime.strptime(end_date, "%Y-%m-%d")

    laundering_txns = []
    for entry in rule_config.get("instances", []):
        rule_id = entry["rule_id"]
        count = entry.get("count", 1)
        rule_def = thresholds["rules"].get(rule_id)
        if rule_def is None:
            log(f"⚠️ No thresholds.yaml entry for {rule_id}, skipping", level="WARNING")
            continue
        if not rule_def.get("built", False):
            log(f"⚠️ {rule_id} is not built yet (thresholds.yaml built: false), skipping", level="WARNING")
            continue

        parsed = parse_rule_id(rule_id)

        for _ in range(count):
            account = random.choice(pattern_accounts)
            rows = dispatch_injector(parsed, account, rule_id, rule_def, window_start, window_end,
                                      known_accounts_set, bents_by_bank, high_risk_countries_cache)
            if rows is None:
                continue

            laundering_txns.extend(rows)
            log(f"✅ Injected {rule_id} ({len(rows)} rows) on account {getattr(account, 'id', '?')}")

    return laundering_txns


def run_rule_injections_by_count(n_flagged, thresholds_path, pattern_accounts,
                                  bents_by_bank, known_accounts_set, start_date, end_date):
    """Simple path: flag n_flagged distinct accounts, one rule each.

    Rule choice is weighted per-account-type by each rule's
    account_type_weight in thresholds.yaml (DRAFT weights, Rio's call to
    tune). n_flagged accounts are sampled without replacement, so every
    flagged account gets exactly one rule_id - never zero, never several.
    """
    if n_flagged > len(pattern_accounts):
        log(f"❌ --n_flagged {n_flagged} exceeds the sampled account pool ({len(pattern_accounts)} accounts). "
            f"Lower --n_flagged or raise --n_persons/--n_companies.", level="ERROR")
        sys.exit(1)

    with open(thresholds_path, "r") as f:
        thresholds = yaml.safe_load(f)
    built_rules = {rid: rdef for rid, rdef in thresholds["rules"].items() if rdef.get("built", False)}
    if not built_rules:
        log("❌ No built: true rules in thresholds.yaml - nothing to inject.", level="ERROR")
        sys.exit(1)

    high_risk_countries_cache = []
    window_start = datetime.strptime(start_date, "%Y-%m-%d")
    window_end = datetime.strptime(end_date, "%Y-%m-%d")

    flagged_accounts = random.sample(pattern_accounts, n_flagged)

    laundering_txns = []
    for account in flagged_accounts:
        account_type = str(getattr(account, "owner_type", "")).lower()
        rule_ids = list(built_rules.keys())
        weights = [
            built_rules[rid].get("account_type_weight", {}).get(account_type, 1.0)
            for rid in rule_ids
        ]
        rule_id = random.choices(rule_ids, weights=weights, k=1)[0]
        rule_def = built_rules[rule_id]
        parsed = parse_rule_id(rule_id)

        rows = dispatch_injector(parsed, account, rule_id, rule_def, window_start, window_end,
                                  known_accounts_set, bents_by_bank, high_risk_countries_cache)
        if rows is None:
            continue

        laundering_txns.extend(rows)
        log(f"✅ Flagged account {getattr(account, 'id', '?')} with {rule_id} ({len(rows)} rows)")

    return laundering_txns


def split_answer_key(all_txns):
    """Strip answer-key-only fields from each row, returning (student_rows, answer_key_rows).

    'timestamp' is dropped from the student file too - it's the combined
    date+time string used internally for date-math (see validate.py); the
    exported file carries 'date'/'time' as separate columns instead.
    """
    answer_key_rows = []
    student_rows = []
    excluded_from_student = set(ANSWER_KEY_FIELDS) | {"timestamp"}
    for row in all_txns:
        answer_key_rows.append({
            "entry_id": row["entry_id"],
            **{f: row.get(f) for f in ANSWER_KEY_FIELDS},
        })
        student_rows.append({k: v for k, v in row.items() if k not in excluded_from_student})
    return student_rows, answer_key_rows


def main():
    parser = argparse.ArgumentParser(description="Synthetic AML Dataset Generator")
    parser.add_argument("--legit_txns", type=int, default=None,
                         help="Optional safety-valve cap on base transaction count; leave unset for realistic "
                              "volume driven by --n_persons/--n_companies and the date range")
    parser.add_argument("--n_flagged", type=int, default=0,
                         help="Number of sampled accounts to flag with injected suspicious activity "
                              "(0 = clean output only). Mutually exclusive with --rule_config.")
    parser.add_argument("--rule_config", type=str, default=None,
                         help="Power-user override: path to a rule-trigger YAML (rule_id + exact instance counts), "
                              "dispatched via thresholds.yaml. Mutually exclusive with --n_flagged.")
    parser.add_argument("--thresholds", type=str, default="thresholds.yaml", help="Path to thresholds.yaml")
    parser.add_argument("--agent_profiles", type=str, default="agents/agent_profiles.xlsx",
                         help="Path to agent profiles Excel file (the sole identity source)")
    parser.add_argument("--output", type=str, default="data/aml_dataset.csv", help="Output file path")
    parser.add_argument("--customer_info", type=str, default="data/customer_info.csv",
                         help="Customer information output path (name/address/type of business for sampled accounts)")
    parser.add_argument("--answer_key", type=str, default="data/answer_key.csv", help="Answer key output path")
    parser.add_argument("--combined_output", type=str, default="data/aml_dataset_combined.xlsx",
                         help="Combined workbook: every bank's transactions and customer info as separate sheets "
                              "in one file (answer key excluded). Always written alongside --output/--customer_info.")
    parser.add_argument("--format", type=str, choices=["csv", "xlsx"], default="csv", help="Export format")
    parser.add_argument("--start_date", type=str, default="2025-01-01", help="Start date for transaction range")
    parser.add_argument("--end_date", type=str, default="2025-01-31", help="End date for transaction range")
    parser.add_argument("--seed", type=int, default=None,
                         help="Random seed - same seed + same args reproduces byte-identical output")
    parser.add_argument("--n_persons", type=int, required=True,
                         help="Number of person accounts to sample from Combined_Data for this run")
    parser.add_argument("--n_companies", type=int, required=True,
                         help="Number of company accounts to sample from Combined_Data for this run")

    args = parser.parse_args()

    if args.rule_config and args.n_flagged:
        log("❌ --rule_config and --n_flagged are mutually exclusive - pick exact rule-mix control "
            "(--rule_config) or simple flagged-account-count control (--n_flagged), not both.", level="ERROR")
        sys.exit(1)

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        Faker.seed(args.seed)
        log(f"🌱 Seeded RNG with --seed {args.seed}")

    log(f"📂 Loading agent profiles from {args.agent_profiles}")
    profile_df = pd.read_excel(args.agent_profiles, sheet_name="Combined_Data")

    persons_pool = profile_df[profile_df["type"] == "person"]
    companies_pool = profile_df[profile_df["type"] == "company"]
    non_customer_rows = profile_df[profile_df["type"].isin(["merchant", "BEnt", "bank"])]

    if args.n_persons > len(persons_pool):
        log(f"❌ --n_persons {args.n_persons} exceeds the available pool ({len(persons_pool)} person rows "
            f"in Combined_Data). Add more person rows or lower --n_persons.", level="ERROR")
        sys.exit(1)
    if args.n_companies > len(companies_pool):
        log(f"❌ --n_companies {args.n_companies} exceeds the available pool ({len(companies_pool)} company rows "
            f"in Combined_Data). Add more company rows or lower --n_companies.", level="ERROR")
        sys.exit(1)

    sampled_persons = persons_pool.sample(n=args.n_persons)
    sampled_companies = companies_pool.sample(n=args.n_companies)
    profile_df = pd.concat([non_customer_rows, sampled_persons, sampled_companies], ignore_index=True)
    log(f"🎯 Sampled {args.n_persons}/{len(persons_pool)} persons and "
        f"{args.n_companies}/{len(companies_pool)} companies for this run")

    known_accounts_set = set(profile_df["account_number"].dropna().astype(str))
    log(f"🔍 Known accounts (from agent profiles): {len(known_accounts_set)}")

    # The rule injectors (generator/rule_injectors.py) only ever read
    # .id/.bank off account objects, so a list of ProfileAccount stand-ins
    # is enough to place injected rows against real sampled accounts.
    customer_rows = profile_df[profile_df["type"].isin(["person", "company"])]
    pattern_accounts = [
        ProfileAccount(
            id=row["account_number"] if pd.notna(row["account_number"]) else row["entity_id"],
            owner_id=row["entity_id"],
            owner_type=str(row["type"]).capitalize(),
            owner_name=row.get("name", ""),
            address=row.get("address", ""),
        )
        for _, row in customer_rows.iterrows()
    ]
    for account, (_, row) in zip(pattern_accounts, customer_rows.iterrows()):
        account.bank = row.get("bank")

    legit_txns, base_txn_count = generate_profile_transactions(
        profile_df=profile_df,
        start_date=args.start_date,
        end_date=args.end_date,
        max_transactions=args.legit_txns,
    )
    log(
        f"✅ Profile-based transactions generated: {base_txn_count} base transactions "
        f"({len(legit_txns)} ledger entries)"
    )

    laundering_txns = []

    if args.rule_config:
        log(f"📂 Loading rule-trigger config from {args.rule_config}")
        bents_by_bank = build_bents_by_bank(profile_df)
        laundering_txns = run_rule_injections(
            args.rule_config, args.thresholds, pattern_accounts,
            bents_by_bank, known_accounts_set, args.start_date, args.end_date,
        )
        log(f"✅ Laundering transactions generated (rule-based): {len(laundering_txns)}")
    elif args.n_flagged:
        log(f"🚩 Flagging {args.n_flagged} of {len(pattern_accounts)} sampled accounts with injected activity")
        bents_by_bank = build_bents_by_bank(profile_df)
        laundering_txns = run_rule_injections_by_count(
            args.n_flagged, args.thresholds, pattern_accounts,
            bents_by_bank, known_accounts_set, args.start_date, args.end_date,
        )
        log(f"✅ Laundering transactions generated ({len(laundering_txns)} rows)")
    else:
        log("🧼 --n_flagged 0 and no --rule_config: clean output, no injected activity")

    # is_laundering is exactly what the rule injectors set directly on
    # their own rows - no downstream taint propagation. A flagged account's
    # ordinary, unrelated activity (e.g. routine vendor payments) stays
    # untainted; only the rows the injector actually built for the rule
    # carry the label. Investigators clearing an alert have to reason
    # about a real account's mixed activity, not a dataset with perfect
    # money-trail provenance - see Rio's call, 2026-07-10.
    all_txns = legit_txns + laundering_txns

    validation_df = pd.DataFrame(all_txns)
    results = run_all_checks(validation_df, args.start_date, args.end_date)
    if not print_report(results):
        log("❌ Validation failed - no output written.", level="ERROR")
        sys.exit(1)

    student_rows, answer_key_rows = split_answer_key(all_txns)

    # Partition by bank: a real investigator only ever sees their own
    # bank's core-system extract - both legs of a transaction when both
    # parties bank there, just their own leg when the counterparty is
    # external. account_to_bank covers every account that can appear here
    # (sampled customers + the full merchant/BEnt pool, all still present
    # in profile_df after sampling).
    bank_code_to_name = dict(
        zip(profile_df.loc[profile_df["type"] == "bank", "bank"],
            profile_df.loc[profile_df["type"] == "bank", "name"])
    )
    account_to_bank_code = dict(
        zip(profile_df["account_number"].dropna().astype(str), profile_df["bank"])
    )
    rows_by_bank: dict[str, list] = {}
    for row in student_rows:
        bank_code = account_to_bank_code.get(str(row["account_id"]))
        bank = bank_code_to_name.get(bank_code, "Unknown Bank")
        row["bank_name"] = bank
        rows_by_bank.setdefault(bank, []).append(row)

    log(f"💾 Exporting {len(student_rows)} transactions across {len(rows_by_bank)} banks to {args.output}")
    if args.format == "csv":
        export_to_csv_by_bank(rows_by_bank, args.output)
    else:
        export_to_excel_by_bank(rows_by_bank, args.output)

    # Customer information: static identity facts (not transactions) for
    # the sampled accounts under review - name, address, and, for
    # companies, what kind of business it is (naics_description). Persons
    # aren't a "business", so type_of_business is blank for them. Scoped to
    # customer_rows (the sampled persons/companies), not merchants/BEnts -
    # those are counterparties, not this bank's own customers. Partitioned
    # by bank for the same reason the transaction files are.
    customer_info_by_bank: dict[str, list] = {}
    for _, row in customer_rows.iterrows():
        bank = bank_code_to_name.get(row.get("bank"), "Unknown Bank")
        customer_info_by_bank.setdefault(bank, []).append({
            "customer_id": row.get("customer_id"),
            "account_number": row.get("account_number"),
            "type": row.get("type"),
            "name": row.get("name"),
            "address": row.get("address"),
            "type_of_business": row.get("naics_description") if row.get("type") == "company" else "",
            "bank_name": bank,
        })

    log(f"💾 Exporting customer information for {len(customer_rows)} customers across "
        f"{len(customer_info_by_bank)} banks to {args.customer_info}")
    if args.format == "csv":
        export_to_csv_by_bank(customer_info_by_bank, args.customer_info)
    else:
        export_to_excel_by_bank(customer_info_by_bank, args.customer_info)

    # Combined workbook: every bank's transactions and customer info as
    # separate sheets in one file, skipping the answer key (a grading
    # artifact, not something any bank's own extract would contain).
    # Short suffixes (_Txn/_Cust) leave room under Excel's 31-char sheet
    # name limit before the shared truncation in export_to_excel_by_bank.
    combined_by_sheet: dict[str, list] = {}
    for bank, rows in rows_by_bank.items():
        combined_by_sheet[f"{bank}_Txn"] = rows
    for bank, rows in customer_info_by_bank.items():
        combined_by_sheet[f"{bank}_Cust"] = rows
    export_to_excel_by_bank(combined_by_sheet, args.combined_output)
    log(f"💾 Exported combined workbook ({len(combined_by_sheet)} sheets) to {args.combined_output}")

    pd.DataFrame(answer_key_rows).to_csv(args.answer_key, index=False)
    log(f"💾 Exported answer key to {args.answer_key}")

    log(f"📦 Total transactions to export: {len(student_rows)}")
    log("✅ Done.")

if __name__ == "__main__":
    main()
