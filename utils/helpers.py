import uuid
import random
from datetime import datetime, timedelta, date
from faker import Faker

from utils.logger import log

fake = Faker()

def generate_uuid(length=12):
    """Generate a short unique ID (default 12 characters).

    Built off the stdlib random module (via random.getrandbits), not
    uuid.uuid4() - uuid4 draws from os.urandom and ignores random.seed(),
    which would silently break --seed reproducibility.
    """
    return uuid.UUID(int=random.getrandbits(128)).hex[:length]

def parse_date(date_str):
    """Parse a date string like '2025-01-01' into a datetime object."""
    return datetime.strptime(date_str, "%Y-%m-%d")

def get_bent_type(name) -> str:
    """Classify a BEnt record as 'atm' or 'branch' from its name string
    (e.g. "Wells Farquod ATM 995123001" vs "...Branch 100059") - BEnt rows
    have no dedicated type column, but the naming convention already
    carries the distinction. Defaults to 'branch' (uncapped, unrounded,
    over-the-counter wording) for anything that doesn't match, including
    the generated-placeholder fallback used when a bank has no BEnt rows
    at all."""
    if not isinstance(name, str):
        return "branch"
    return "atm" if "atm" in name.lower() else "branch"

def random_timestamp(start_date, end_date):
    """Generate a random timestamp between two datetime objects."""
    delta = end_date - start_date
    random_seconds = random.randint(0, int(delta.total_seconds()))
    return start_date + timedelta(seconds=random_seconds)

# Real BICs this generator must never accidentally produce - duplicated
# from validate.py's REAL_BIC_DENY_LIST (not imported, to avoid a circular
# import - validate.py already imports from this module). Keep in sync by
# hand if that list changes.
_REAL_BIC_DENY_LIST = {"UPNBUS44", "VALLMTMT"}

def generate_synthetic_bic(country_code: str = "US") -> str:
    """Generate a fictitious but structurally-valid 8-character BIC/SWIFT
    code (4-letter bank code + 2-letter country code, defaulting to 'US' +
    2-character location code, all uppercase) - used for any wire
    counterparty that isn't one of the 3 modeled banks (see
    agents/agent_profiles.xlsx's swift_code column,
    scripts/fix_bank_swift_codes.py). A real BIC's 5th-6th characters are
    the country code, so an international counterparty should pass its own
    (see config/trade_countries.yaml / config/high_risk_countries.yaml)
    rather than leaving every synthetic BIC looking domestic."""
    while True:
        bank_code = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=4))
        location_code = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=2))
        bic = f"{bank_code}{country_code}{location_code}"
        if bic not in _REAL_BIC_DENY_LIST:
            return bic

# Generic, plausible wire purposes (SWIFT MT103 Field 70/"Originator to
# Beneficiary Information" equivalent) - deliberately one shared pool for
# every wire regardless of is_laundering, so this field carries no
# leakage tell on its own (a laundering-typology wire's stated purpose
# looks exactly as mundane as a legitimate one - that's the nature of the
# typology, not something to "fix" by making it sound suspicious).
WIRE_PURPOSE_TEMPLATES = [
    "Invoice settlement",
    "Contract payment - professional services",
    "Trade settlement",
    "Funds transfer - business operations",
    "Real estate closing costs",
    "Vendor payment",
    "Equipment purchase",
    "Consulting services payment",
    "Supply chain settlement",
    "Investment funding transfer",
]

def safe_sample(population, k):
    """Safely sample k items from a list, even if the list is smaller than k."""
    return random.sample(population, min(k, len(population)))

def to_datetime(value):
    """
    Convert a value to a datetime object.
    Accepts either a string ("YYYY-MM-DD") or a datetime.date object.
    """
    if isinstance(value, datetime):
        return value
    elif isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d")
    else:
        raise TypeError(f"Unsupported type for to_datetime: {type(value)}")

def split_transaction(
    txn_id,
    timestamp,
    src,
    tgt,
    amount,
    currency,
    payment_type,
    is_laundering,
    source_description="",
    known_accounts=None,
    post_date=None,
    atm_id=None,
    atm_location=None,
    credit_timestamp=None,
    credit_post_date=None,
):
    """Split a transaction into debit and credit entries.

    credit_timestamp/credit_post_date (optional) let the credit leg post on
    a later date than the debit leg - a check's clearing float, where the
    payer's account is debited on write/clear but the payee doesn't deposit
    it (and it doesn't clear) until days later. Every other payment type
    leaves these unset and both legs share the one timestamp/post_date, as
    before.
    """
    known_accounts = known_accounts or set()
    rows = []
    timestamp_date, timestamp_time = timestamp.split(" ", 1)
    credit_ts = credit_timestamp if credit_timestamp else timestamp
    credit_date, credit_time = credit_ts.split(" ", 1)
    credit_pd = credit_post_date if credit_post_date else post_date

    src_known = src is not None and hasattr(src, "id") and src.id in known_accounts
    tgt_known = tgt is not None and hasattr(tgt, "id") and tgt.id in known_accounts

    src_name = getattr(src, "owner_name", fake.name()) if src is not None else ""
    tgt_name = getattr(tgt, "owner_name", None)
    if not tgt_name:
        if hasattr(tgt, "owner_type") and tgt.owner_type in ["Company", "Merchant"]:
            tgt_name = fake.company()
        else:
            tgt_name = fake.name()

    # A caller-supplied source_description (e.g. describe_transaction's
    # "Rent/Mortgage"/"Utility - Electricity"/"Restocking/Supplier") is
    # more specific than anything this function can build from just the
    # counterparty name, so it wins when provided. Previously this
    # parameter was accepted but silently discarded - every non-cash
    # transaction fell through to the generic templates below regardless
    # of what a caller passed in.
    if source_description:
        credit_description = source_description
        debit_description = source_description
    else:
        credit_description = f"{payment_type.upper()} - {tgt_name}"
        debit_description = f"{payment_type.upper()} - {tgt_name}"

    # ACH/check/cashier's check: always build from the real resolved
    # src/tgt names, overriding any caller-supplied source_description -
    # same precedent as cash below. An outgoing (debit) transfer should
    # name who it was paid to, not restate who sent it; the incoming
    # (credit) side should name who it came from. describe_transaction's
    # own wording for these three used to fabricate an unrelated random
    # name/company - this replaces that entirely. (ACH previously had a
    # conditional version of this same idea guarded by "no caller-supplied
    # description," but every real call site always supplies one, so that
    # branch never actually ran - unconditional override, matching cash and
    # check/c_check, is what actually takes effect.)
    if payment_type.lower() in ("ach", "check", "c_check") and src is not None and tgt is not None:
        label = {"ach": "ACH", "check": "CHECK", "c_check": "CASHIER'S CHECK"}[payment_type.lower()]
        debit_description = f"{label} - Paid to {tgt_name}"
        credit_description = f"{label} - Received from {src_name}"

    # Wire-specific details (Rio's call, 2026-07-15): a standard Travel
    # Rule compliance record (31 CFR 1010.410(f) - originator/beneficiary
    # name, address, account, and the beneficiary's institution) plus a
    # separate SWIFT MT103 Field 70-style "Originator to Beneficiary
    # Information" purpose/remittance field. Both fields describe the
    # whole transfer, not one leg, so both legs get the identical value -
    # unlike check/ach's "Paid to/Received from" wording. The beneficiary-
    # institution name here is best-effort: bank_name on a ProfileAccount
    # is normally resolved later (post-generation, by main.py - see the
    # bank-assignment fix), so it's usually not yet known at this point -
    # falls back to "External Institution", same as the real Travel Rule's
    # own "as many of the following items as are received" framing (not
    # everything is always captured in practice either). swift_code
    # itself (this row's own bank) is a separate main.py export-time
    # field, not built here - see main.py's bank-resolution loop.
    def _clean(val, default):
        # A pandas NaN (float) is truthy in Python, so a plain "or" falls
        # through it instead of the default - a NaN != itself is the
        # standard NaN check without needing a pandas/math import here.
        if val is None or val != val:
            return default
        val = str(val).strip()
        return val if val and val.lower() != "nan" else default

    travel_rule_info = None
    originator_beneficiary_info = None
    counterparty_country_code = None
    if payment_type.lower() == "wire" and src is not None and tgt is not None:
        src_id = src.id if hasattr(src, "id") else "N/A"
        tgt_id = tgt.id if hasattr(tgt, "id") else "N/A"
        src_address = _clean(getattr(src, "address", None), "Address on file")
        tgt_address = _clean(getattr(tgt, "address", None), "Address on file")
        tgt_institution = _clean(getattr(tgt, "bank_name", None), "External Institution")
        # Whichever side carries an explicit (non-default) country_code is
        # the transaction's country - set by inject_cty (high-risk) or the
        # legitimate international-trade counterparties
        # (generate_company_revenue_transactions/generate_restocking_
        # transactions, gated on INTERNATIONAL_TRADE_ARCHETYPES). Defaults
        # to "US" when neither side sets one - an ordinary domestic wire,
        # not a blank field.
        src_country = getattr(src, "country_code", None)
        tgt_country = getattr(tgt, "country_code", None)
        counterparty_country_code = (
            tgt_country if tgt_country and tgt_country != "US"
            else (src_country if src_country and src_country != "US" else "US")
        )
        tgt_bic = getattr(tgt, "swift_code", None) or generate_synthetic_bic(counterparty_country_code)
        travel_rule_info = (
            f"Originator: {src_name}, {src_address}, Acct {src_id} | "
            f"Beneficiary: {tgt_name}, {tgt_address}, Acct {tgt_id}, "
            f"Institution: {tgt_institution} ({tgt_bic})"
        )
        originator_beneficiary_info = random.choice(WIRE_PURPOSE_TEMPLATES)

    if payment_type.lower() == "cash":
        # Use provided ATM/BEnt metadata if available
        if atm_id is None:
            atm_id = generate_uuid(8)
        if atm_location is None:
            atm_name = fake.company()
            atm_address = fake.address().replace("\n", ", ")
            atm_location = f"{atm_name} ({atm_address})"

        # ATM vs. over-the-counter (branch/teller) are mechanically
        # different transactions, not just a different location - say so
        # in the description. Rio's call, 2026-07-10.
        cash_label = "ATM" if get_bent_type(atm_id) == "atm" else "CASH"
        credit_description = f"{cash_label} - Deposit at {atm_location}"
        debit_description = f"{cash_label} - Withdrawal at {atm_location}"

        placeholder_cp = "ATM"

        # Deposit: src is None
        if src is None and tgt is not None:
            if tgt_known:
                rows.append({
                    "transaction_id": txn_id,
                    "entry_id": txn_id + "-C",
                    "timestamp": timestamp,
                    "date": timestamp_date,
                    "time": timestamp_time,
                    "account_id": tgt.id,
                    "counterparty": placeholder_cp,
                    "amount": abs(amount),
                    "direction": "credit",
                    "currency": currency,
                    "bank_name": tgt.bank_name,
                    "owner_name": tgt.owner_name,
                    "payment_type": payment_type,
                    "is_laundering": is_laundering,
                    "source_description": credit_description,
                    "post_date": post_date,
                    "atm_id": atm_id,
                    "atm_location": atm_location
                })
            return rows

        # Withdrawal: tgt is None
        if tgt is None and src is not None:
            if src_known:
                rows.append({
                    "transaction_id": txn_id,
                    "entry_id": txn_id + "-D",
                    "timestamp": timestamp,
                    "date": timestamp_date,
                    "time": timestamp_time,
                    "account_id": src.id,
                    "counterparty": placeholder_cp,
                    "amount": abs(amount),
                    "direction": "debit",
                    "currency": currency,
                    "bank_name": src.bank_name,
                    "owner_name": src.owner_name,
                    "payment_type": payment_type,
                    "is_laundering": is_laundering,
                    "source_description": debit_description,
                    "post_date": post_date,
                    "atm_id": atm_id,
                    "atm_location": atm_location
                })
            return rows

        # Traditional cash transfer between two accounts (rare)
        if src_known:
            rows.append({
                "transaction_id": txn_id,
                "entry_id": txn_id + "-D",
                "timestamp": timestamp,
                "date": timestamp_date,
                "time": timestamp_time,
                "account_id": src.id,
                "counterparty": tgt.id if tgt else placeholder_cp,
                "amount": abs(amount),
                "direction": "debit",
                "currency": currency,
                "bank_name": src.bank_name,
                "owner_name": src.owner_name,
                "payment_type": payment_type,
                "is_laundering": is_laundering,
                "source_description": debit_description,
                "post_date": post_date,
                "atm_id": atm_id,
                "atm_location": atm_location
            })

        if tgt_known:
            rows.append({
                "transaction_id": txn_id,
                "entry_id": txn_id + "-C",
                "timestamp": timestamp,
                "date": timestamp_date,
                "time": timestamp_time,
                "account_id": tgt.id,
                "counterparty": src.id if src else placeholder_cp,
                "amount": abs(amount),
                "direction": "credit",
                "currency": currency,
                "bank_name": tgt.bank_name,
                "owner_name": tgt.owner_name,
                "payment_type": payment_type,
                "is_laundering": is_laundering,
                "source_description": credit_description,
                "post_date": post_date,
                "atm_id": atm_id,
                "atm_location": atm_location
            })

        return rows

    # Non-cash transactions
    if src_known:
        rows.append({
            "transaction_id": txn_id,
            "entry_id": txn_id + "-D",
            "timestamp": timestamp,
            "date": timestamp_date,
            "time": timestamp_time,
            "account_id": src.id,
            "counterparty": tgt.id if tgt is not None else "",
            "amount": abs(amount),
            "direction": "debit",
            "currency": currency,
            "bank_name": src.bank_name,
            "owner_name": src.owner_name,
            "payment_type": payment_type,
            "is_laundering": is_laundering,
            "source_description": debit_description,
            "post_date": post_date,
            "travel_rule_info": travel_rule_info,
            "originator_beneficiary_info": originator_beneficiary_info,
            "counterparty_country_code": counterparty_country_code,
        })

    if tgt_known:
        rows.append({
            "transaction_id": txn_id,
            "entry_id": txn_id + "-C",
            "timestamp": credit_ts,
            "date": credit_date,
            "time": credit_time,
            "account_id": tgt.id,
            "counterparty": src.id if src else "",
            "amount": abs(amount),
            "direction": "credit",
            "currency": currency,
            "bank_name": tgt.bank_name,
            "owner_name": tgt.owner_name,
            "payment_type": payment_type,
            "is_laundering": is_laundering,
            "source_description": credit_description,
            "post_date": credit_pd,
            "travel_rule_info": travel_rule_info,
            "originator_beneficiary_info": originator_beneficiary_info,
            "counterparty_country_code": counterparty_country_code,
        })

    if not src_known and not tgt_known:
        log(f"Skipping txn {txn_id}: both accounts unknown", level="WARNING")

    return rows


def generate_timestamp(start_date, end_date):
    """Generate a random timestamp between two datetime objects."""
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d")
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%Y-%m-%d")

    delta = end_date - start_date
    random_seconds = random.randint(0, int(delta.total_seconds()))
    return start_date + timedelta(seconds=random_seconds)

def describe_transaction(payment_type, purpose=None):
    company = fake.company()
    name = fake.name()
    address = fake.address().replace("\n", ", ")

    if payment_type == "ach":
        # Superseded by split_transaction's own leg-aware ACH wording (built
        # from the real src/tgt names) - see split_transaction. This
        # fallback only matters if describe_transaction is ever called from
        # a path that doesn't route through split_transaction.
        return f"ACH - {purpose}"
    elif payment_type == "cash":
        direction = "Deposit" if purpose == "Deposit" else "Withdrawal"
        return f"CASH - {direction} at {company} ATM ({address})"
    elif payment_type == "wire":
        return f"WIRE - {purpose} via {company} Bank"
    elif payment_type == "check":
        # Superseded by split_transaction's own leg-aware check wording
        # (built from the real src/tgt names) - this fallback only matters
        # if describe_transaction is ever called from a path that doesn't
        # route through split_transaction.
        return f"CHECK - {purpose}"
    elif payment_type == "c_check":
        return f"CASHIER'S CHECK - {purpose}"
    elif payment_type == "p2p":
        return f"P2P - {purpose} sent to {name}"
    elif payment_type in ("ccard", "credit"):
        return f"CREDIT CARD - {purpose} charged to account at {company}"
    elif payment_type == "debit":
        return f"DEBIT CARD - {purpose} charged to account at {company}"
    elif payment_type == "pos":
        return f"POS - {purpose} at {company} ({address})"

    return f"{payment_type.upper()} - {purpose or 'Transaction'}"


def is_us_federal_holiday(dt: datetime) -> bool:
    """Return True if the given date falls on a US federal holiday (2025)."""
    holidays_2025 = {
        (1, 1),   # New Year's Day
        (1, 20),  # Martin Luther King Jr. Day
        (2, 17),  # Washington's Birthday
        (5, 26),  # Memorial Day
        (7, 4),   # Independence Day
        (9, 1),   # Labor Day
        (10, 13), # Columbus Day
        (11, 11), # Veterans Day
        (11, 27), # Thanksgiving Day
        (12, 25), # Christmas Day
    }
    return (dt.month, dt.day) in holidays_2025


def generate_post_date(transaction_dt: datetime) -> datetime:
    """Return a posting datetime after ``transaction_dt`` within business hours."""

    for _ in range(100):
        # Try an offset between 0 and 3 days
        post_dt = transaction_dt + timedelta(days=random.randint(0, 3))

        # If the tentative date falls on a weekend, move to the following Monday
        if post_dt.weekday() >= 5:
            post_dt += timedelta(days=7 - post_dt.weekday())

        # Skip US federal holidays
        while is_us_federal_holiday(post_dt):
            post_dt += timedelta(days=1)

        # Ensure we don't exceed the three-day window
        if (post_dt - transaction_dt).days > 3:
            continue

        # Choose a posting time within banking hours
        start_hour = 8
        if post_dt.date() == transaction_dt.date():
            start_hour = max(start_hour, transaction_dt.hour)
        if start_hour >= 17:
            # No business hours remaining on this day
            continue
        hour = random.randint(start_hour, 16)
        minute = random.randint(0, 59)
        second = random.randint(0, 59)
        post_dt = post_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)

        # Verify ordering
        if post_dt > transaction_dt:
            return post_dt

    # Fallback: next business day at 09:00
    post_dt = transaction_dt + timedelta(days=1)
    post_dt = post_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    while post_dt.weekday() >= 5 or is_us_federal_holiday(post_dt):
        post_dt += timedelta(days=1)
    return post_dt


def generate_transaction_timestamp(start_dt: datetime, end_dt: datetime,
                                   entity_type: str | None = None,
                                   override_hours: bool = False) -> datetime:
    """Generate a transaction timestamp honoring business hour rules."""
    for _ in range(100):
        ts = random_timestamp(start_dt, end_dt)
        if override_hours:
            return ts

        if entity_type == "Company":
            if ts.weekday() < 5 and 8 <= ts.hour < 17:
                return ts
        else:  # Person or other
            if 8 <= ts.hour < 20:
                return ts

    return ts  # fallback if conditions not met
