import math
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
    get_bent_type,
    is_us_federal_holiday,
    generate_synthetic_bic,
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

# Archetypes whose card/debit/pos revenue is generated as aggregated daily
# batch-settlement deposits (generate_daily_card_settlement) instead of one
# row per walk-in sale - real per-ticket amounts are too small (restaurant
# ~$29-34, retail ~$81 per Fed/consumer-payments research) to generate
# individually without an unrealistic explosion in row count; a real
# merchant's bank statement shows the payment processor's daily batch
# anyway, not each swipe. Rio's call, 2026-07-14.
DAILY_SETTLEMENT_ARCHETYPES = {"RESTAURANT", "RETAIL", "GAS_STATION"}

# Archetypes with genuine (legitimate, non-injected) international wire
# activity, and what share of their wire volume goes to a real ordinary
# trading-partner country (config/trade_countries.yaml) rather than
# domestic - so an inject_cty high-risk-country wire isn't the only
# international activity in the dataset. DRAFT probabilities, Rio's call
# 2026-07-15: IMPORT_EXPORT is majority-international by definition;
# MANUFACTURING/WHOLESALE_DISTRIBUTION occasionally source internationally
# but stay majority domestic.
INTERNATIONAL_TRADE_ARCHETYPES = {
    "IMPORT_EXPORT": 0.60,
    "MANUFACTURING": 0.20,
    "WHOLESALE_DISTRIBUTION": 0.20,
}

_PAYMENT_RANGES_PATH = os.path.join("config", "payment_ranges.yaml")
with open(_PAYMENT_RANGES_PATH, "r") as _f:
    PAYMENT_RANGES = yaml.safe_load(_f)

_TRADE_COUNTRIES_PATH = os.path.join("config", "trade_countries.yaml")
with open(_TRADE_COUNTRIES_PATH, "r") as _f:
    TRADE_COUNTRIES = yaml.safe_load(_f)["countries"]


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
    """Map an entity_id to a company archetype (RESTAURANT, RETAIL, ...)
    via config/payment_ranges.yaml's company_segments. Returns None for a
    company not in the mapping (e.g. a future roster addition not yet
    classified)."""
    return _COMPANY_SEGMENT_LOOKUP.get(entity_id)


def get_company_size_tier(transaction_scaler) -> str:
    """Map a company's transaction_scaler to a small/mid/large size tier -
    used for wire/check ranges (config/payment_ranges.yaml's
    size_tiered_payment_ranges) and the formulaic revenue-target
    calculation. Size (not archetype) drives a wire/check's realistic
    range - a small business's wire floor is lower than a large one's,
    regardless of industry. Breakpoints chosen off the actual roster's
    transaction_scaler distribution, 2026-07-14: <=0.75 (13 companies
    today), <=1.5 (17), else large (4 - Spice Flow GmbH, Charmont Paper
    Products, Graham-Potts, Rio Titan)."""
    scaler = float(transaction_scaler or 1)
    if scaler <= 0.75:
        return "small"
    if scaler <= 1.5:
        return "mid"
    return "large"


def get_company_owners(owners_str) -> list[tuple[str, float]]:
    """Parse a company's `owners` cell ("PERS1002:60,PERS1008:40") into
    [(person_entity_id, pct), ...]. Returns [] for an unowned company
    (most companies - only ~18 of 34 have a roster owner, see
    scripts/assign_business_ownership.py). Percentages are whatever's in
    the data (should sum to 100 for a fully-owned company) - not
    re-normalized here."""
    if not isinstance(owners_str, str) or not owners_str.strip():
        return []
    pairs = []
    for chunk in owners_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        person_id, _, pct = chunk.partition(":")
        pairs.append((person_id.strip(), float(pct) if pct else 100.0))
    return pairs


def pick_payment_type_for_amount(payment_types, amount, segment, segment_ranges, weights=None):
    """Choose a payment_type from ``payment_types`` whose typical range (for
    ``segment``, looked up in ``segment_ranges``) actually contains
    ``amount`` - the fix for payment_type and amount being picked
    independently (e.g. a $30,000 purchase rolling as P2P). A [0, 0] range
    means "not realistic for this segment" and is always excluded, even if
    the counterparty's accepted_payment_methods lists it.

    ``weights`` (optional, e.g. payment_ranges.yaml's revenue_mix_weights
    for a company archetype) biases the choice among whichever types are
    still amount-eligible, instead of picking uniformly among them - a
    restaurant's revenue genuinely skews toward card+cash, not an even
    split across every accepted rail. Types with no weight entry are
    excluded when weights are given.

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
        if weights:
            weighted = [pt for pt in eligible if weights.get(pt)]
            if weighted:
                return random.choices(weighted, weights=[weights[pt] for pt in weighted], k=1)[0]
        return random.choice(eligible)

    # Cash has a real physical ceiling a purchase amount doesn't - a
    # business can't plausibly hand over $25,000 in cash for a routine
    # transaction the way it could send a $25,000 wire. Exclude cash from
    # the "closest fallback" pool whenever a non-cash option is on offer,
    # so the fallback picks a rail that's actually capable of carrying a
    # large amount instead of a wildly-out-of-range cash figure. Only
    # falls back to cash if it's the sole type available at all.
    candidates = [(pt, bounds(pt)) for pt in payment_types if bounds(pt) and pt != "cash"]
    if not candidates:
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


def compute_check_clearing_dates(payment_type: str, debit_ts_dt: datetime) -> tuple[datetime, datetime]:
    """A check's credit leg (the payee depositing it) posts days after the
    debit leg (the payer writing/clearing it) - config/payment_ranges.yaml's
    check_clearing_float. generate_post_date's own business-day/holiday-aware
    lag then layers on top, anchored to this later date rather than the
    debit date."""
    lo, hi = PAYMENT_RANGES["check_clearing_float"][payment_type.lower()]
    credit_ts_dt = debit_ts_dt + timedelta(days=random.randint(lo, hi))
    return credit_ts_dt, generate_post_date(credit_ts_dt)


def choose_cash_withdrawal(payer_acct, payer_bank, bents_by_bank, amount, segment, date_key, atm_daily_totals):
    """Pick a withdrawal source (ATM or branch/teller) and the resulting
    amount for a cash-paying purchase of ``amount``.

    Real ATMs dispense in $20 multiples (no change) and cap daily
    withdrawals per account; branch/teller cash is exact-amount and
    uncapped - a mechanically different transaction, not just a different
    location. Rounds up to the next $20 so the withdrawal always covers at
    least the purchase price. If that would exceed the account's daily ATM
    cap for ``date_key`` (config/payment_ranges.yaml's atm_daily_limit,
    keyed by P1-P6 person segment), the withdrawal routes to a branch/
    teller instead - uncapped, and left at the original exact amount.

    Returns (final_amount, bent_id, bent_location).
    """
    bents = bents_by_bank.get(payer_bank, [])
    atm_bents = [b for b in bents if get_bent_type(b.get("name")) == "atm"]
    branch_bents = [b for b in bents if get_bent_type(b.get("name")) != "atm"]

    def pick(pool):
        if pool:
            rec = random.choice(pool)
            return rec.get("name"), rec.get("address")
        return generate_uuid(8), fake.address().replace("\n", ", ")

    daily_cap = PAYMENT_RANGES["atm_daily_limit"].get(segment) if segment else None
    rounded = math.ceil(amount / 20) * 20 if amount > 0 else 0

    if atm_bents and daily_cap is not None:
        key = (payer_acct.id, date_key)
        so_far = atm_daily_totals.get(key, 0)
        if so_far + rounded <= daily_cap:
            atm_daily_totals[key] = so_far + rounded
            bent_id, bent_loc = pick(atm_bents)
            return rounded, bent_id, bent_loc

    # Over the ATM cap, no ATM available, or no segment to look up a cap
    # for (e.g. a company payer) - branch/teller cash, exact amount.
    bent_id, bent_loc = pick(branch_bents or bents)
    return amount, bent_id, bent_loc


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


def get_monthly_dates(start_dt: datetime, end_dt: datetime, day_of_month: int) -> list[datetime]:
    """Return one date per month within range, on ``day_of_month`` (clamped
    to the last day of a shorter month). Used for fixed_recurring bills
    (rent/mortgage, utilities) that fall on the same day every period."""
    dates = []
    current = start_dt.replace(day=1)
    while current <= end_dt:
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1, day=1)
        else:
            next_month = current.replace(month=current.month + 1, day=1)
        last_day = (next_month - timedelta(days=1)).day
        due = current.replace(day=min(day_of_month, last_day))
        if start_dt <= due <= end_dt:
            dates.append(due)
        current = next_month
    return dates


def get_weekly_dates(start_dt: datetime, end_dt: datetime, day_of_week: int) -> list[datetime]:
    """Return one date per week within range, on ``day_of_week`` (0=Monday,
    per datetime.weekday()). Used for weekly business cash till deposits."""
    dates = []
    current = start_dt + timedelta(days=(day_of_week - start_dt.weekday()) % 7)
    while current <= end_dt:
        dates.append(current)
        current += timedelta(days=7)
    return dates


def get_business_days(start_dt: datetime, end_dt: datetime) -> list[datetime]:
    """Return every weekday within range that isn't a US federal holiday -
    used by generate_daily_card_settlement, since a card processor doesn't
    settle on a day the bank itself is closed."""
    days = []
    current = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= end:
        if current.weekday() < 5 and not is_us_federal_holiday(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def maybe_internationalize_wire_counterparty(counterparty, segment: str, payment_type: str):
    """For IMPORT_EXPORT/MANUFACTURING/WHOLESALE_DISTRIBUTION wires only
    (INTERNATIONAL_TRADE_ARCHETYPES), roll that archetype's international-
    share probability and, if it hits, turn a synthetic counterparty into
    a real-country international one - sets .country_code/.swift_code and
    rewrites owner_name/address to reflect the country, same naming
    convention rule_injectors.py's inject_cty already uses for its
    high-risk-country counterparties. International wire counterparties
    are always businesses, never individuals. No-op (returns counterparty
    unchanged) for every other archetype/payment_type - this exists so
    genuine international wire activity isn't limited to the injected CTY
    typology (Rio's call, 2026-07-15)."""
    intl_share = INTERNATIONAL_TRADE_ARCHETYPES.get(segment)
    if payment_type.lower() != "wire" or not intl_share or random.random() >= intl_share:
        return counterparty
    country = random.choice(TRADE_COUNTRIES)
    counterparty.owner_type = "Company"
    counterparty.owner_name = f"{fake.company()} ({country['name']})"
    counterparty.address = f"{fake.street_address()}, {country['name']}"
    counterparty.country_code = country["country_code"]
    counterparty.swift_code = generate_synthetic_bic(country["country_code"])
    return counterparty


def generate_daily_card_settlement(
    companies_df: pd.DataFrame,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Aggregated daily card-batch settlement deposits for
    DAILY_SETTLEMENT_ARCHETYPES (RESTAURANT/RETAIL/GAS_STATION) - one
    deposit per active card rail (ccard/debit/pos) per business day, sized
    to that rail's share of the company's target daily revenue, instead of
    one row per individual walk-in sale. This is both more realistic (a
    merchant's own bank statement shows the processor's daily batch, not
    each $30 swipe) and avoids a 15-50x row-count explosion from generating
    real per-ticket amounts ($8-150) at real per-business volume
    ($50,000-$110,000+/month). generate_company_revenue_transactions still
    handles this same company's ach/check/cash/p2p revenue separately (see
    its per_txn_weights filtering) - this function only ever emits
    ccard/credit/debit/pos.

    Target daily revenue is reconstructed from the same two fields
    generate_company_revenue_transactions and generate_restocking_
    transactions already use: revenue_monthly_target (a transaction COUNT)
    x average_expense (now the archetype/tier's blended ticket, not a
    literal amount-drawing band - see that function's docstring) = monthly
    revenue in dollars, spread evenly across the month's business days.
    """
    card_types = ("ccard", "credit", "debit", "pos")

    for _, company in companies_df.iterrows():
        segment = get_company_segment(company.get("entity_id"))
        if segment not in DAILY_SETTLEMENT_ARCHETYPES:
            continue

        revenue_target = company.get("revenue_monthly_target")
        avg_rev = company.get("average_expense")
        if pd.isna(revenue_target) or pd.isna(avg_rev):
            continue
        monthly_revenue_dollars = float(revenue_target) * float(avg_rev)

        weights = PAYMENT_RANGES["revenue_mix_weights"].get(segment, {})
        card_weights = {pt: w for pt, w in weights.items() if pt in card_types}
        total_card_weight = sum(card_weights.values())
        if not card_weights or not total_card_weight:
            continue
        # Card rails only get their own share of the company's total
        # target revenue (total_card_weight, e.g. ~0.90 for RESTAURANT) -
        # the rest (ach/check/cash/p2p) is generate_company_revenue_
        # transactions' per_txn_weights' share, generated separately there.
        monthly_revenue_dollars *= total_card_weight

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

        business_days = get_business_days(start_dt, end_dt)
        if not business_days:
            continue
        # Business days per calendar month varies (~20-23) - spread the
        # month's target evenly across this range's actual business days
        # rather than assuming a fixed count.
        months_in_range = max(1.0, (end_dt - start_dt).days / 30.0)
        avg_daily_revenue_dollars = (monthly_revenue_dollars * months_in_range) / len(business_days)

        for day in business_days:
            # +/-30% day-to-day variance around the monthly average -
            # real daily settlement totals fluctuate (busier days,
            # slower days), they don't repeat the identical figure every
            # business day. Wider than the weekly cash-sweep's +/-15%
            # since daily fluctuation is naturally larger than
            # week-to-week. Rio's call, 2026-07-15.
            daily_revenue_dollars = avg_daily_revenue_dollars * random.uniform(0.7, 1.3)
            for payment_type, weight in card_weights.items():
                amount = round(daily_revenue_dollars * (weight / total_card_weight), 2)
                if amount <= 0:
                    continue
                ts_dt = generate_transaction_timestamp(
                    day.replace(hour=8), day.replace(hour=20), entity_type="Company",
                )
                timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

                counterparty = ProfileAccount(
                    id=generate_uuid(),
                    owner_id=generate_uuid(8),
                    owner_type="Company",
                    owner_name=f"{fake.company()} Card Processing",
                    address=fake.address().replace("\n", ", ")
                )
                entries = split_transaction(
                    txn_id=generate_uuid(),
                    timestamp=timestamp,
                    src=counterparty,
                    tgt=comp_acct,
                    amount=amount,
                    currency="USD",
                    payment_type=payment_type,
                    is_laundering=False,
                    source_description=f"{payment_type.upper()} - Daily Settlement",
                    known_accounts=known_accounts,
                    post_date=post_date,
                )
                if not register(entries):
                    return


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

        company_segment = get_company_segment(company.get("entity_id"))
        revenue_weights = PAYMENT_RANGES["revenue_mix_weights"].get(company_segment)

        if revenue_weights:
            # The archetype's revenue_mix_weights is a more specific,
            # more current statement of "what rails does this business
            # realistically receive revenue on" than accepted_payment_methods,
            # which predates the archetype system and - for several
            # companies migrated from the original Actimize roster - never
            # lists cards or cash at all, even for a restaurant/retail/gas
            # station that obviously takes both from walk-in customers.
            payment_types = list(revenue_weights.keys())
        else:
            pay_opts = company.get("accepted_payment_methods")
            if isinstance(pay_opts, str) and pay_opts.strip():
                payment_types = [p.strip().lower() for p in pay_opts.split(',') if p.strip()]
            else:
                payment_types = PAYMENT_TYPES

        avg_rev = company.get("average_expense")
        avg_rev = 500.0 if pd.isna(avg_rev) else float(avg_rev)
        size_tier = get_company_size_tier(company.get("transaction_scaler"))
        archetype_ranges = PAYMENT_RANGES["payment_type_ranges"]["companies"].get(company_segment, {})

        # Per-transaction revenue weights, for archetypes with a
        # daily-settlement mechanic (generate_daily_card_settlement,
        # RESTAURANT/RETAIL/GAS_STATION): ccard/credit/debit/pos are
        # excluded here since that function generates them as aggregated
        # daily batch deposits instead - this loop only handles the
        # residual rails (ach/check/cash/p2p) for those archetypes.
        per_txn_weights = revenue_weights
        if revenue_weights and company_segment in DAILY_SETTLEMENT_ARCHETYPES:
            per_txn_weights = {
                pt: w for pt, w in revenue_weights.items()
                if pt not in ("ccard", "credit", "debit", "pos")
            }

        # revenue_monthly_target's count reflects ALL rails (card included,
        # since it's also used by generate_daily_card_settlement to
        # reconstruct total monthly revenue in dollars). This loop only
        # generates the per_txn_weights subset, so its own transaction
        # count needs scaling down to that subset's share of the total
        # weight - otherwise a restaurant's ~10% non-card weight would
        # still generate a full restaurant's worth of individual cash/ach/
        # check transactions instead of a small residual.
        weight_fraction = sum(per_txn_weights.values()) if per_txn_weights else 1.0
        num_txns = max(1, int(round(float(revenue_target) * months_in_range * weight_fraction)))

        for _ in range(num_txns):
            # 2026-07-14: payment_type is now chosen FIRST (weighted
            # directly from revenue_mix_weights, unconditioned on any
            # amount), and the amount is drawn from THAT type's own range
            # second. The old amount-first-then-eligible-type order could
            # never work for wire/check-heavy archetypes (REAL_ESTATE,
            # FINANCIAL_SERVICES, IMPORT_EXPORT, etc.) - a single +/-30%
            # band around one avg_rev value can't simultaneously fall
            # inside both a $50 card range and a $10,000+ wire range, so
            # wire was silently almost-dead in those archetypes despite
            # carrying majority weight. Only applies when revenue_weights
            # exists (the archetype is classified) - the accepted_payment_
            # methods fallback below keeps the old amount-then-type logic,
            # since there's no weights concept to choose a type from first.
            if per_txn_weights:
                payment_type = random.choices(
                    list(per_txn_weights.keys()), weights=list(per_txn_weights.values()), k=1
                )[0]
                # Size-tiered wire/check ranges only apply where that rail
                # is a genuine primary revenue channel (the 8 B2B
                # archetypes) - for RESTAURANT/RETAIL/GAS_STATION, check is
                # a rare incidental edge case (an occasional catering/
                # private-event invoice), not scaled to the business's
                # overall size the way a B2B company's routine invoicing
                # is. Using the size-tiered range there let a handful of
                # $2,500-$40,000 "mid-tier" checks dwarf a restaurant's
                # entire card-dominant revenue - use the archetype's own
                # modest range instead.
                if payment_type in ("wire", "check") and company_segment not in DAILY_SETTLEMENT_ARCHETYPES:
                    lo, hi = PAYMENT_RANGES["size_tiered_payment_ranges"][payment_type][size_tier]
                else:
                    lo, hi = archetype_ranges.get(payment_type, (avg_rev * 0.7, avg_rev * 1.3))
                amount = round(random.uniform(lo, hi), 2)
            else:
                amount = round(random.uniform(avg_rev * 0.7, avg_rev * 1.3), 2)
                payment_type = pick_payment_type_for_amount(
                    payment_types, amount, company_segment,
                    PAYMENT_RANGES["payment_type_ranges"]["companies"],
                    weights=revenue_weights,
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

                # A business deposits bulk till cash over the counter, not
                # through an ATM - branch-only, same reasoning as
                # choose_cash_withdrawal's person-side ATM/branch split.
                bank = str(company.get("bank"))
                branch_bents = [b for b in bents_by_bank.get(bank, []) if get_bent_type(b.get("name")) != "atm"]
                if branch_bents:
                    bent = random.choice(branch_bents)
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
                counterparty = maybe_internationalize_wire_counterparty(counterparty, company_segment, payment_type)
                credit_timestamp = credit_post_date = None
                if payment_type.lower() in ("check", "c_check"):
                    credit_ts_dt, credit_pd_dt = compute_check_clearing_dates(payment_type, ts_dt)
                    credit_timestamp = credit_ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                    credit_post_date = credit_pd_dt.strftime("%Y-%m-%d %H:%M:%S")

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
                    post_date=post_date,
                    credit_timestamp=credit_timestamp,
                    credit_post_date=credit_post_date,
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
                credit_timestamp = credit_post_date = None
                if payment_type.lower() in ("check", "c_check"):
                    credit_ts_dt, credit_pd_dt = compute_check_clearing_dates(payment_type, ts_dt)
                    credit_timestamp = credit_ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                    credit_post_date = credit_pd_dt.strftime("%Y-%m-%d %H:%M:%S")
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
                    post_date=post_date,
                    credit_timestamp=credit_timestamp,
                    credit_post_date=credit_post_date,
                )
            if not register(entries):
                return


def generate_rent_transactions(
    profile_df: pd.DataFrame,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Fixed monthly rent/mortgage for every sampled person and company -
    config/transaction_categories.yaml's fixed_recurring category. Amount
    and day-of-month are each drawn once per profile and reused every
    period, to a synthesized landlord/lender counterparty (same untracked-
    counterparty pattern as company revenue and self-employment income).
    """
    payers = profile_df[profile_df["type"].isin(["person", "company"])]
    rent_table = PAYMENT_RANGES["rent_monthly"]

    for _, payer in payers.iterrows():
        is_person = payer["type"] == "person"
        if is_person:
            segment = get_person_segment(payer.get("income_level"), payer.get("employment_status"))
            base_range = rent_table["persons"].get(segment)
            scaler = 1.0
        else:
            segment = get_company_segment(payer.get("entity_id"))
            base_range = rent_table["companies"].get(segment)
            scaler = float(payer.get("transaction_scaler") or 1)
        if not base_range:
            continue

        amount = round(random.uniform(base_range[0], base_range[1]) * scaler, 2)
        day_of_month = random.randint(1, 28)
        due_dates = get_monthly_dates(start_dt, end_dt, day_of_month)

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
        landlord = ProfileAccount(
            id=generate_uuid(),
            owner_id=generate_uuid(8),
            owner_type="Company",
            owner_name=fake.company(),
            address=fake.address().replace("\n", ", ")
        )
        rent_mix = PAYMENT_RANGES["rent_payment_mix"]["persons" if is_person else "companies"]
        payment_type = random.choices(list(rent_mix.keys()), weights=list(rent_mix.values()), k=1)[0]

        for due in due_dates:
            pay_start = due.replace(hour=8, minute=0, second=0, microsecond=0)
            pay_end = due.replace(hour=16, minute=59, second=59, microsecond=0)
            ts_dt = generate_transaction_timestamp(pay_start, pay_end, entity_type=payer_acct.owner_type)
            timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

            credit_timestamp = credit_post_date = None
            if payment_type.lower() in ("check", "c_check"):
                credit_ts_dt, credit_pd_dt = compute_check_clearing_dates(payment_type, ts_dt)
                credit_timestamp = credit_ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                credit_post_date = credit_pd_dt.strftime("%Y-%m-%d %H:%M:%S")

            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=payer_acct,
                tgt=landlord,
                amount=amount,
                currency="USD",
                payment_type=payment_type,
                is_laundering=False,
                source_description=describe_transaction(payment_type, "Rent/Mortgage"),
                known_accounts=known_accounts,
                post_date=post_date,
                credit_timestamp=credit_timestamp,
                credit_post_date=credit_post_date,
            )
            if not register(entries):
                return


def generate_utilities_transactions(
    profile_df: pd.DataFrame,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Monthly electricity/phone/internet bills for every sampled person
    and company - three separate line items, each with its own fixed
    per-profile baseline and small month-to-month variance
    (variable_recurring, config/transaction_categories.yaml), paid to a
    synthesized utility-provider counterparty.
    """
    payers = profile_df[profile_df["type"].isin(["person", "company"])]
    util_table = PAYMENT_RANGES["utilities_monthly"]

    for _, payer in payers.iterrows():
        is_person = payer["type"] == "person"
        if is_person:
            segment = get_person_segment(payer.get("income_level"), payer.get("employment_status"))
            bills = util_table["persons"].get(segment)
            scaler = 1.0
        else:
            segment = get_company_segment(payer.get("entity_id"))
            bills = util_table["companies"].get(segment)
            scaler = float(payer.get("transaction_scaler") or 1)
        if not bills:
            continue

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

        for bill_name, bill_range in bills.items():
            baseline = random.uniform(bill_range[0], bill_range[1]) * scaler
            day_of_month = random.randint(1, 28)
            provider = ProfileAccount(
                id=generate_uuid(),
                owner_id=generate_uuid(8),
                owner_type="Company",
                owner_name=fake.company(),
                address=fake.address().replace("\n", ", ")
            )

            for due in get_monthly_dates(start_dt, end_dt, day_of_month):
                amount = round(baseline * random.uniform(0.85, 1.15), 2)
                pay_start = due.replace(hour=8, minute=0, second=0, microsecond=0)
                pay_end = due.replace(hour=16, minute=59, second=59, microsecond=0)
                ts_dt = generate_transaction_timestamp(pay_start, pay_end, entity_type=payer_acct.owner_type)
                timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

                entries = split_transaction(
                    txn_id=generate_uuid(),
                    timestamp=timestamp,
                    src=payer_acct,
                    tgt=provider,
                    amount=amount,
                    currency="USD",
                    payment_type="ach",
                    is_laundering=False,
                    source_description=describe_transaction("ach", f"Utility - {bill_name.capitalize()}"),
                    known_accounts=known_accounts,
                    post_date=post_date
                )
                if not register(entries):
                    return


def generate_restocking_transactions(
    companies_df: pd.DataFrame,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
    months_in_range: float,
) -> None:
    """Inventory/COGS restocking for companies whose archetype has a
    meaningful cost of goods sold (config/payment_ranges.yaml's
    restocking_pct_of_revenue) - sized as a percentage of the company's
    actual monthly revenue in dollars, split into payments at the
    archetype's typical frequency, to a synthesized supplier counterparty.
    Archetypes with no real restocking (professional/real-estate/
    financial services) are simply absent from restocking_pct_of_revenue
    and skipped.

    revenue_monthly_target is a transaction *frequency* (revenue txns/
    month), the same way merchant_frequency is - not a dollar figure.
    Actual monthly revenue in dollars is revenue_monthly_target x
    average_expense, the same math generate_company_revenue_transactions
    uses to size individual revenue transactions.
    """
    restock_table = PAYMENT_RANGES["restocking_pct_of_revenue"]
    freq_per_month = {"weekly": 4, "biweekly": 2, "monthly": 1}

    for _, company in companies_df.iterrows():
        segment = get_company_segment(company.get("entity_id"))
        spec = restock_table.get(segment)
        revenue_freq = company.get("revenue_monthly_target")
        avg_rev = company.get("average_expense")
        if not spec or pd.isna(revenue_freq) or pd.isna(avg_rev):
            continue

        monthly_revenue_dollars = float(revenue_freq) * float(avg_rev)
        pct = random.uniform(spec["pct"][0], spec["pct"][1])
        monthly_cogs = monthly_revenue_dollars * pct
        num_payments = max(1, int(round(freq_per_month[spec["frequency"]] * months_in_range)))
        amount_per_payment = round(monthly_cogs * months_in_range / num_payments, 2)

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
        supplier = ProfileAccount(
            id=generate_uuid(),
            owner_id=generate_uuid(8),
            owner_type="Company",
            owner_name=fake.company(),
            address=fake.address().replace("\n", ", ")
        )

        for _ in range(num_payments):
            payment_type = random.choice(spec["payment_types"])
            ts_dt = generate_transaction_timestamp(start_dt, end_dt, entity_type="Company")
            timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

            # supplier is a synthetic, non-known counterparty (same pattern
            # as landlord/utility provider), so only the debit leg ever
            # actually gets emitted - credit_timestamp is a no-op today but
            # kept for consistency/future-proofing if that ever changes.
            credit_timestamp = credit_post_date = None
            if payment_type.lower() in ("check", "c_check"):
                credit_ts_dt, credit_pd_dt = compute_check_clearing_dates(payment_type, ts_dt)
                credit_timestamp = credit_ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                credit_post_date = credit_pd_dt.strftime("%Y-%m-%d %H:%M:%S")

            # supplier is shared/reused across every payment this company
            # makes (its identity shouldn't shuffle payment to payment) -
            # a fresh copy is internationalized per-payment instead of
            # mutating the shared instance, since payment_type (and so
            # whether this specific payment qualifies) varies each time.
            payment_supplier = supplier
            if payment_type.lower() == "wire":
                payment_supplier = ProfileAccount(
                    id=supplier.id, owner_id=supplier.owner_id, owner_type=supplier.owner_type,
                    owner_name=supplier.owner_name, address=supplier.address,
                )
                payment_supplier = maybe_internationalize_wire_counterparty(payment_supplier, segment, payment_type)

            entries = split_transaction(
                txn_id=generate_uuid(),
                timestamp=timestamp,
                src=comp_acct,
                tgt=payment_supplier,
                amount=amount_per_payment,
                currency="USD",
                payment_type=payment_type,
                is_laundering=False,
                source_description=describe_transaction(payment_type, "Restocking/Supplier"),
                known_accounts=known_accounts,
                post_date=post_date,
                credit_timestamp=credit_timestamp,
                credit_post_date=credit_post_date,
            )
            if not register(entries):
                return


def generate_business_cash_deposits(
    companies_df: pd.DataFrame,
    bents_by_bank: dict,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Weekly OTC bulk till/register cash for companies in the cash-heavy
    archetypes (config/payment_ranges.yaml's business_cash_deposit_weekly/
    business_cash_withdrawal_weekly) - a deposit (the week's accumulated
    cash sales going to the bank) and a smaller separate withdrawal
    (restocking the register with change). Always branch-sourced, never
    ATM, and during business hours - a real teller isn't available 24/7
    the way an ATM is. Additional to, not instead of, the smaller
    per-transaction cash entries in generate_company_revenue_transactions's
    revenue mix - that weight was lowered accordingly (see
    revenue_mix_weights' comment) so the same till cash isn't counted
    twice.
    """
    pct_table = PAYMENT_RANGES["business_cash_deposit_pct_of_revenue"]
    withdrawal_table = PAYMENT_RANGES["business_cash_withdrawal_weekly"]

    for _, company in companies_df.iterrows():
        segment = get_company_segment(company.get("entity_id"))
        pct_range = pct_table.get(segment)
        if not pct_range:
            continue
        revenue_target = company.get("revenue_monthly_target")
        avg_rev = company.get("average_expense")
        if pd.isna(revenue_target) or pd.isna(avg_rev):
            continue
        monthly_revenue_dollars = float(revenue_target) * float(avg_rev)
        weekly_base = monthly_revenue_dollars * random.uniform(*pct_range) / 4.33
        scaler = float(company.get("transaction_scaler") or 1)
        # weekly_cash_leg() below multiplies by scaler again (needed for
        # withdrawal_range, which isn't revenue-derived) - monthly_revenue_
        # dollars already reflects this company's scaler (baked into
        # revenue_monthly_target/average_expense by
        # scripts/recalibrate_company_revenue.py), so pre-divide here to
        # avoid applying it twice.
        deposit_range = (weekly_base * 0.85 / scaler, weekly_base * 1.15 / scaler)
        withdrawal_range = withdrawal_table.get(segment)

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
        bank = str(company.get("bank"))
        branch_bents = [b for b in bents_by_bank.get(bank, []) if get_bent_type(b.get("name")) != "atm"]

        def pick_branch():
            if branch_bents:
                b = random.choice(branch_bents)
                return b.get("name"), b.get("address")
            return generate_uuid(8), fake.address().replace("\n", ", ")

        def weekly_cash_leg(amount_range, src, tgt):
            day_of_week = random.randint(0, 6)
            for due in get_weekly_dates(start_dt, end_dt, day_of_week):
                day_start = due.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = due.replace(hour=23, minute=59, second=59, microsecond=0)
                ts_dt = generate_transaction_timestamp(day_start, day_end, entity_type="Company")
                timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
                bent_id, bent_loc = pick_branch()
                amount = round(random.uniform(amount_range[0], amount_range[1]) * scaler, 2)

                entries = split_transaction(
                    txn_id=generate_uuid(),
                    timestamp=timestamp,
                    src=src,
                    tgt=tgt,
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
                    return False
            return True

        if not weekly_cash_leg(deposit_range, None, comp_acct):
            return
        if withdrawal_range and not weekly_cash_leg(withdrawal_range, comp_acct, None):
            return


def generate_large_personal_withdrawals(
    profile_df: pd.DataFrame,
    bents_by_bank: dict,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """An occasional, one-off large OTC cash withdrawal per person - savings
    drawn down for a big purchase (e.g. a used car), not tied to any
    specific merchant transaction. At most one per person per run
    (config/payment_ranges.yaml's large_personal_withdrawal.probability).
    Always branch-sourced - well beyond any ATM daily cap anyway, but
    explicit rather than relying on choose_cash_withdrawal's cap-overflow
    fallback.
    """
    spec = PAYMENT_RANGES["large_personal_withdrawal"]
    persons = profile_df[profile_df["type"] == "person"]

    for _, person in persons.iterrows():
        if random.random() >= spec["probability"]:
            continue
        segment = get_person_segment(person.get("income_level"), person.get("employment_status"))
        amount_range = spec["amount"].get(segment)
        if not amount_range:
            continue

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
        bank = str(person.get("bank"))
        branch_bents = [b for b in bents_by_bank.get(bank, []) if get_bent_type(b.get("name")) != "atm"]
        if branch_bents:
            bent = random.choice(branch_bents)
            bent_id = bent.get("name")
            bent_loc = bent.get("address")
        else:
            bent_id = generate_uuid(8)
            bent_loc = fake.address().replace("\n", ", ")

        amount = round(random.uniform(amount_range[0], amount_range[1]), 2)
        ts_dt = generate_transaction_timestamp(start_dt, end_dt, entity_type="Person")
        timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

        entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=person_acct,
            tgt=None,
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


def build_bank_gl_accounts(profile_df: pd.DataFrame) -> dict[str, "ProfileAccount"]:
    """One synthetic per-bank cashier's-check suspense GL account, keyed by
    bank code (str, same keying as bents_by_bank) - used by
    generate_c_check_gl_transactions. Unlike every other synthetic
    counterparty in this module (landlord, utility provider, supplier),
    this one needs to actually be a *known* account so its deposit leg gets
    emitted - the caller is responsible for adding its .id into
    known_accounts."""
    banks = profile_df[profile_df["type"] == "bank"]
    gl_accounts: dict[str, ProfileAccount] = {}
    for _, bank_row in banks.iterrows():
        bank_code = str(bank_row["bank"])
        bank_name = bank_row.get("name", "")
        gl_id = f"GL-CCHECK-{bank_code}"
        gl_accounts[bank_code] = ProfileAccount(
            id=gl_id,
            owner_id=gl_id,
            owner_type="Company",
            owner_name=f"{bank_name} Cashier's Check Suspense Account",
            bank_name=bank_name,
        )
    return gl_accounts


def generate_c_check_gl_transactions(
    profile_df: pd.DataFrame,
    bents_by_bank: dict,
    gl_accounts_by_bank: dict,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Legitimate (is_laundering=False), not a typology injection: models a
    teller processing a cashier's check purchase as a cash withdrawal from
    the customer's account funding a deposit into the bank's own c_check
    GL/suspense account - two separate transactions, same visit, no shared
    reference field (Rio's call, 2026-07-11: correlate by amount/timing,
    same as real casework). The withdrawal leg is schema-identical to any
    other cash withdrawal, which is the point - it can legitimately look
    like a cash-monitoring trigger until the offsetting c_check deposit is
    found. At most one per person per run
    (config/payment_ranges.yaml's c_check_gl_purchase.probability). Always
    branch-sourced, same teller visit for both legs (no clearing float -
    that's for a check moving between two external parties over days,
    which doesn't apply to an instant internal GL posting).
    """
    spec = PAYMENT_RANGES["c_check_gl_purchase"]
    persons = profile_df[profile_df["type"] == "person"]

    for _, person in persons.iterrows():
        if random.random() >= spec["probability"]:
            continue
        segment = get_person_segment(person.get("income_level"), person.get("employment_status"))
        amount_range = spec["amount"].get(segment)
        if not amount_range:
            continue

        bank = str(person.get("bank"))
        gl_account = gl_accounts_by_bank.get(bank)
        if gl_account is None:
            continue

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
        branch_bents = [b for b in bents_by_bank.get(bank, []) if get_bent_type(b.get("name")) != "atm"]
        if branch_bents:
            bent = random.choice(branch_bents)
            bent_id = bent.get("name")
            bent_loc = bent.get("address")
        else:
            bent_id = generate_uuid(8)
            bent_loc = fake.address().replace("\n", ", ")

        amount = round(random.uniform(amount_range[0], amount_range[1]), 2)
        ts_dt = generate_transaction_timestamp(start_dt, end_dt, entity_type="Person")
        timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")

        withdrawal_entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=person_acct,
            tgt=None,
            amount=amount,
            currency="USD",
            payment_type="cash",
            is_laundering=False,
            known_accounts=known_accounts,
            post_date=post_date,
            atm_id=bent_id,
            atm_location=bent_loc,
        )
        if not register(withdrawal_entries):
            return

        gl_entries = split_transaction(
            txn_id=generate_uuid(),
            timestamp=timestamp,
            src=None,
            tgt=gl_account,
            amount=amount,
            currency="USD",
            payment_type="c_check",
            is_laundering=False,
            source_description="CASHIER'S CHECK - GL Funding - Customer Withdrawal",
            known_accounts=known_accounts,
            post_date=post_date,
        )
        if not register(gl_entries):
            return


def generate_owner_distribution_transactions(
    profile_df: pd.DataFrame,
    companies_df: pd.DataFrame,
    known_accounts: set,
    register,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """Monthly owner distributions for companies with an `owners` cell
    (scripts/assign_business_ownership.py, ~18 of 34 companies) - a
    percentage of actual monthly revenue in dollars
    (config/payment_ranges.yaml's owner_distribution_pct_of_revenue),
    split across joint owners by their ownership pct, paid from the
    company to each owner's own personal account. Additional to, not
    instead of, whatever salary/self-employment income an owner already
    has - an owner-operator commonly draws both.

    Requires the owner to actually be present in profile_df - main.py's
    close_relational_sample guarantees this for any sampled company with
    an owner, so this only silently skips a company here if it's called
    without that closure having run.
    """
    persons_by_id = profile_df[profile_df["type"] == "person"].set_index("entity_id")
    pct_lo, pct_hi = PAYMENT_RANGES["owner_distribution_pct_of_revenue"]

    for _, company in companies_df.iterrows():
        owners = get_company_owners(company.get("owners"))
        revenue_freq = company.get("revenue_monthly_target")
        avg_rev = company.get("average_expense")
        if not owners or pd.isna(revenue_freq) or pd.isna(avg_rev):
            continue

        monthly_revenue_dollars = float(revenue_freq) * float(avg_rev)
        distribution_pct = random.uniform(pct_lo, pct_hi)
        monthly_distribution = monthly_revenue_dollars * distribution_pct

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

        for person_id, pct in owners:
            if person_id not in persons_by_id.index:
                continue
            owner_row = persons_by_id.loc[person_id]
            owner_acct_id = owner_row.get("account_number")
            if pd.isna(owner_acct_id):
                owner_acct_id = person_id
            owner_acct = ProfileAccount(
                id=owner_acct_id,
                owner_id=person_id,
                owner_type="Person",
                owner_name=owner_row.get("name", ""),
                address=owner_row.get("address", "")
            )
            day_of_month = random.randint(1, 28)
            amount = round(monthly_distribution * (pct / 100.0), 2)

            for due in get_monthly_dates(start_dt, end_dt, day_of_month):
                pay_start = due.replace(hour=8, minute=0, second=0, microsecond=0)
                pay_end = due.replace(hour=16, minute=59, second=59, microsecond=0)
                ts_dt = generate_transaction_timestamp(pay_start, pay_end, entity_type="Company")
                timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                post_date = generate_post_date(ts_dt).strftime("%Y-%m-%d %H:%M:%S")
                payment_type = random.choice(["ach", "wire"])

                entries = split_transaction(
                    txn_id=generate_uuid(),
                    timestamp=timestamp,
                    src=comp_acct,
                    tgt=owner_acct,
                    amount=amount,
                    currency="USD",
                    payment_type=payment_type,
                    is_laundering=False,
                    source_description=describe_transaction(payment_type, "Owner Distribution"),
                    known_accounts=known_accounts,
                    post_date=post_date
                )
                if not register(entries):
                    return


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

    # Unlike every other synthetic counterparty in this function (landlord,
    # utility provider, supplier), the c_check GL account needs to be known
    # so its deposit leg actually gets emitted - see
    # generate_c_check_gl_transactions.
    gl_accounts_by_bank = build_bank_gl_accounts(profile_df)
    known_accounts.update(acct.id for acct in gl_accounts_by_bank.values())

    pending_deposits: dict[str, float] = {}
    # (account_id, "YYYY-MM-DD") -> cumulative ATM withdrawals that day -
    # see choose_cash_withdrawal().
    atm_daily_totals: dict[tuple[str, str], float] = {}

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
                # Data-quality backstop: a bad average_expense value on a
                # merchant row (e.g. a five/six-figure "average" walk-in
                # purchase) has no ceiling anywhere else in this loop, and
                # payment_type's own amount-fallback logic then routes the
                # resulting oversized purchase onto whichever rail has the
                # widest range (check/c_check) regardless of realism.
                # Capping the source value here is cheaper than chasing it
                # downstream at every payment-type range.
                avg_exp = min(float(avg_exp), PAYMENT_RANGES["max_merchant_average_expense"])
                amount = random.uniform(avg_exp * 0.85, avg_exp * 1.15)
                amount *= txn_scaler

                # Amount is drawn first (unchanged - still driven by the
                # merchant's average_expense x the payer's transaction_scaler)
                # - payment_type is then picked only from among the merchant's
                # accepted methods whose typical range (for the payer's own
                # P1-P6/C1-C3 segment) actually contains this amount, instead
                # of chosen independently of it. weights (expense_mix_weights,
                # persons only - keyed P1-P6, so a company payer_segment like
                # "RESTAURANT" simply won't match and this no-ops back to
                # uniform selection for company payers) biases the choice so
                # a real person's everyday purchases skew card/cash and check
                # stays rare, instead of picked as often as anything else
                # that happens to fit the amount.
                payment_type = pick_payment_type_for_amount(
                    payment_types, amount, payer_segment, payer_range_table,
                    weights=PAYMENT_RANGES["expense_mix_weights"].get(payer_segment),
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
                    # Withdrawal by payer via BEnt - ATM (rounded to the next
                    # $20, capped per day by the payer's segment) or branch/
                    # teller (exact amount, uncapped). See
                    # choose_cash_withdrawal()'s docstring.
                    payer_bank = str(payer.get("bank"))
                    amount, bent_id, bent_loc = choose_cash_withdrawal(
                        payer_acct, payer_bank, bents_by_bank, amount,
                        payer_segment, timestamp[:10], atm_daily_totals,
                    )

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
                    credit_timestamp = credit_post_date = None
                    if payment_type.lower() in ("check", "c_check"):
                        credit_ts_dt, credit_pd_dt = compute_check_clearing_dates(payment_type, ts_dt)
                        credit_timestamp = credit_ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                        credit_post_date = credit_pd_dt.strftime("%Y-%m-%d %H:%M:%S")

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
                        post_date=post_date,
                        credit_timestamp=credit_timestamp,
                        credit_post_date=credit_post_date,
                    )
                    if not register(entries):
                        break

    # Generate payroll transactions
    employees = profile_df[(profile_df["type"] == "person") & profile_df["employer"].notna()]
    companies = profile_df[profile_df["type"] == "company"].set_index("entity_id")
    payroll_dates = get_payroll_dates(start_dt, end_dt)

    # Biweekly amount by income_level/employment_status segment
    # (config/payment_ranges.yaml's payroll_biweekly) - replaces a flat
    # $5,000-base formula that ran 1.5-2.4x above BLS-grounded wages for
    # the Low/Medium tiers. Falls back to the Medium/P3 range if a segment
    # has no payroll entry (e.g. a future roster addition not yet
    # classified). Drawn ONCE per employee, not per pay period - a
    # salaried paycheck is the same amount every time, not a fresh random
    # draw each cycle.
    employee_pay = {}
    for _, emp in employees.iterrows():
        emp_segment = get_person_segment(emp.get("income_level"), emp.get("employment_status"))
        lo, hi = PAYMENT_RANGES["payroll_biweekly"].get(emp_segment, PAYMENT_RANGES["payroll_biweekly"]["P3"])
        employee_pay[emp["entity_id"]] = random.uniform(lo, hi)

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

            amount = employee_pay[emp["entity_id"]]

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
    generate_daily_card_settlement(
        companies_df=companies_df,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    # Standard overhead skeleton (Phase 2, 2026-07-10): fixed monthly rent/
    # mortgage and utilities for every sampled person and company, plus
    # inventory/COGS restocking for companies whose archetype has one.
    # Previously companies had payroll-out and revenue-in but no overhead
    # side at all, and persons had no fixed bills distinct from generic
    # merchant-pattern spending.
    generate_rent_transactions(
        profile_df=profile_df,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    generate_utilities_transactions(
        profile_df=profile_df,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    generate_restocking_transactions(
        companies_df=companies_df,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
        months_in_range=months_in_range,
    )
    generate_business_cash_deposits(
        companies_df=companies_df,
        bents_by_bank=bents_by_bank,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    generate_large_personal_withdrawals(
        profile_df=profile_df,
        bents_by_bank=bents_by_bank,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    generate_c_check_gl_transactions(
        profile_df=profile_df,
        bents_by_bank=bents_by_bank,
        gl_accounts_by_bank=gl_accounts_by_bank,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    generate_owner_distribution_transactions(
        profile_df=profile_df,
        companies_df=companies_df,
        known_accounts=known_accounts,
        register=register,
        start_dt=start_dt,
        end_dt=end_dt,
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
        # A merchant/company deposits accumulated cash over the counter,
        # not through an ATM - branch-only.
        target_branch_bents = [
            b for b in bents_by_bank.get(target_bank, []) if get_bent_type(b.get("name")) != "atm"
        ]
        if target_branch_bents:
            bent_rec = random.choice(target_branch_bents)
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
