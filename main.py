import argparse
import random
import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yaml
from faker import Faker

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from generator.transactions import generate_profile_transactions, ProfileAccount, get_company_owners, get_person_segment
from generator.exporter import export_to_csv_by_bank, export_to_excel_by_bank
from generator.rule_taxonomy import parse_rule_id
from generator.rule_injectors import inject_dst, inject_eat, inject_cty, inject_mbd, inject_est, inject_eop, inject_ftf
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
                       known_accounts_set, bents_by_bank, high_risk_countries_cache,
                       accounts_by_party_id=None):
    """Call the injector matching parsed.subtype for one account/rule_id.

    high_risk_countries_cache is a single-item list used as a mutable box so
    the CTY country list is loaded at most once per caller, across many calls.
    Returns the injected rows, or None if no injector exists for this subtype.

    accounts_by_party_id (party-group rules only - parsed.scope == "P"):
    party_id -> list of sampled ProfileAccount objects (see main.py's
    pattern_accounts loop). The flagged account's own party members
    (excluding itself) are looked up and passed to the injector so a
    burst can spread across a person and their linked employer/owned
    company, not just one account acting alone.
    """
    if parsed.subtype in ("DST", "EAT", "MBD"):
        bank = str(getattr(account, "bank", "")) or None
        pool = bents_by_bank.get(bank) or [b for branches in bents_by_bank.values() for b in branches]

    party_accounts = None
    if parsed.scope == "P":
        party_id = getattr(account, "party_id", None)
        party_accounts = [
            a for a in (accounts_by_party_id or {}).get(party_id, [])
            if getattr(a, "id", None) != getattr(account, "id", None)
        ]

    if parsed.subtype == "DST":
        return inject_dst(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, direction=parsed.direction,
                           bents_by_bank=bents_by_bank, bank=bank, segment=getattr(account, "segment", None))
    elif parsed.subtype == "EAT":
        return inject_eat(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, tran_type=parsed.tran_type, direction=parsed.direction,
                           bents_by_bank=bents_by_bank, bank=bank, segment=getattr(account, "segment", None),
                           party_accounts=party_accounts)
    elif parsed.subtype == "CTY":
        if not high_risk_countries_cache:
            with open(rule_def["high_risk_country_list"], "r") as f:
                high_risk_countries_cache.append(yaml.safe_load(f)["countries"])
        return inject_cty(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, high_risk_countries_cache[0],
                           segment=getattr(account, "segment", None),
                           owner_type=getattr(account, "owner_type", None),
                           transaction_scaler=getattr(account, "transaction_scaler", None))
    elif parsed.subtype == "EST":
        return inject_est(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, direction=parsed.direction,
                           segment=getattr(account, "segment", None),
                           owner_type=getattr(account, "owner_type", None),
                           transaction_scaler=getattr(account, "transaction_scaler", None))
    elif parsed.subtype == "MBD":
        return inject_mbd(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, pool)
    elif parsed.subtype == "EOP":
        return inject_eop(account, rule_id, rule_def, window_start, window_end,
                           known_accounts_set, category=parsed.category,
                           party_accounts=party_accounts)
    elif parsed.subtype == "FTR":
        return inject_ftf(account, rule_id, rule_def, window_start, window_end, known_accounts_set)
    else:
        log(f"⚠️ No injector implemented for subtype {parsed.subtype} ({rule_id}), skipping", level="WARNING")
        return None


def run_rule_injections(rule_config_path, thresholds_path, pattern_accounts,
                         bents_by_bank, known_accounts_set, start_date, end_date,
                         accounts_by_party_id=None):
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
                                      known_accounts_set, bents_by_bank, high_risk_countries_cache,
                                      accounts_by_party_id)
            if rows is None:
                continue

            laundering_txns.extend(rows)
            log(f"✅ Injected {rule_id} ({len(rows)} rows) on account {getattr(account, 'id', '?')}")

    return laundering_txns


def _pick_shared_window(window_start, window_end, days=30):
    """Pick one shared sub-window of ``days`` length within
    [window_start, window_end], clamped to the exercise's actual range if
    it's shorter than ``days``. Used so a paired alert's two rule firings
    genuinely cluster together in time, the way a real alert period would,
    instead of landing independently anywhere across the whole exercise."""
    span = (window_end - window_start).days
    if span <= days:
        return window_start, window_end
    offset = random.randint(0, span - days)
    pair_start = window_start + timedelta(days=offset)
    return pair_start, pair_start + timedelta(days=days)


def run_rule_injections_by_count(n_flagged, thresholds_path, pattern_accounts,
                                  bents_by_bank, known_accounts_set, start_date, end_date,
                                  accounts_by_party_id=None):
    """Flag n_flagged distinct accounts. Most get exactly one rule; per
    thresholds.yaml's rule_overlaps.pair_probability, some instead get a
    pair of rules from rule_overlaps.pairs, fired within a shared ~30-day
    window so both genuinely cluster into one alert period rather than
    landing independently anywhere in the exercise - real alerts package
    overlapping rule hits on the same account together, they don't treat
    them as unrelated cases. Rio's call, 2026-07-10; see thresholds.yaml's
    rule_overlaps comment for how to iterate on the pair list.

    Rule choice (solo or within a pair) is weighted per-account-type by
    each rule's account_type_weight in thresholds.yaml (DRAFT weights,
    Rio's call to tune).
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

    overlap_cfg = thresholds.get("rule_overlaps", {})
    pair_probability = overlap_cfg.get("pair_probability", 0.0)
    eligible_pairs = [
        pair for pair in overlap_cfg.get("pairs", [])
        if all(rid in built_rules for rid in pair)
    ]

    high_risk_countries_cache = []
    window_start = datetime.strptime(start_date, "%Y-%m-%d")
    window_end = datetime.strptime(end_date, "%Y-%m-%d")

    flagged_accounts = random.sample(pattern_accounts, n_flagged)

    def pick_rule_id(account_type):
        rule_ids = list(built_rules.keys())
        weights = [
            built_rules[rid].get("account_type_weight", {}).get(account_type, 1.0)
            for rid in rule_ids
        ]
        return random.choices(rule_ids, weights=weights, k=1)[0]

    laundering_txns = []
    for account in flagged_accounts:
        account_type = str(getattr(account, "owner_type", "")).lower()

        if eligible_pairs and random.random() < pair_probability:
            rule_id_set = list(random.choice(eligible_pairs))
            win_start, win_end = _pick_shared_window(window_start, window_end)
        else:
            rule_id_set = [pick_rule_id(account_type)]
            win_start, win_end = window_start, window_end

        fired = []
        for rule_id in rule_id_set:
            rule_def = built_rules[rule_id]
            parsed = parse_rule_id(rule_id)
            rows = dispatch_injector(parsed, account, rule_id, rule_def, win_start, win_end,
                                      known_accounts_set, bents_by_bank, high_risk_countries_cache,
                                      accounts_by_party_id)
            if rows is None:
                continue
            laundering_txns.extend(rows)
            fired.append((rule_id, len(rows)))

        if not fired:
            continue
        if len(fired) > 1:
            summary = " + ".join(f"{rid} ({n} rows)" for rid, n in fired)
            log(f"✅ Flagged account {getattr(account, 'id', '?')} with {summary} (paired alert)")
        else:
            rule_id, n_rows = fired[0]
            log(f"✅ Flagged account {getattr(account, 'id', '?')} with {rule_id} ({n_rows} rows)")

    return laundering_txns


def close_relational_sample(sampled_persons, sampled_companies, persons_pool, companies_pool):
    """Fixed-point closure over the initial random sample: pull in any
    employer or owned company a sampled person is connected to, and any
    owner a sampled company is connected to, repeating until nothing new
    is added. --n_persons/--n_companies are a floor, not an exact count -
    relational completeness (an employed person's employer always being
    present, an owner's company always being present) wins over matching
    the requested headcount exactly. Rio's call, 2026-07-10 - this is the
    fix for employed persons showing up with no income at all when their
    employer wasn't part of the random --n_companies draw.

    Returns (sampled_persons, sampled_companies), each possibly grown.
    """
    person_ids = set(sampled_persons["entity_id"])
    company_ids = set(sampled_companies["entity_id"])
    has_owners_col = "owners" in companies_pool.columns

    while True:
        needed_companies = set(sampled_persons["employer"].dropna()) - company_ids
        needed_persons = set()

        if has_owners_col:
            # Owned-company closure: does any sampled person own a company
            # not yet in the sample?
            for _, comp in companies_pool.iterrows():
                if comp["entity_id"] in company_ids:
                    continue
                owner_ids = {pid for pid, _ in get_company_owners(comp.get("owners"))}
                if person_ids & owner_ids:
                    needed_companies.add(comp["entity_id"])

            # Owner closure: does any sampled company have an owner not
            # yet in the sample?
            owned_by_sampled_companies = sampled_companies["owners"] if "owners" in sampled_companies.columns else []
            for owners_str in owned_by_sampled_companies:
                needed_persons.update(pid for pid, _ in get_company_owners(owners_str))
            needed_persons -= person_ids

        if not needed_companies and not needed_persons:
            break

        if needed_companies:
            new_companies = companies_pool[companies_pool["entity_id"].isin(needed_companies)]
            sampled_companies = pd.concat([sampled_companies, new_companies], ignore_index=True)
            company_ids |= needed_companies
        if needed_persons:
            new_persons = persons_pool[persons_pool["entity_id"].isin(needed_persons)]
            sampled_persons = pd.concat([sampled_persons, new_persons], ignore_index=True)
            person_ids |= needed_persons

    return sampled_persons, sampled_companies


def split_answer_key(all_txns):
    """Strip answer-key-only fields from each row, returning (student_rows, answer_key_rows).

    'timestamp' is dropped from the student file too - it's the combined
    date+time string used internally for date-math (see validate.py); the
    exported file carries 'date'/'time' as separate columns instead.

    A row with a populated rule_id actually contributed to a rule firing -
    it gets the full transaction record in the answer key (amount, date,
    parties, everything), not just the label fields, so the grading file
    is self-contained and doesn't require a join back to the transaction
    files to see what the flagged transaction actually was. Rows with no
    rule_id keep the existing thin format - there's nothing to show beyond
    is_laundering=False. Rio's call, 2026-07-10.
    """
    answer_key_rows = []
    student_rows = []
    excluded_from_student = set(ANSWER_KEY_FIELDS) | {"timestamp"}
    for row in all_txns:
        if row.get("rule_id"):
            answer_key_rows.append({k: v for k, v in row.items() if k != "timestamp"})
        else:
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

    # --n_persons/--n_companies are a floor: pull in any employer/owned
    # company a sampled person is connected to, and any owner a sampled
    # company is connected to, so an employed or owning person never ends
    # up with a relationship dangling outside the sample (see
    # close_relational_sample's docstring - this is the fix for employed
    # persons showing up with no income at all).
    sampled_persons, sampled_companies = close_relational_sample(
        sampled_persons, sampled_companies, persons_pool, companies_pool,
    )

    profile_df = pd.concat([non_customer_rows, sampled_persons, sampled_companies], ignore_index=True)
    if len(sampled_persons) > args.n_persons or len(sampled_companies) > args.n_companies:
        log(f"🎯 Sampled {args.n_persons}/{len(persons_pool)} persons and {args.n_companies}/{len(companies_pool)} "
            f"companies requested - grew to {len(sampled_persons)} persons and {len(sampled_companies)} companies "
            f"after pulling in employment/ownership relationships")
    else:
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
        # P1-P6 for persons (used by inject_dst/inject_eat's ATM rounding
        # and daily-cap logic, same as legitimate cash withdrawals) - None
        # for companies, get_person_segment already returns None if either
        # field is missing.
        account.segment = get_person_segment(row.get("income_level"), row.get("employment_status"))
        # Size tier for company wire amounts (inject_cty/inject_est - see
        # get_company_size_tier, generator/transactions.py); None for
        # persons, same as .segment is None for companies above.
        account.transaction_scaler = row.get("transaction_scaler")
        # Customer-relationship party grouping (scripts/add_party_id.py) -
        # used by the party-group Alert Matrix rules (EOP-P, EAT-ATM-P) to
        # spread a burst across a person and their linked employer/owned
        # company rather than one account acting alone.
        account.party_id = row.get("party_id")

    accounts_by_party_id: dict = {}
    for account in pattern_accounts:
        accounts_by_party_id.setdefault(account.party_id, []).append(account)

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
            accounts_by_party_id,
        )
        log(f"✅ Laundering transactions generated (rule-based): {len(laundering_txns)}")
    elif args.n_flagged:
        log(f"🚩 Flagging {args.n_flagged} of {len(pattern_accounts)} sampled accounts with injected activity")
        bents_by_bank = build_bents_by_bank(profile_df)
        laundering_txns = run_rule_injections_by_count(
            args.n_flagged, args.thresholds, pattern_accounts,
            bents_by_bank, known_accounts_set, args.start_date, args.end_date,
            accounts_by_party_id,
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

    # in_package: "y" if this row's account_id is one of the sampled
    # persons/companies (customer_rows - the same roster customer_info is
    # built from), "n" for everything else (merchants, suppliers,
    # synthesized landlords/utility companies, any other external
    # counterparty). Deliberately independent of is_laundering - a real
    # alert drags in related accounts/parties for review whether or not
    # they personally have flagged transactions in the answer key.
    # Rio's call, 2026-07-10.
    package_accounts = set(customer_rows["account_number"].dropna().astype(str))
    for row in all_txns:
        row["in_package"] = "y" if str(row.get("account_id")) in package_accounts else "n"

    # account_id -> sorted distinct rule_ids that fired on it, for
    # customer_info's alert_rule column below. Standard in real AML alert
    # systems - lets a student search for/recognize a specific typology in
    # the data rather than only discovering it by re-deriving the answer
    # key. Rio's call, 2026-07-10. Reads straight off laundering_txns, so
    # an account with more than one rule (see run_rule_injections_by_count's
    # paired-alert path) naturally shows both here with no extra plumbing.
    account_alert_rules: dict[str, list[str]] = {}
    for row in laundering_txns:
        rule_id = row.get("rule_id")
        if not rule_id:
            continue
        rules = account_alert_rules.setdefault(str(row["account_id"]), [])
        if rule_id not in rules:
            rules.append(rule_id)

    # Partition by bank: a real investigator only ever sees their own
    # bank's core-system extract - both legs of a transaction when both
    # parties bank there, just their own leg when the counterparty is
    # external. account_to_bank covers every account that can appear here
    # (sampled customers + the full merchant/BEnt pool, all still present
    # in profile_df after sampling).
    #
    # Resolved on all_txns, before validation/split_answer_key (not after,
    # as this used to run) - swift_code/bank_name need to be correct
    # *before* run_all_checks sees the data (otherwise bic_length/
    # bic_not_real_institution can never see a populated swift_code column
    # at all, since validation would already be done by the time it was
    # set), and mutating all_txns's own row dicts in place means both
    # student_rows and answer_key_rows inherit the resolved values
    # naturally from split_answer_key's dict comprehensions, instead of
    # only student_rows getting the fix as before. 2026-07-15.
    bank_code_to_name = dict(
        zip(profile_df.loc[profile_df["type"] == "bank", "bank"],
            profile_df.loc[profile_df["type"] == "bank", "name"])
    )
    # swift_code (wire transactions only) - this row's own account's
    # bank's BIC, not something split_transaction can know at generation
    # time (see travel_rule_info's docstring in utils/helpers.py for why).
    bank_code_to_swift = dict(
        zip(profile_df.loc[profile_df["type"] == "bank", "bank"],
            profile_df.loc[profile_df["type"] == "bank", "swift_code"])
    )
    has_account_number = profile_df["account_number"].notna()
    account_to_bank_code = dict(
        zip(
            profile_df.loc[has_account_number, "account_number"].astype(str),
            profile_df.loc[has_account_number, "bank"],
        )
    )
    for row in all_txns:
        bank_code = account_to_bank_code.get(str(row["account_id"]))
        # Fall back to whatever split_transaction already computed
        # (row["bank_name"]) before defaulting to "Unknown Bank" - covers
        # accounts that exist only as a synthetic ledger entity, not a real
        # profile_df row, e.g. the c_check GL suspense account
        # (generator/transactions.py::build_bank_gl_accounts), which still
        # has a correct bank_name baked in from its own construction.
        bank = bank_code_to_name.get(bank_code) or row.get("bank_name") or "Unknown Bank"
        row["bank_name"] = bank
        if row.get("payment_type") == "wire":
            row["swift_code"] = bank_code_to_swift.get(bank_code)

    validation_df = pd.DataFrame(all_txns)
    person_account_ids = frozenset(
        profile_df.loc[profile_df["type"] == "person", "account_number"].dropna().astype(str)
    )
    results = run_all_checks(validation_df, args.start_date, args.end_date, person_account_ids)
    if not print_report(results):
        log("❌ Validation failed - no output written.", level="ERROR")
        sys.exit(1)

    student_rows, answer_key_rows = split_answer_key(all_txns)

    rows_by_bank: dict[str, list] = {}
    for row in student_rows:
        rows_by_bank.setdefault(row["bank_name"], []).append(row)

    log(f"💾 Exporting {len(student_rows)} transactions across {len(rows_by_bank)} banks to {args.output}")
    if args.format == "csv":
        export_to_csv_by_bank(rows_by_bank, args.output)
    else:
        export_to_excel_by_bank(rows_by_bank, args.output)

    # Customer information: static identity facts (not transactions) for
    # the sampled accounts under review - name, address, and type_of_business:
    # a company's industry (naics_description) or a person's own occupation
    # (scripts/assign_occupations.py) - same column, content depends on row
    # type, not blank for persons anymore. Scoped to customer_rows (the
    # sampled persons/companies), not merchants/BEnts - those are
    # counterparties, not this bank's own customers. Partitioned by bank
    # for the same reason the transaction files are. alert_rule is blank
    # for the (large majority of) accounts with no injected activity -
    # that's correct, not a bug, same as is_laundering/in_package.
    customer_info_by_bank: dict[str, list] = {}
    for _, row in customer_rows.iterrows():
        bank = bank_code_to_name.get(row.get("bank"), "Unknown Bank")
        type_of_business = row.get("naics_description") if row.get("type") == "company" else row.get("occupation")
        customer_info_by_bank.setdefault(bank, []).append({
            "customer_id": row.get("customer_id"),
            "account_number": row.get("account_number"),
            "type": row.get("type"),
            "name": row.get("name"),
            "address": row.get("address"),
            "type_of_business": type_of_business,
            "bank_name": bank,
            "alert_rule": ", ".join(account_alert_rules.get(str(row.get("account_number")), [])),
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
