"""Internal-only account balance ledger.

Tracks each sampled person/company account's opening balance and, once
generation is complete, its true chronological running balance. This exists
purely for generation-time realism (funding checks) and validate.py's
balance-integrity gates (running_balance_reconciliation, no_negative_running_
balance) - it is never exposed in any student-facing export. See main.py's
split_answer_key, which excludes running_balance from every exported file.

Deliberately has no dependency on generator/transactions.py (which imports
from here for register()'s live funding-check hook) - opening-balance range
lookups (segment/size-tier aware) live in main.py, where that account
metadata is already resolved onto each ProfileAccount.
"""

from __future__ import annotations


class LedgerState:
    """Tracks a live, approximate balance per known account during
    generation - used only for check_sufficient_funds gating. This is NOT
    the authoritative running_balance record: generation runs as a series
    of per-payer/per-category loops (payroll, rent, purchases, ...) and
    injection runs as a separate later pass, so entries are not applied in
    true chronological order here. The authoritative, chronologically
    correct running_balance is computed once, after every row (legit and
    injected) is fully assembled, by compute_running_balances() below.
    """

    def __init__(self):
        self.opening_balances: dict[str, float] = {}
        self._live: dict[str, float] = {}

    def register_account(self, account_id: str, opening_balance: float) -> None:
        self.opening_balances[account_id] = opening_balance
        self._live[account_id] = opening_balance

    def apply_entry(self, account_id: str, signed_amount: float) -> float | None:
        """Update and return this account's live balance. Returns None for
        an account this ledger doesn't track (a merchant, a synthetic
        landlord/supplier, a GL account, or any other counterparty that
        isn't one of the sampled person/company accounts) - callers should
        simply skip stamping/gating on that case, not treat it as an error.
        """
        if account_id not in self._live:
            return None
        self._live[account_id] += signed_amount
        return self._live[account_id]

    def check_sufficient_funds(self, account_id: str, amount: float, overdraft_limit: float = 0.0) -> bool:
        """Would a debit of `amount` take this account below -overdraft_limit?

        Accounts this ledger doesn't track are always considered affordable
        - this gate only applies to the sampled person/company accounts
        that actually have an opening balance on file.
        """
        if account_id not in self._live:
            return True
        return self._live[account_id] - amount >= -overdraft_limit


def compute_running_balances(rows: list[dict], opening_balances: dict[str, float]) -> None:
    """Stamp the canonical, chronologically correct running_balance onto
    `rows` in place, once every row (legit + injected) is fully assembled.

    Each tracked account's rows are sorted by timestamp and walked in order
    from its opening balance - this is the only way to guarantee correct
    posting order given generation and injection each run in their own
    internal (non-chronological-across-accounts) sequence. Rows belonging
    to an account with no entry in `opening_balances` (a merchant, a
    synthetic landlord/supplier, a GL account - anything that isn't a
    sampled person/company account) are left without a running_balance,
    matching how those accounts were never funding-checked either.
    """
    by_account: dict[str, list[dict]] = {}
    for row in rows:
        acct = row.get("account_id")
        if acct in opening_balances:
            by_account.setdefault(acct, []).append(row)

    for acct, acct_rows in by_account.items():
        acct_rows.sort(key=lambda r: r["timestamp"])
        balance = opening_balances[acct]
        for row in acct_rows:
            signed = row["amount"] if row["direction"] == "credit" else -row["amount"]
            balance += signed
            row["running_balance"] = round(balance, 2)
