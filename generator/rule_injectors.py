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

from generator.transactions import (
    PAYMENT_TYPES,
    ProfileAccount,
    choose_cash_withdrawal,
    get_company_size_tier,
    PAYMENT_RANGES,
    TRADE_COUNTRIES,
)
from utils.helpers import (
    fake,
    generate_uuid,
    generate_post_date,
    generate_transaction_timestamp,
    generate_synthetic_bic,
    get_bent_type,
    split_transaction,
)

# Account-to-account transfer rails only - excludes cash (ATM/branch) and
# the point-of-sale card sub-types (ccard/credit/debit/pos), which model a
# purchase, not a transfer. p2p is included: Venmo/Zelle-style structuring
# is a real typology. Used by inject_ftf (FTF's tran_type is genuinely
# ALL/"any transaction type," but a one-directional card purchase doesn't
# fit a flow-through's in-then-out shape) - every other injector fires on
# its own rule-specific rail (DST/EAT on cash, CTY/EST/EOP on wire), so
# this stays the one shared multi-rail list.
TRANSFER_PAYMENT_TYPES = [p for p in PAYMENT_TYPES if p not in ("cash", "ccard", "credit", "debit", "pos")]


def _tag(rows, rule_id, typology, role, difficulty):
    for r in rows:
        r["rule_id"] = rule_id
        r["typology"] = typology
        r["role_in_typology"] = role
        r["difficulty"] = difficulty
    return rows


def _pick_branch(bank, bents_by_bank):
    """Pick a branch (never ATM) bent for a c_check-issuing teller visit -
    a cashier's check is a bank/teller instrument, never dispensed by an
    ATM (unlike cash, which choose_cash_withdrawal may route through
    either). Falls back to a synthetic placeholder if the bank has no
    modeled branches."""
    bents = (bents_by_bank or {}).get(bank, [])
    branch_bents = [b for b in bents if get_bent_type(b.get("name")) != "atm"]
    if branch_bents:
        b = random.choice(branch_bents)
        return b.get("name"), b.get("address")
    return generate_uuid(8), fake.address().replace("\n", ", ")


def _segment_wire_range(owner_type, segment, transaction_scaler):
    """Return (lo, hi) for a size/segment-aware wire amount - a person
    account's own P1-P6 wire range (payment_type_ranges.persons[segment].
    wire) or a company account's size-tier wire range
    (size_tiered_payment_ranges.wire[tier], via get_company_size_tier) -
    the same tables the legitimate wire-generation path already uses
    (generator/transactions.py), so an injected wire looks native to the
    account it landed on instead of drawing from one flat system-wide
    range. Falls back to a flat $15k-$60k range if neither lookup applies
    (e.g. a company with no transaction_scaler, or an unrecognized
    owner_type)."""
    if str(owner_type).lower() == "person" and segment:
        rng = PAYMENT_RANGES["payment_type_ranges"]["persons"].get(segment, {}).get("wire")
        if rng:
            return tuple(rng)
    elif str(owner_type).lower() == "company":
        tier = get_company_size_tier(transaction_scaler)
        return tuple(PAYMENT_RANGES["size_tiered_payment_ranges"]["wire"][tier])
    return (15000, 60000)


def _draw_segment_wire_amount(owner_type, segment, transaction_scaler, floor=None):
    """Draw a wire amount from _segment_wire_range, optionally forced above
    ``floor`` (an "excessive transfer" rule's own threshold_amount) - if
    the account's own segment/size range already clears the floor (e.g. a
    large company, whose range already runs well past it), the range is
    used as-is; if not (e.g. a P1 person, whose ordinary wire ceiling is
    far below a $50k floor), the range is shifted up to start at the floor
    so the amount is still segment-flavored (scaled off the account's own
    range width) rather than a flat draw."""
    lo, hi = _segment_wire_range(owner_type, segment, transaction_scaler)
    if floor is not None:
        lo = max(lo, floor)
        hi = max(hi, lo * 1.5)
    return round(random.uniform(lo, hi), 2)


def inject_dst(account, rule_id, params, window_start, window_end, known_accounts, direction,
                bents_by_bank=None, bank=None, segment=None):
    """DST: structuring - several independent same-day episodes spread
    across a week, each made of N cash pieces sub-CTR-threshold that
    together aggregate above it for that specific day.

    direction: 'INN' (deposits) or 'OUT' (withdrawals). Timestamps are
    unconstrained by business hours - these are ATM/cash-equivalent
    transactions, and ATMs are self-service and available 24/7.

    Repeated-episode structure (Rio's call, 2026-07-15): real structuring
    is rarely a single one-off day - a structurer typically repeats the
    pattern several times over a period, which is what actually trips a
    rule tuned to catch it. min_episodes/max_episodes (DRAFT 3/5) each
    land on a distinct day within window_days (finally used here - it sat
    unread in thresholds.yaml since this injector was first built).

    Amount/source selection reuses choose_cash_withdrawal (same utility
    every legitimate cash path uses) rather than a raw random bent choice -
    real ATMs dispense in $20 multiples and cap daily withdrawals per
    account; a structuring piece ($6,500-$9,999 here) is always far above
    every segment's atm_daily_limit, so this naturally and correctly routes
    every piece to a branch/teller (exact amount, uncapped) - no real ATM
    dispenses that much in one transaction.
    """
    lo = params.get("deposit_min", params.get("withdrawal_min", 6500))
    hi = params.get("deposit_max", params.get("withdrawal_max", 9999))
    ctr_threshold = params["ctr_threshold"]
    min_pieces = params.get("min_deposits", params.get("min_withdrawals", 2))
    window_days = params.get("window_days", 7)
    min_episodes = params.get("min_episodes", 3)
    max_episodes = params.get("max_episodes", 5)

    span_days = max(1, min((window_end - window_start).days, window_days))
    n_episodes = random.randint(min_episodes, max_episodes)
    offsets = random.sample(range(span_days + 1), min(n_episodes, span_days + 1))

    atm_daily_totals: dict = {}
    rows = []
    for offset in offsets:
        day = window_start + timedelta(days=offset)
        day = day.replace(hour=random.randint(0, 23), minute=random.randint(0, 59))

        pieces = []
        total = 0
        while total < ctr_threshold or len(pieces) < min_pieces:
            amount = round(random.uniform(lo, hi), 2)
            pieces.append(amount)
            total += amount

        for i, amount in enumerate(pieces):
            ts = day.replace(minute=(day.minute + i * 7) % 60)
            timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
            date_key = ts.strftime("%Y-%m-%d")
            final_amount, atm_id, atm_location = choose_cash_withdrawal(
                account, bank, bents_by_bank or {}, amount, segment, date_key, atm_daily_totals,
            )
            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=None if direction == "INN" else account,
                tgt=account if direction == "INN" else None,
                amount=final_amount,
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


def inject_eat(account, rule_id, params, window_start, window_end, known_accounts, tran_type, direction,
               bents_by_bank=None, bank=None, segment=None, party_accounts=None):
    """EAT: excessive activity - cash/ATM transactions over a window, aggregating above threshold_amount.

    Confined to a random window_days-length sub-window within
    [window_start, window_end] - the rule fires on activity concentrated
    within that window, not spread across however wide a range the
    caller happens to pass. Timestamps are unconstrained by business
    hours (ATMs are self-service, available 24/7).

    Amount/source selection reuses choose_cash_withdrawal - these pieces
    ($400-1,200) are realistically ATM-sized, so (unlike DST) some will
    actually round to a $20 multiple and route through an ATM, capped per
    day by the account's segment, same as legitimate cash activity.

    CCE ("Cash or Cash Equivalents") rules can also fire via c_check
    (Rio's call, 2026-07-15) - a cashier's check is a genuine cash-
    equivalent instrument, just issued at a branch/teller rather than an
    ATM, so it's never subject to choose_cash_withdrawal's ATM rounding/
    cap. ATM-category rules stay cash-only - they're specifically about
    ATM transactions, not cash equivalents generally.

    party_accounts (party-group scope only, e.g. AML-ATM-ATM-OUT-P-D05-
    EAT): additional ProfileAccount objects sharing this account's
    party_id (see main.py's accounts_by_party_id) - each piece picks a
    random payer from the flagged account plus its party rather than
    always the same one, modeling a customer-relationship-level burst
    (e.g. a business owner and their company both making ATM withdrawals)
    instead of one account's own activity. Each payer's own bank/segment
    is looked up per piece so a party member at a different bank still
    gets their own bank's ATM/branch network and daily cap, not the
    anchor account's.
    """
    threshold = params["threshold_amount"]
    window_days = params.get("window_days", 5)
    burst_start = fake.date_time_between_dates(window_start, max(window_start, window_end - timedelta(days=window_days)))
    burst_end = min(window_end, burst_start + timedelta(days=window_days))
    cce_eligible = tran_type == "CCE"
    payer_pool = [account] + list(party_accounts or [])

    atm_daily_totals: dict = {}
    rows = []
    total = 0
    while total < threshold:
        payer = random.choice(payer_pool)
        payer_bank = str(getattr(payer, "bank", "")) or None if payer is not account else bank
        payer_segment = getattr(payer, "segment", None) if payer is not account else segment
        amount = round(random.uniform(400, 1200), 2)
        ts = generate_transaction_timestamp(burst_start, burst_end, override_hours=True)
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
        date_key = ts.strftime("%Y-%m-%d")

        if cce_eligible and random.random() < 0.3:
            final_amount = amount
            branch_id, branch_loc = _pick_branch(payer_bank, bents_by_bank)
            label = "Withdrawal" if direction == "OUT" else "Deposit"
            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=payer if direction == "OUT" else None,
                tgt=None if direction == "OUT" else payer,
                amount=final_amount,
                currency="USD",
                payment_type="c_check",
                is_laundering=True,
                known_accounts=known_accounts,
                post_date=post_date,
                source_description=f"CASHIER'S CHECK - {label} at {branch_loc}",
            )
        else:
            final_amount, atm_id, atm_location = choose_cash_withdrawal(
                payer, payer_bank, bents_by_bank or {}, amount, payer_segment, date_key, atm_daily_totals,
            )
            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=payer if direction == "OUT" else None,
                tgt=None if direction == "OUT" else payer,
                amount=final_amount,
                currency="USD",
                payment_type="cash",
                is_laundering=True,
                known_accounts=known_accounts,
                post_date=post_date,
                atm_id=atm_id,
                atm_location=atm_location,
            )
        rows.extend(entries)
        total += final_amount

    return _tag(rows, rule_id, f"excessive_{tran_type.lower()}", "account_holder", "low")


def inject_cty(account, rule_id, params, window_start, window_end, known_accounts, high_risk_countries,
                segment=None, owner_type=None, transaction_scaler=None):
    """CTY: single wire to/from a fictional counterparty in a high-risk jurisdiction.

    Wire amount is segment/size-aware (Rio's call, 2026-07-15) instead of
    a flat $15k-$60k draw - a person account pulls from its own P1-P6 wire
    range, a company account from its size tier's wire range (same tables/
    helper the legitimate wire path already uses - see
    _segment_wire_range). A large company can now wire well past
    $2,000,000 (this tier's ceiling was raised to $5,000,000 in
    config/payment_ranges.yaml specifically for this). The counterparty's
    country_code/swift_code are also now actually set - previously
    omitted, so a CTY-flagged wire's counterparty_country_code silently
    defaulted to "US" in split_transaction despite literally being a
    high-risk-country transfer; now matches the same naming/BIC convention
    maybe_internationalize_wire_counterparty uses for legitimate
    international wires.
    """
    country = random.choice(high_risk_countries)
    counterparty = ProfileAccount(
        id=generate_uuid(10),
        owner_id=generate_uuid(8),
        owner_type="Company",
        owner_name=f"{fake.company()} ({country['name']})",
        address=f"{fake.street_address()}, {country['name']}",
    )
    counterparty.country_code = country["country_code"]
    counterparty.swift_code = generate_synthetic_bic(country["country_code"])

    ts = generate_transaction_timestamp(window_start, window_end, override_hours=True)
    timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
    post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
    direction = random.choice(["INN", "OUT"])
    amount = _draw_segment_wire_amount(owner_type, segment, transaction_scaler)

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


def inject_est(account, rule_id, params, window_start, window_end, known_accounts, direction,
               segment=None, owner_type=None, transaction_scaler=None):
    """EST: single excessive wire transfer clearing threshold_amount.

    Not restricted to a high-risk-country counterparty - thresholds.yaml
    carries no high_risk_country_list for this rule (unlike CTY), so "High
    Risk" here reads as "high-dollar-value transfer," not "to/from a
    high-risk jurisdiction." Counterparty is domestic by default,
    occasionally an ordinary international trading partner
    (config/trade_countries.yaml) for realism - kept entirely distinct
    from CTY's high-risk-only pool. Amount is segment/size-aware but
    forced above threshold_amount (see _draw_segment_wire_amount's floor
    argument) - the rule is specifically about a transfer that's
    excessive *for this account*, so a P1 person's injected wire is still
    scaled off their own range, just pushed past the threshold rather than
    drawn from it unmodified.
    """
    threshold = params["threshold_amount"]
    if random.random() < 0.25:
        country = random.choice(TRADE_COUNTRIES)
        counterparty = ProfileAccount(
            id=generate_uuid(10),
            owner_id=generate_uuid(8),
            owner_type="Company",
            owner_name=f"{fake.company()} ({country['name']})",
            address=f"{fake.street_address()}, {country['name']}",
        )
        counterparty.country_code = country["country_code"]
        counterparty.swift_code = generate_synthetic_bic(country["country_code"])
    else:
        counterparty = ProfileAccount(
            id=generate_uuid(10),
            owner_id=generate_uuid(8),
            owner_type="Company",
            owner_name=fake.company(),
            address=fake.address().replace("\n", ", "),
        )

    ts = generate_transaction_timestamp(window_start, window_end, override_hours=True)
    timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
    post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")
    amount = _draw_segment_wire_amount(owner_type, segment, transaction_scaler, floor=threshold)

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
    return _tag(entries, rule_id, "excessive_transfer", "account_holder", "low")


def inject_eop(account, rule_id, params, window_start, window_end, known_accounts, category,
                party_accounts=None):
    """EOP: burst of international wire transfers concentrated on a single
    counterparty (source spreadsheet: "a high volume of international
    electronic funds transfer activity to/from a single beneficiary/
    originator over a rolling window"). IFT = International Electronic
    Funds Transfer specifically - the counterparty is always a genuine
    international entity (config/trade_countries.yaml, the same ordinary-
    trading-partner pool legitimate international wires already use), not
    domestic and not restricted to CTY's high-risk-only list.

    category ("EBO" or "EBB", from the rule_id's own category token)
    picks the account's role: EBO ("Electronic Burst Originator") - the
    flagged account/party repeatedly *sends* to the one counterparty;
    EBB ("Electronic Burst Beneficiary") - it repeatedly *receives* from
    them. Pieces (DRAFT $1,500-$8,000 each) accumulate until the total
    clears threshold_amount within window_days - same aggregate-burst
    shape as inject_eat, applied to wire/IFT instead of cash.

    party_accounts (party-group scope only, e.g. the *-P-D05-EOP variants):
    additional ProfileAccount objects sharing this account's party_id -
    each piece picks a random payer/payee from the flagged account plus
    its party, modeling the burst at the customer-relationship level
    (e.g. an owner's personal and business accounts both wiring the same
    overseas counterparty) rather than one account acting alone.
    """
    threshold = params["threshold_amount"]
    window_days = params.get("window_days", 30)
    piece_min = params.get("piece_min", 1500)
    piece_max = params.get("piece_max", 8000)
    direction = "OUT" if category == "EBO" else "INN"

    burst_start = fake.date_time_between_dates(window_start, max(window_start, window_end - timedelta(days=window_days)))
    burst_end = min(window_end, burst_start + timedelta(days=window_days))

    country = random.choice(TRADE_COUNTRIES)
    counterparty = ProfileAccount(
        id=generate_uuid(10),
        owner_id=generate_uuid(8),
        owner_type="Company",
        owner_name=f"{fake.company()} ({country['name']})",
        address=f"{fake.street_address()}, {country['name']}",
    )
    counterparty.country_code = country["country_code"]
    counterparty.swift_code = generate_synthetic_bic(country["country_code"])

    payer_pool = [account] + list(party_accounts or [])

    rows = []
    total = 0
    while total < threshold:
        payer = random.choice(payer_pool)
        amount = round(random.uniform(piece_min, piece_max), 2)
        ts = generate_transaction_timestamp(burst_start, burst_end, override_hours=True)
        timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")

        entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=counterparty if direction == "INN" else payer,
            tgt=payer if direction == "INN" else counterparty,
            amount=amount,
            currency="USD",
            payment_type="wire",
            is_laundering=True,
            known_accounts=known_accounts,
            post_date=post_date,
        )
        rows.extend(entries)
        total += amount

    typology = "burst_beneficiary" if category == "EBB" else "burst_originator"
    return _tag(rows, rule_id, typology, "account_holder", "low")


def inject_ftf(account, rule_id, params, window_start, window_end, known_accounts):
    """FTF: flow-through of funds - a large volume of funds enters the
    account and a similar amount leaves within the same rolling window
    (classic pass-through/layering - money in, then back out before it can
    be traced to a single large balance sitting still).

    "Similar" (Rio's call, 2026-07-15, resolving the source workbook's own
    unanswered ratio question): the smaller of total-in/total-out must be
    at least ratio_threshold_pct (90%) of the larger - a symmetric band,
    not a one-sided outflow floor. Implemented by always drawing total_in
    as the larger side and total_out within [ratio_min * total_in,
    total_in] - which direction happens to be bigger isn't the
    evidentiary signal here, the near-equality is.

    tran_type is genuinely ALL/"any transaction type" per the rule's own
    definition, but a card purchase/POS is inherently one-directional (no
    "point of sale" refund flow), so pieces draw from
    TRANSFER_PAYMENT_TYPES (wire/ach/check/c_check/p2p) - the account-to-
    account rails this typology is actually about. Each piece uses its own
    fresh, unrelated counterparty (no concentration on one party - that's
    EOP's typology, not this one).
    """
    threshold = params.get("threshold_amount", 20000)
    ratio_min = params["ratio_threshold_pct"] / 100
    window_days = params.get("window_days", 7)

    burst_start = fake.date_time_between_dates(window_start, max(window_start, window_end - timedelta(days=window_days)))
    burst_end = min(window_end, burst_start + timedelta(days=window_days))

    total_in = round(random.uniform(threshold, threshold * 1.5), 2)
    total_out = round(random.uniform(total_in * ratio_min, total_in), 2)

    rows = []
    for total, direction in ((total_in, "INN"), (total_out, "OUT")):
        remaining = total
        while remaining > 0:
            piece = round(min(remaining, random.uniform(1000, 8000)), 2)
            remaining = round(remaining - piece, 2)
            payment_type = random.choice(TRANSFER_PAYMENT_TYPES)
            counterparty = ProfileAccount(
                id=generate_uuid(10),
                owner_id=generate_uuid(8),
                owner_type="Company",
                owner_name=fake.company(),
                address=fake.address().replace("\n", ", "),
            )
            ts = generate_transaction_timestamp(burst_start, burst_end, override_hours=True)
            timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts).strftime("%Y-%m-%d %H:%M:%S")

            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=counterparty if direction == "INN" else account,
                tgt=account if direction == "INN" else counterparty,
                amount=piece,
                currency="USD",
                payment_type=payment_type,
                is_laundering=True,
                known_accounts=known_accounts,
                post_date=post_date,
            )
            rows.extend(entries)

    return _tag(rows, rule_id, "flow_through", "account_holder", "medium")


def inject_mbd(account, rule_id, params, window_start, window_end, known_accounts, bent_pool):
    """MBD: cash deposits into the same account at several distinct branches within a window.

    Reuses atm_id/atm_location (already populated on every cash row - see
    utils/helpers.split_transaction) as branch identity rather than adding
    a new schema field, so no new leak-risk column is introduced. Each
    branch deposit is cash or, occasionally, c_check (Rio's call,
    2026-07-15 - both are cash-equivalent instruments, so a mix across the
    branches is realistic, not just cash everywhere). The bent_pool itself
    stays unfiltered (ATMs included) - banks treat an ATM as a mini-branch
    for this rule, confirmed as the correct reading, not the possible
    inconsistency an earlier pass here had flagged.
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

        if random.random() < 0.25:
            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=None,
                tgt=account,
                amount=amount,
                currency="USD",
                payment_type="c_check",
                is_laundering=True,
                known_accounts=known_accounts,
                post_date=post_date,
                source_description=f"CASHIER'S CHECK - Deposit at {branch.get('address')}",
            )
        else:
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
