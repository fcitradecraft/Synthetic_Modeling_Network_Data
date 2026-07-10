"""
One injector function per Alert Matrix rule subtype, dispatched by
rule_id. Each function returns transaction rows tagged with rule_id,
typology, role_in_typology, and difficulty for the answer key - these
tags are added to the row dicts here and must be stripped before the
student-facing file is written (see main.py).

Reuses utils.helpers.split_transaction for all double-entry mechanics,
firing on rule-accurate parameters (from thresholds.yaml) rather than
generic typology parameters.
"""
import random
from datetime import timedelta

from generator.transactions import PAYMENT_TYPES, ProfileAccount
from utils.helpers import (
    fake,
    generate_uuid,
    generate_post_date,
    generate_transaction_timestamp,
    split_transaction,
)

# Account-to-account transfer rails only - excludes cash (ATM/branch) and
# the point-of-sale card sub-types (ccard/credit/debit/pos), which model a
# purchase, not a transfer. p2p is included: Venmo/Zelle-style structuring
# is a real typology, even though none of the injectors below draw on it
# yet (each hardcodes payment_type to match its own rule's textual
# definition - DST/EAT fire on cash, CTY fires on wire). This list exists
# so a future P2P-based rule from the Alert Matrix has an accurate rail
# list to pull from.
TRANSFER_PAYMENT_TYPES = [p for p in PAYMENT_TYPES if p not in ("cash", "ccard", "credit", "debit", "pos")]


def _tag(rows, rule_id, typology, role, difficulty):
    for r in rows:
        r["rule_id"] = rule_id
        r["typology"] = typology
        r["role_in_typology"] = role
        r["difficulty"] = difficulty
    return rows


def _pick_branch(bent_pool):
    """Return (atm_id, atm_location) from a real branch, or a generated
    fallback if no branch pool is available - never falls through to
    split_transaction's generic fake-company-name default, which produces
    ATM locations inconsistent with the bank's own branch network."""
    if bent_pool:
        branch = random.choice(bent_pool)
        return branch.get("name"), branch.get("address")
    return generate_uuid(8), fake.address().replace("\n", ", ")


def inject_dst(account, rule_id, params, window_start, window_end, known_accounts, direction, bent_pool=None):
    """DST: structuring - N cash pieces, sub-CTR-threshold, same day, aggregating above it.

    direction: 'INN' (deposits) or 'OUT' (withdrawals). Timestamps are
    unconstrained by business hours - these are ATM/cash-equivalent
    transactions, and ATMs are self-service and available 24/7.
    """
    day = fake.date_time_between_dates(window_start, window_end)
    day = day.replace(hour=random.randint(0, 23), minute=random.randint(0, 59))

    lo = params.get("deposit_min", params.get("withdrawal_min", 8200))
    hi = params.get("deposit_max", params.get("withdrawal_max", 9800))
    ctr_threshold = params["ctr_threshold"]
    min_pieces = params.get("min_deposits", params.get("min_withdrawals", 2))

    pieces = []
    total = 0
    while total < ctr_threshold or len(pieces) < min_pieces:
        amount = round(random.uniform(lo, hi), 2)
        pieces.append(amount)
        total += amount

    rows = []
    for i, amount in enumerate(pieces):
        ts = day.replace(minute=(day.minute + i * 7) % 60)
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
        atm_id, atm_location = _pick_branch(bent_pool)
        entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=None if direction == "INN" else account,
            tgt=account if direction == "INN" else None,
            amount=amount,
            currency="USD",
            payment_type="cash",
            is_laundering=True,
            known_accounts=known_accounts,
            post_date=post_date,
            atm_id=atm_id,
            atm_location=atm_location,
        )
        rows.extend(entries)

    return _tag(rows, rule_id, "structuring", "structurer", "medium")


def inject_eat(account, rule_id, params, window_start, window_end, known_accounts, tran_type, direction, bent_pool=None):
    """EAT: excessive activity - cash/ATM transactions over a window, aggregating above threshold_amount.

    Confined to a random window_days-length sub-window within
    [window_start, window_end] - the rule fires on activity concentrated
    within that window, not spread across however wide a range the
    caller happens to pass. Timestamps are unconstrained by business
    hours (ATMs are self-service, available 24/7).
    """
    threshold = params["threshold_amount"]
    window_days = params.get("window_days", 5)
    burst_start = fake.date_time_between_dates(window_start, max(window_start, window_end - timedelta(days=window_days)))
    burst_end = min(window_end, burst_start + timedelta(days=window_days))
    payment_type = "cash"

    rows = []
    total = 0
    while total < threshold:
        amount = round(random.uniform(400, 1200), 2)
        ts = generate_transaction_timestamp(burst_start, burst_end, override_hours=True)
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
        atm_id, atm_location = _pick_branch(bent_pool)

        entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=account if direction == "OUT" else None,
            tgt=None if direction == "OUT" else account,
            amount=amount,
            currency="USD",
            payment_type=payment_type,
            is_laundering=True,
            known_accounts=known_accounts,
            post_date=post_date,
            atm_id=atm_id,
            atm_location=atm_location,
        )
        rows.extend(entries)
        total += amount

    return _tag(rows, rule_id, f"excessive_{tran_type.lower()}", "account_holder", "low")


def inject_cty(account, rule_id, params, window_start, window_end, known_accounts, high_risk_countries):
    """CTY: single wire to/from a fictional counterparty in a high-risk jurisdiction."""
    country = random.choice(high_risk_countries)
    counterparty = ProfileAccount(
        id=generate_uuid(10),
        owner_id=generate_uuid(8),
        owner_type="Company",
        owner_name=f"{fake.company()} ({country['name']})",
        address=f"{fake.street_address()}, {country['name']}",
    )

    ts = generate_transaction_timestamp(window_start, window_end, override_hours=True)
    timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
    post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
    direction = random.choice(["INN", "OUT"])
    amount = round(random.uniform(15000, 60000), 2)

    entries = split_transaction(
        txn_id=generate_uuid(),
        timestamp=timestamp,
        src=counterparty if direction == "INN" else account,
        tgt=account if direction == "INN" else counterparty,
        amount=amount,
        currency="USD",
        payment_type="wire",
        is_laundering=True,
        known_accounts=known_accounts,
        post_date=post_date,
    )
    return _tag(entries, rule_id, "high_risk_country", "account_holder", "low")


def inject_mbd(account, rule_id, params, window_start, window_end, known_accounts, bent_pool):
    """MBD: cash deposits into the same account at several distinct branches within a window.

    Reuses atm_id/atm_location (already populated on every cash row - see
    utils/helpers.split_transaction) as branch identity rather than adding
    a new schema field, so no new leak-risk column is introduced.
    """
    min_branches = params.get("min_branches", 3)
    min_amount = params.get("min_deposit_amount", 1000)
    window_days = params.get("window_days", 5)
    burst_start = fake.date_time_between_dates(window_start, max(window_start, window_end - timedelta(days=window_days)))
    burst_end = min(window_end, burst_start + timedelta(days=window_days))
    branches = random.sample(bent_pool, min(min_branches, len(bent_pool)))

    rows = []
    for branch in branches:
        amount = round(random.uniform(min_amount, min_amount * 3), 2)
        ts = generate_transaction_timestamp(burst_start, burst_end, override_hours=True)
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")

        entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=None,
            tgt=account,
            amount=amount,
            currency="USD",
            payment_type="cash",
            is_laundering=True,
            known_accounts=known_accounts,
            post_date=post_date,
            atm_id=branch.get("name"),
            atm_location=branch.get("address"),
        )
        rows.extend(entries)

    return _tag(rows, rule_id, "multiple_branch_deposits", "account_holder", "medium")
