import os
import random
from datetime import datetime, timedelta

import yaml

from utils.helpers import (
    generate_uuid,
    generate_transaction_timestamp,
    generate_post_date,
    to_datetime,
    split_transaction,
    describe_transaction,
    fake,
)
from utils.logger import log
import pandas as pd

# Common payment types - matches the 10 tokens actually used in
# Combined_Data's accepted_payment_methods column (ACH, C_Check, Cash,
# Ccard, Check, Credit, Debit, P2P, PoS, Wire, case-insensitive). Card
# sub-types are kept distinct rather than consolidated into one "card"
# type (Rio's call, 2026-07-10) so they can be separately calibrated if
# real behavior diverges later (e.g. credit skewing to larger purchases).
PAYMENT_TYPES = ["wire", "ach", "check", "c_check", "cash", "p2p", "ccard", "credit", "debit", "pos"]

_PAYMENT_RANGES_PATH = os.path.join("config", "payment_ranges.yaml")
with open(_PAYMENT_RANGES_PATH, "r") as _f:
    PAYMENT_RANGES = yaml.safe_load(_f)


def get_person_segment(income_level, employment_status):
    """Map (income_level, employment_status) to a P1-P6 segment via
    config/payment_ranges.yaml's person_segment_matrix. Returns None if
    either field is missing (e.g. a non-person row)."""
    if pd.isna(income_level) or pd.isna(employment_status):
        return None
    return PAYMENT_RANGES["person_segment_matrix"].get(income_level, {}).get(employment_status)


_COMPANY_SEGMENT_LOOKUP = {
    entity_id: seg
    for seg, ids in PAYMENT_RANGES["company_segments"].items()
    for entity_id in ids
}


def get_company_segment(entity_id):
    """Map an entity_id to a C1-C3 segment via config/payment_ranges.yaml's
    company_segments. Returns None for a company not in the mapping (e.g.
    a future roster addition not yet classified)."""
    return _COMPANY_SEGMENT_LOOKUP.get(entity_id)


def pick_payment_type_for_amount(payment_types, amount, segment, segment_ranges):
    """Choose a payment_type from ``payment_types`` whose typical range (for
    ``segment``, looked up in ``segment_ranges``) actually contains
    ``amount`` - the fix for payment_type and amount being picked
    independently (e.g. a $30,000 purchase rolling as P2P). A [0, 0] range
    means "not realistic for this segment" and is always excluded, even if
    the counterparty's accepted_payment_methods lists it.

    Falls back to the accepted type whose range is numerically closest to
    ``amount`` if none contain it outright (e.g. a merchant that only takes
    card payments for a purchase priced above every card range) - this
    still keeps the choice amount-aware rather than reintroducing the
    original bug in the fallback path. Falls back to a uniform random
    choice only if ``segment`` is unmapped or none of the accepted types
    have any range defined at all.
    """
    ranges = (segment_ranges or {}).get(segment) if segment else None
    if not ranges:
        return random.choice(payment_types)

    def bounds(pt):
        r = ranges.get(pt)
        if not r or tuple(r) == (0, 0):
            return None
        return r

    eligible = [pt for pt in payment_types if bounds(pt) and bounds(pt)[0] <= amount <= bounds(pt)[1]]
    if eligible:
        return random.choice(eligible)

    candidates = [(pt, bounds(pt)) for pt in payment_types if bounds(pt)]
    if not candidates:
        return random.choice(payment_types)

    def distance(r):
        lo, hi = r
        if amount < lo:
            return lo - amount
        if amount > hi:
            return amount - hi
        return 0

    candidates.sort(key=lambda c: distance(c[1]))
    return candidates[0][0]


class ProfileAccount:
    """Lightweight account object used for profile-driven transactions."""

    def __init__(self, id, owner_id, owner_type, owner_name="", bank_name="", address=""):
        self.id = str(id)
        self.owner_id = owner_id
        self.owner_type = owner_type
        self.owner_name = owner_name
        self.bank_name = bank_name
        self.address = address


def get_payroll_dates(start_dt: datetime, end_dt: datetime) -> list[datetime]:
    """Return payroll dates (1st and 3rd Monday) within range."""
    dates = []
    current = start_dt.replace(day=1)
    while current <= end_dt:
        first_monday = current + timedelta(days=(0 - current.weekday()) % 7)
        third_monday = first_monday + timedelta(days=14)
        if start_dt <= first_monday <= end_dt:
            dates.append(first_monday)
        if start_dt <= third_monday <= end_dt:
            dates.append(third_monday)
        # advance to first day of next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1, day=1)
        else:
            current = current.replace(month=current.month + 1, day=1)
    return dates


def generate_company_revenue_transactions(
    companies_df: pd.DataFrame,
    bents_by_bank: dict,
    known_accounts: set,
    register,
    pending_deposits: dict,
    start_dt: datetime,
    end_dt: datetime,
    months_in_range: float,
) -> None:
    """Generate incoming revenue transactions (card/ACH/check/cash receipts)
    for company accounts from synthesized, non-tracked counterparties.

    Companies previously had no incoming-transaction mechanism at all -
    accepted_payment_methods/average_expense/revenue_monthly_target are
    populated by scripts/recalibrate_transaction_volume.py. The synthesized
    counterparty is deliberately not a known account (same pattern
    generator/rule_injectors.py's inject_cty uses) so split_transaction
    emits only the credit leg on the company's own account, matching how a
    real bank statement shows revenue from untracked external payers.
    """
    for _, company in companies_df.iterrows():
        revenue_target = company.get("revenue_monthly_target")
        if pd.isna(revenue_target):
            continue
        num_txns = max(1, int(round(float(revenue_target) * months_in_range)))

        comp_acct_id = company.get("account_number")
        if pd.isna(comp_acct_id):
            comp_acct_id = company["entity_id"]
        comp_acct = ProfileAccount(
            id=comp_acct_id,
            owner_id=company["entity_id"],
            owner_type="Company",
            owner_name=company.get("name", ""),
            address=company.get("address", "")
        )

        pay_opts = company.get("accepted_payment_methods")
        if isinstance(pay_opts, str) and pay_opts.strip():
            payment_types = [p.strip().lower() for p in pay_opts.split(',') if p.strip()]
        else:
            payment_types = PAYMENT_TYPES

        avg_rev = company.get("average_expense")
        avg_rev = 500.0 if pd.isna(avg_rev) else float(avg_rev)

        company_segment = get_company_segment(company.get("entity_id"))

        for _ in range(num_txns):
            # Amount is drawn first from average_expense (unchanged,
            # already hand-calibrated) - payment_type is then picked only
            # from among the company's accepted methods whose typical
            # range (for its C1/C2/C3 segment) actually contains this
            # amount, instead of chosen independently of it.
            amount = round(random.uniform(avg_rev * 0.7, avg_rev * 1.3), 2)
            payment_type = pick_payment_type_for_amount(
                payment_types, amount, company_segment,
                PAYMENT_RANGES["payment_type_ranges"]["companies"],
            )

            # ATMs/branches are self-service and available 24/7 - only cash
            # legs skip the business-hours constraint.
            ts_dt = generate_transaction_timestamp(
                start_dt, end_dt, entity_type="Company",
                override_hours=(payment_type == "cash"),
            )
            timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
            txn_id = generate_uuid()

            if payment_type == "cash":
                deposit_now = random.choice([True, False])
                if not deposit_now:
                    pending_deposits[comp_acct.id] = pending_deposits.get(comp_acct.id, 0) + amount
                    continue

                bank = str(company.get("bank"))
                bents = bents_by_bank.get(bank, [])
                if bents:
                    bent = random.choice(bents)
                    bent_id = bent.get("name")
                    bent_loc = bent.get("address")
                else:
                    bent_id = generate_uuid(8)
                    bent_loc = fake.address().replace("\n", ", ")

                entries = split_transaction(
                    txn_id=txn_id,
                    timestamp=timestamp,
                    src=None,
                    tgt=comp_acct,
                    amount=amount,
                    currency="USD",
                    payment_type="cash",
                    is_laundering=False,
                    known_accounts=known_accounts,
                    post_date=post_date,
                    atm_id=bent_id,
                    atm_location=bent_loc,
                )
                if not register(entries):
                    return
            else:
                counterparty_type = random.choice(["Person", "Company"])
                counterparty = ProfileAccount(
                    id=generate_uuid(),
                    owner_id=generate_uuid(8),
                    owner_type=counterparty_type,
                    owner_name=fake.name() if counterparty_type == "Person" else fake.company(),
                    address=fake.address().replace("\n", ", ")
                )
                entries = split_transaction(
                    txn_id=txn_id,
                    timestamp=timestamp,
                    src=counterparty,
                    tgt=comp_acct,
                    amount=amount,
                    currency="USD",
                    payment_type=payment_type,
                    is_laundering=False,
                    source_description=describe_transaction(payment_type, "Revenue"),
                    known_accounts=known_accounts,
                    post_date=post_date
                )
                if not register(entries):
                    return


def generate_self_employment_income_transactions(
    profile_df: pd.DataFrame,
    bents_by_bank: dict,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
    months_in_range: float,
) -> None:
    """Generate income for the persons the payroll loop can't reach - any
    person with no `employer` value (self-employed or unemployed). Previously
    these persons had no income transactions anywhere in the generator, so
    they could spend indefinitely with no funding source, which is itself an
    unrealistic pattern for a clean account.

    Self-employed persons (segments P2/P4, config/payment_ranges.yaml's
    client_payment table) get irregular client/customer payments - amount
    and frequency are both irregular, which is part of what distinguishes
    self-employment income from salaried payroll.

    Unemployed-but-High-income persons (segment P6, unearned_income table)
    get a periodic distribution instead - confirmed by Rio 2026-07-10 as
    explained unearned income (trust/investment/rental), not deliberately
    unexplained wealth.
    """
    no_employer = profile_df[(profile_df["type"] == "person") & profile_df["employer"].isna()]

    for _, person in no_employer.iterrows():
        segment = get_person_segment(person.get("income_level"), person.get("employment_status"))

        if segment in PAYMENT_RANGES["client_payment"]:
            spec = PAYMENT_RANGES["client_payment"][segment]
            lo_n, hi_n = spec["per_month"]
            num_payments = max(1, int(round(random.uniform(lo_n, hi_n) * months_in_range)))
        elif segment in PAYMENT_RANGES["unearned_income"]:
            spec = PAYMENT_RANGES["unearned_income"][segment]
            num_payments = max(1, int(round(months_in_range)))  # monthly
        else:
            continue

        amt_lo, amt_hi = spec["amount"]
        payment_types = spec["payment_types"]

        person_acct_id = person.get("account_number")
        if pd.isna(person_acct_id):
            person_acct_id = person["entity_id"]
        person_acct = ProfileAccount(
            id=person_acct_id,
            owner_id=person["entity_id"],
            owner_type="Person",
            owner_name=person.get("name", ""),
            address=person.get("address", "")
        )

        for _ in range(num_payments):
            amount = round(random.uniform(amt_lo, amt_hi), 2)
            payment_type = random.choice(payment_types)

            ts_dt = generate_transaction_timestamp(
                start_dt, end_dt, entity_type="Person",
                override_hours=(payment_type == "cash"),
            )
            timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
            txn_id = generate_uuid()

            if payment_type == "cash":
                bank = str(person.get("bank"))
                bents = bents_by_bank.get(bank, [])
                if bents:
                    bent = random.choice(bents)
                    bent_id = bent.get("name")
                    bent_loc = bent.get("address")
                else:
                    bent_id = generate_uuid(8)
                    bent_loc = fake.address().replace("\n", ", ")

                entries = split_transaction(
                    txn_id=txn_id,
                    timestamp=timestamp,
                    src=None,
                    tgt=person_acct,
                    amount=amount,
                    currency="USD",
                    payment_type="cash",
                    is_laundering=False,
                    known_accounts=known_accounts,
                    post_date=post_date,
                    atm_id=bent_id,
                    atm_location=bent_loc,
                )
            else:
                counterparty = ProfileAccount(
                    id=generate_uuid(),
                    owner_id=generate_uuid(8),
                    owner_type="Company",
                    owner_name=fake.company(),
                    address=fake.address().replace("\n", ", ")
                )
                purpose = "Client Payment" if segment in PAYMENT_RANGES["client_payment"] else "Distribution"
                entries = split_transaction(
                    txn_id=txn_id,
                    timestamp=timestamp,
                    src=counterparty,
                    tgt=person_acct,
                    amount=amount,
                    currency="USD",
                    payment_type=payment_type,
                    is_laundering=False,
                    source_description=describe_transaction(payment_type, purpose),
                    known_accounts=known_accounts,
                    post_date=post_date
                )
            if not register(entries):
                return


def generate_legit_transactions(accounts, entities, n=1000, start_date="2025-01-01", end_date="2025-01-31", known_accounts=None):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    known_accounts = set(known_accounts) if known_accounts else set()

    transactions = []
    attempts = 0
    skipped_visibility = 0
    skipped_known = 0
    skipped_payment_type = 0
    success = 0

    while success < n and attempts < n * 10:  # Avoid infinite loops
        attempts += 1

        # Randomly choose a primary account and its owning entity
        primary_acct = random.choice(accounts)
        primary_entity = next((e for e in entities if e.id == primary_acct.owner_id), None)
        if not primary_entity:
            continue

        sender_rules = primary_entity.get_allowed_transactions()
        if not sender_rules:
            skipped_payment_type += 1
            continue

        payment_type = random.choice(list(sender_rules.keys()))
        purpose = random.choice(sender_rules[payment_type])
        source_description = describe_transaction(payment_type, purpose)

        ts_dt = generate_transaction_timestamp(
            start_dt,
            end_dt,
            entity_type=primary_entity.__class__.__name__,
        )
        timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
        amount = round(random.uniform(50, 5000), 2)
        txn_id = generate_uuid()

        # Determine src/tgt accounts based on payment type
        if payment_type.lower() == "cash":
            deposit = purpose.lower() == "deposit" if purpose else random.choice([True, False])
            if deposit:
                src = None
                tgt = primary_acct
                if tgt.id not in known_accounts:
                    skipped_known += 1
                    continue
                if primary_entity.visibility not in ["receiver", "both"]:
                    skipped_visibility += 1
                    continue
            else:
                src = primary_acct
                tgt = None
                if src.id not in known_accounts:
                    skipped_known += 1
                    continue
                if primary_entity.visibility not in ["sender", "both"]:
                    skipped_visibility += 1
                    continue
        else:
            # Non-cash transfers require two accounts
            src = primary_acct
            tgt = random.choice([a for a in accounts if a.id != src.id])
            tgt_entity = next((e for e in entities if e.id == tgt.owner_id), None)
            if not tgt_entity:
                continue
            if src.id not in known_accounts and tgt.id not in known_accounts:
                skipped_known += 1
                continue
            if primary_entity.visibility not in ["sender", "both"] or tgt_entity.visibility not in ["receiver", "both"]:
                skipped_visibility += 1
                continue

        entries = split_transaction(
            txn_id=txn_id,
            timestamp=timestamp,
            src=src,
            tgt=tgt,
            amount=amount,
            currency="USD",
            payment_type=payment_type,
            is_laundering=False,
            source_description=source_description,
            known_accounts=known_accounts,
            post_date=post_date
        )

        transactions.extend(entries)
        success += 1

    print(f"[DEBUG] Attempted: {attempts}")
    print(f"[DEBUG] Success: {success}")
    print(f"[DEBUG] Skipped (visibility): {skipped_visibility}")
    print(f"[DEBUG] Skipped (unknown accounts): {skipped_known}")
    print(f"[DEBUG] Skipped (payment type issues): {skipped_payment_type}")

    return transactions


def generate_profile_transactions(
    profile_df: pd.DataFrame,
    start_date: str,
    end_date: str,
    max_transactions: int | None = None,
) -> tuple[list[dict], int]:
    """Generate transactions using structured agent profiles.

    Returns a tuple of (ledger_entries, base_transaction_count).
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # merchant_frequency values are calibrated per-month; scale by the
    # number of months actually spanned so a 3-month range isn't as sparse
    # as a 1-month range.
    months_in_range = max(1.0, (end_dt - start_dt).days / 30.0)

    known_accounts = set(profile_df["account_number"].dropna().astype(str))

    merchants = profile_df[profile_df["type"] == "merchant"].copy()
    companies_df = profile_df[profile_df["type"] == "company"].copy()
    payers = profile_df[profile_df["type"].isin(["person", "company"])]
    bent_df = profile_df[profile_df["type"] == "BEnt"]
    bents_by_bank = {
        str(bank): g[["name", "address"]].to_dict("records")
        for bank, g in bent_df.groupby("bank")
    }

    pending_deposits: dict[str, float] = {}

    transactions = []
    base_txn_count = 0
    limit_reached = False

    def register(entries: list[dict]) -> bool:
        nonlocal base_txn_count, limit_reached

        if limit_reached:
            return False

        transactions.extend(entries)
        base_txn_count += 1

        if max_transactions is not None and base_txn_count >= max_transactions:
            limit_reached = True
        return not limit_reached

    for _, payer in payers.iterrows():
        if limit_reached:
            break

        patterns = payer.get("merchant_patterns")
        freqs = payer.get("merchant_frequency")
        if not isinstance(patterns, str) or not isinstance(freqs, str):
            continue

        pattern_list = [p.strip() for p in patterns.split(',') if p.strip()]
        freq_list = [float(f.strip()) for f in freqs.split(',') if f.strip()]
        if not pattern_list or not freq_list:
            continue

        txn_scaler = float(payer.get("transaction_scaler") or 1)
        if payer["type"] == "person":
            payer_segment = get_person_segment(payer.get("income_level"), payer.get("employment_status"))
            payer_range_table = PAYMENT_RANGES["payment_type_ranges"]["persons"]
        else:
            payer_segment = get_company_segment(payer.get("entity_id"))
            payer_range_table = PAYMENT_RANGES["payment_type_ranges"]["companies"]
        payer_acct_id = payer.get("account_number")
        if pd.isna(payer_acct_id):
            payer_acct_id = payer["entity_id"]
        payer_acct = ProfileAccount(
            id=payer_acct_id,
            owner_id=payer["entity_id"],
            owner_type=payer["type"].capitalize(),
            owner_name=payer.get("name", ""),
            address=payer.get("address", "")
        )

        for code, freq in zip(pattern_list, freq_list):
            if limit_reached:
                break
            try:
                freq_val = float(freq)
            except ValueError:
                continue
            num_txns = max(1, int(round(freq_val * months_in_range)))

            eligible = merchants[merchants["naics_code"].astype(str).str.startswith(str(int(float(code)) if code.strip().replace('.', '', 1).isdigit() else code))]
            if eligible.empty:
                continue

            for _ in range(num_txns):
                if limit_reached:
                    break
                merchant = eligible.sample(1).iloc[0]
                tgt_acct_id = merchant.get("account_number")
                if pd.isna(tgt_acct_id):
                    tgt_acct_id = merchant["entity_id"]
                tgt_acct = ProfileAccount(
                    id=tgt_acct_id,
                    owner_id=merchant["entity_id"],
                    owner_type="Merchant",
                    owner_name=merchant.get("name", ""),
                    address=merchant.get("address", "")
                )

                pay_opts = merchant.get("accepted_payment_methods")
                if isinstance(pay_opts, str) and pay_opts.strip():
                    payment_types = [p.strip().lower() for p in pay_opts.split(',') if p.strip()]
                else:
                    payment_types = PAYMENT_TYPES

                avg_exp = merchant.get("average_expense")
                if pd.isna(avg_exp):
                    avg_exp = 100.0
                amount = random.uniform(avg_exp * 0.85, avg_exp * 1.15)
                amount *= txn_scaler

                # Amount is drawn first (unchanged - still driven by the
                # merchant's average_expense x the payer's transaction_scaler)
                # - payment_type is then picked only from among the merchant's
                # accepted methods whose typical range (for the payer's own
                # P1-P6/C1-C3 segment) actually contains this amount, instead
                # of chosen independently of it.
                payment_type = pick_payment_type_for_amount(
                    payment_types, amount, payer_segment, payer_range_table,
                )

                # ATMs are self-service and available 24/7, unlike card/check/ACH
                # purchases - only cash legs skip the business-hours constraint.
                ts_dt = generate_transaction_timestamp(
                    start_dt, end_dt, entity_type=payer_acct.owner_type,
                    override_hours=(payment_type == "cash"),
                )
                timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
                txn_id = generate_uuid()

                amount = round(amount, 2)

                if payment_type == "cash":
                    # Withdrawal by payer via BEnt
                    payer_bank = str(payer.get("bank"))
                    payer_bents = bents_by_bank.get(payer_bank, [])
                    if payer_bents:
                        bent = random.choice(payer_bents)
                        bent_id = bent.get("name")
                        bent_loc = bent.get("address")
                    else:
                        bent_id = generate_uuid(8)
                        bent_loc = fake.address().replace("\n", ", ")

                    entries = split_transaction(
                        txn_id=txn_id + "W",
                        timestamp=timestamp,
                        src=payer_acct,
                        tgt=None,
                        amount=amount,
                        currency="USD",
                        payment_type="cash",
                        is_laundering=False,
                        known_accounts=known_accounts,
                        post_date=post_date,
                        atm_id=bent_id,
                        atm_location=bent_loc

                    )
                    if not register(entries):
                        break

                    deposit_now = random.choice([True, False])
                    if deposit_now:
                        if limit_reached:
                            break
                        merch_bank = str(merchant.get("bank"))
                        merch_bents = bents_by_bank.get(merch_bank, [])
                        if merch_bents:
                            bent2_rec = random.choice(merch_bents)
                            bent2 = bent2_rec.get("name")
                            bent2_loc = bent2_rec.get("address")
                        else:
                            bent2 = generate_uuid(8)
                            bent2_loc = fake.address().replace("\n", ", ")

                        entries = split_transaction(
                            txn_id=txn_id + "D",
                            timestamp=timestamp,
                            src=None,
                            tgt=tgt_acct,
                            amount=amount,
                            currency="USD",
                            payment_type="cash",
                            is_laundering=False,
                            known_accounts=known_accounts,
                            post_date=post_date,
                            atm_id=bent2,
                            atm_location=bent2_loc,
                        )
                        if not register(entries):
                            break
                    else:
                        pending_deposits[tgt_acct.id] = pending_deposits.get(tgt_acct.id, 0) + amount
                else:
                    entries = split_transaction(
                        txn_id=txn_id,
                        timestamp=timestamp,
                        src=payer_acct,
                        tgt=tgt_acct,
                        amount=amount,
                        currency="USD",
                        payment_type=payment_type,
                        is_laundering=False,
                        source_description=describe_transaction(payment_type, "Purchase"),
                        known_accounts=known_accounts,
                        post_date=post_date
                    )
                    if not register(entries):
                        break

    # Generate payroll transactions
    employees = profile_df[(profile_df["type"] == "person") & profile_df["employer"].notna()]
    companies = profile_df[profile_df["type"] == "company"].set_index("entity_id")
    payroll_dates = get_payroll_dates(start_dt, end_dt)

    for pay_date in payroll_dates:
        if limit_reached:
            break
        for _, emp in employees.iterrows():
            if limit_reached:
                break
            employer_id = emp.get("employer")
            if employer_id not in companies.index:
                continue
            comp = companies.loc[employer_id]

            # Biweekly amount by income_level/employment_status segment
            # (config/payment_ranges.yaml's payroll_biweekly) - replaces a
            # flat $5,000-base formula that ran 1.5-2.4x above BLS-grounded
            # wages for the Low/Medium tiers. Falls back to the Medium/P3
            # range if a segment has no payroll entry (e.g. a future
            # roster addition not yet classified).
            emp_segment = get_person_segment(emp.get("income_level"), emp.get("employment_status"))
            lo, hi = PAYMENT_RANGES["payroll_biweekly"].get(emp_segment, PAYMENT_RANGES["payroll_biweekly"]["P3"])
            amount = random.uniform(lo, hi)

            emp_acct_id = emp.get("account_number")
            if pd.isna(emp_acct_id):
                emp_acct_id = emp["entity_id"]
            comp_acct_id = comp.get("account_number")
            if pd.isna(comp_acct_id):
                comp_acct_id = comp["entity_id"]

            emp_acct = ProfileAccount(
                id=emp_acct_id,
                owner_id=emp["entity_id"],
                owner_type="Person",
                owner_name=emp.get("name", ""),
                address=emp.get("address", "")
            )
            comp_acct = ProfileAccount(
                id=comp_acct_id,
                owner_id=comp.name,
                owner_type="Company",
                owner_name=comp.get("name", ""),
                address=comp.get("address", "")
            )

            pay_start = pay_date.replace(hour=8, minute=0, second=0, microsecond=0)
            pay_end = pay_date.replace(hour=16, minute=59, second=59, microsecond=0)
            ts_dt = generate_transaction_timestamp(pay_start, pay_end, entity_type="Company")
            timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
            txn_id = generate_uuid()

            entries = split_transaction(
                txn_id=txn_id,
                timestamp=timestamp,
                src=comp_acct,
                tgt=emp_acct,
                amount=round(amount, 2),
                currency="USD",
                payment_type="ach",
                is_laundering=False,
                known_accounts=known_accounts,
                post_date=post_date
            )
            for e in entries:
                if e["direction"] == "credit":
                    e["source_description"] = f"ACH Direct Dep Payroll {comp_acct.owner_name} - {comp_acct.address}"
                else:
                    e["source_description"] = f"ACH Payroll {emp_acct.owner_name} - {emp_acct.id}"
            if not register(entries):
                break

    # Income for persons the payroll loop above can't reach - self-employed
    # (no employer, client payments) and unemployed-High-income (unearned
    # income). Previously these persons had no income transactions at all.
    generate_self_employment_income_transactions(
        profile_df=profile_df,
        bents_by_bank=bents_by_bank,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
        months_in_range=months_in_range,
    )

    # Revenue-side transactions for companies (card/ACH/check/cash receipts
    # from synthesized, non-tracked counterparties) - may add further
    # entries to pending_deposits for cash revenue deferred to the batch
    # deposit loop below.
    generate_company_revenue_transactions(
        companies_df=companies_df,
        bents_by_bank=bents_by_bank,
        known_accounts=known_accounts,
        register=register,
        pending_deposits=pending_deposits,
        start_dt=start_dt,
        end_dt=end_dt,
        months_in_range=months_in_range,
    )

    # Batch deposit accumulated cash for merchants/companies. Resolve
    # against both tables - a company account can be the deferred-deposit
    # target now that generate_company_revenue_transactions can defer cash
    # revenue the same way merchant cash purchases already do.
    revenue_targets = pd.concat([merchants, companies_df])
    for acct_id, amt in pending_deposits.items():
        if limit_reached:
            break
        target_row = revenue_targets[revenue_targets["account_number"] == acct_id]
        if target_row.empty:
            continue
        target = target_row.iloc[0]
        target_bank = str(target.get("bank"))
        target_bents = bents_by_bank.get(target_bank, [])
        if target_bents:
            bent_rec = random.choice(target_bents)
            bent_id = bent_rec.get("name")
            bent_loc = bent_rec.get("address")
        else:
            bent_id = generate_uuid(8)
            bent_loc = fake.address().replace("\n", ", ")

        tgt_acct = ProfileAccount(
            id=acct_id,
            owner_id=target["entity_id"],
            owner_type="Merchant" if target["type"] == "merchant" else "Company",
            owner_name=target.get("name", ""),
            bank_name="",
            address=target.get("address", "")
        )

        # Cash deposit - ATMs/branches are self-service and available 24/7.
        ts_dt = generate_transaction_timestamp(start_dt, end_dt, entity_type="Company", override_hours=True)
        timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
        txn_id = generate_uuid()

        entries = split_transaction(
            txn_id=txn_id,
            timestamp=timestamp,
            src=None,
            tgt=tgt_acct,
            amount=round(amt, 2),
            currency="USD",
            payment_type="cash",
            is_laundering=False,
            known_accounts=known_accounts,
            post_date=post_date,
            atm_id=bent_id,
            atm_location=bent_loc
        )
        if not register(entries):
            break

    if limit_reached and max_transactions is not None:
        log(
            f"⚖️ Trimmed profile-driven transactions to requested limit "
            f"({base_txn_count}/{max_transactions})"
        )

    return transactions, base_txn_count
