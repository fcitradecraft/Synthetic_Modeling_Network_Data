"""
Parses the Actimize Alert Matrix's 7-token rule_id into its component
fields, e.g. AML-STR-CCE-INN-A-D01-DST ->

    business=AML, category=STR, tran_type=CCE, direction=INN,
    scope=A, time_period=D01, subtype=DST

See CLAUDE_CODE_HANDOFF.md section 4 for the full taxonomy table.
"""
from collections import namedtuple

RuleId = namedtuple("RuleId", [
    "raw", "business", "category", "tran_type", "direction", "scope", "time_period", "subtype",
])

DIRECTIONS = {"INN": "credit", "OUT": "debit", "ALL": "both", "KEY": "key_based"}
SCOPES = {"A": "account", "P": "party"}


def parse_rule_id(rule_id: str) -> RuleId:
    parts = rule_id.split("-")
    if len(parts) != 7:
        raise ValueError(f"Expected a 7-token rule_id, got {rule_id!r} ({len(parts)} tokens)")
    business, category, tran_type, direction, scope, time_period, subtype = parts
    return RuleId(rule_id, business, category, tran_type, direction, scope, time_period, subtype)


def window_days(time_period: str) -> int:
    """Convert a time_period token (S01/D01/D05/D07/D30/M01) to a day count."""
    if time_period == "S01":
        return 0  # single transaction, no window
    if time_period.startswith("D"):
        return int(time_period[1:])
    if time_period == "M01":
        return 30
    raise ValueError(f"Unrecognized time_period: {time_period!r}")
