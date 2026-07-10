import uuid
import random
from datetime import datetime, timedelta, date
from faker import Faker

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

def random_timestamp(start_date, end_date):
    """Generate a random timestamp between two datetime objects."""
    delta = end_date - start_date
    random_seconds = random.randint(0, int(delta.total_seconds()))
    return start_date + timedelta(seconds=random_seconds)

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
):
    """Split a transaction into debit and credit entries."""
    known_accounts = known_accounts or set()
    rows = []
    timestamp_date, timestamp_time = timestamp.split(" ", 1)

    src_known = src is not None and hasattr(src, "id") and src.id in known_accounts
    tgt_known = tgt is not None and hasattr(tgt, "id") and tgt.id in known_accounts

    src_name = getattr(src, "owner_name", fake.name()) if src is not None else ""
    tgt_name = getattr(tgt, "owner_name", None)
    if not tgt_name:
        if hasattr(tgt, "owner_type") and tgt.owner_type in ["Company", "Merchant"]:
            tgt_name = fake.company()
        else:
            tgt_name = fake.name()

    credit_description = f"{payment_type.upper()} - {tgt_name}"
    debit_description = f"{payment_type.upper()} - {tgt_name}"

    if payment_type.lower() == "ach" and src is not None and tgt is not None:
        if src.owner_type == "Person" and tgt.owner_type in ["Company", "Merchant"]:
            debit_description = f"ACH Transfer - {abs(amount):.2f} - {tgt_name}"
            credit_description = f"ACH Transfer - {abs(amount):.2f} - {src_name}"

    if payment_type.lower() == "cash":
        # Use provided ATM/BEnt metadata if available
        if atm_id is None:
            atm_id = generate_uuid(8)
        if atm_location is None:
            atm_name = fake.company()
            atm_address = fake.address().replace("\n", ", ")
            atm_location = f"{atm_name} ({atm_address})"

        credit_description = f"CASH - Deposit at {atm_location}"
        debit_description = f"CASH - Withdrawal at {atm_location}"

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
            "counterparty": tgt.id,
            "amount": abs(amount),
            "direction": "debit",
            "currency": currency,
            "bank_name": src.bank_name,
            "owner_name": src.owner_name,
            "payment_type": payment_type,
            "is_laundering": is_laundering,
            "source_description": debit_description,
            "post_date": post_date
        })

    if tgt_known:
        rows.append({
            "transaction_id": txn_id,
            "entry_id": txn_id + "-C",
            "timestamp": timestamp,
            "date": timestamp_date,
            "time": timestamp_time,
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
            "post_date": post_date
        })

    print(f"[DEBUG] src: {src.id if src else 'CASH'}, tgt: {tgt.id if tgt else 'CASH'}, src_known: {src_known}, tgt_known: {tgt_known}")
    if not src_known and not tgt_known:
        print(f"⚠️ Skipping txn {txn_id}: both accounts unknown")

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
        return f"ACH - {purpose} from {company} ({address})"
    elif payment_type == "cash":
        direction = "Deposit" if purpose == "Deposit" else "Withdrawal"
        return f"CASH - {direction} at {company} ATM ({address})"
    elif payment_type == "wire":
        return f"WIRE - {purpose} via {company} Bank"
    elif payment_type == "credit_card":
        # Legacy token - superseded by the granular ccard/credit/debit/pos
        # tokens below, kept only so old saved output/tests don't break.
        return f"CREDIT CARD - {purpose} charged to account at {company}"
    elif payment_type == "check":
        return f"CHECK - {purpose} written by {name}"
    elif payment_type == "c_check":
        return f"CASHIER'S CHECK - {purpose} issued to {name}"
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
