# AML Alert-Clearing Exercise Generator

A seeded Python generator that produces Actimize-style core-banking datasets for training AML
investigators to clear alerts. Given a headcount of persons/companies to sample and how many to
flag, it emits realistic legitimate activity plus rule-accurate suspicious patterns, validates
the result, and exports per-bank student files, a customer-info file, and a separate answer key.

Everything here dispatches off the 23-rule `Alert Matrix` in `Actimize Rules_Alert Clearing
Ex.xlsx` (the spec) - see `CLAUDE_CODE_HANDOFF.md` for the full rule taxonomy and project history.

---

## Requirements

- Python 3.10+ (the pinned `numpy==2.2.6` needs it; macOS system Python is 3.9.x, install 3.12
  via Homebrew or python.org if `python3 --version` comes back lower)
- A virtual environment - `aml-env/` in this repo is one such venv, already set up

```bash
python3.12 -m venv aml-env
source aml-env/bin/activate
pip install -r requirements.txt
```

Every example below assumes you're running from the repo root with that venv active (or calling
`./aml-env/bin/python3` directly, which doesn't require activating anything).

---

## Quick start

```bash
./aml-env/bin/python3 main.py \
  --n_persons 6 --n_companies 10 --n_flagged 3 \
  --seed 42 --start_date 2025-01-01 --end_date 2025-03-31
```

This samples 6 persons and 10 companies from `agents/agent_profiles.xlsx`, flags 3 of those 16
accounts with a rule-based suspicious pattern each, and writes everything to `data/`. Same
`--seed` + same arguments always reproduces byte-identical output.

Use `--n_flagged 0` for guaranteed-clean output - no injection call is made at all.

---

## How a run works

1. **Sample.** `--n_persons`/`--n_companies` pull that many rows from `Combined_Data`'s person
   and company pools (capped today at 22 persons / 34 companies - the command hard-errors if you
   ask for more than exist). Merchants, bank-entity (`BEnt`) rows, and banks themselves are never
   sampled down; they stay fully available as counterparties, so the simulated world doesn't feel
   artificially small even when the sampled customer roster is.
2. **Generate legitimate activity.** For every sampled person and company:
   - **Payroll** - persons with an `employer` value get biweekly direct deposits, sized off their
     `income_level`/`employment_status` segment (see `config/payment_ranges.yaml`).
   - **Self-employed / unearned income** - persons with no `employer` (self-employed or
     unemployed-high-income) get an irregular client-payment or periodic-distribution channel
     instead, so no account is a pure spender with no funding source.
   - **Purchases** - every person/company spends at merchants per their `merchant_patterns`/
     `merchant_frequency` columns, at an amount drawn from the merchant's `average_expense`. The
     payment rail (P2P, ACH, wire, check, cashier's check, cash, or one of the card sub-types) is
     then chosen only from among the merchant's accepted methods whose typical range for that
     payer's segment actually contains the amount - so a $30,000 purchase can't roll as P2P.
   - **Company revenue** - companies also receive incoming revenue from synthesized, untracked
     counterparties, sized off `revenue_monthly_target`, with the same amount-then-rail logic.
3. **Inject suspicious activity.** `--n_flagged` picks that many distinct sampled accounts and
   fires one rule injector each, weighted by `account_type_weight` in `thresholds.yaml` (e.g.
   international wire activity is weighted toward companies). `--rule_config` is a power-user
   override for an exact rule_id + count mix instead of a random weighted pick.
4. **Validate.** `validate.py` runs 12 gates against the full transaction set before anything is
   written - see [Validation](#validation) below. Only a hard `FAIL` blocks export.
5. **Export.** Transactions and customer info are partitioned **per bank** (a bank's own extract
   only shows its own accounts' legs), plus one combined workbook across all banks and one
   unified answer key.

---

## CLI reference

| Flag | Default | Purpose |
|---|---|---|
| `--n_persons` | *required* | Persons to sample from `Combined_Data` |
| `--n_companies` | *required* | Companies to sample from `Combined_Data` |
| `--n_flagged` | `0` | Sampled accounts to flag with one injected rule each. Mutually exclusive with `--rule_config` |
| `--rule_config` | none | Path to a YAML of exact `rule_id` + instance counts, instead of a random weighted pick |
| `--seed` | none | Random seed - same seed + same args → byte-identical output |
| `--start_date` / `--end_date` | `2025-01-01` / `2025-01-31` | Transaction date range |
| `--legit_txns` | uncapped | Optional safety-valve ceiling on base (pre-injection) transaction count |
| `--agent_profiles` | `agents/agent_profiles.xlsx` | Identity source workbook |
| `--thresholds` | `thresholds.yaml` | Per-rule firing parameters |
| `--output` | `data/aml_dataset.csv` | Student transaction file (partitioned per bank) |
| `--customer_info` | `data/customer_info.csv` | Customer info file (partitioned per bank) |
| `--combined_output` | `data/aml_dataset_combined.xlsx` | One workbook, every bank as its own sheet pair (always written) |
| `--answer_key` | `data/answer_key.csv` | Grading file - unified across all banks |
| `--format` | `csv` | `csv` (one file per bank) or `xlsx` (one workbook, one sheet per bank) |

---

## Data and config files

### `agents/agent_profiles.xlsx` → `Combined_Data` sheet

The sole identity source. Every row is a person, company, merchant, `BEnt` (bank branch/ATM), or
bank, with columns including `entity_id`, `type`, `bank`, `account_number`, `name`, `address`,
`accepted_payment_methods`, `average_expense`, `transaction_scaler` (a size/spend multiplier),
`merchant_patterns`/`merchant_frequency` (what a payer buys and how often), and, for persons,
`dob`, `employment_status`, `employer`, `income_level`. `BEnt` rows supply the branch/ATM
identity used for every cash leg - without them the generator falls back to a placeholder ATM.

### `thresholds.yaml`

One entry per `rule_id`, each with `built: true/false` and its firing parameters (amounts,
window days, minimum counts, `account_type_weight`). 8 of the 23 Alert Matrix rules are built and
injectable today; 9 more are documented but not yet built (`built: false`); 6 Historical Profile
Deviation rules are deprioritized and not represented at all. Only the $10,000 CTR threshold (31
CFR 1010.311) is settled regulation - every other number is a DRAFT business/risk judgment call.
See the file's own header comment before treating any number as final.

### `config/payment_ranges.yaml`

Segment-based typical value/volume ranges per payment rail, researched from Federal Reserve,
Nacha, BLS, and small-business cash-flow data. Persons are segmented `P1`-`P6` by
`income_level` × `employment_status` (computed, not stored); companies are segmented `C1`-`C3`
(retail/F&B, manufacturing, professional services) by an explicit `entity_id` list, since 11 of
34 companies have no populated NAICS description to classify from automatically. Edit ranges
directly to recalibrate a segment; move an `entity_id` between the `company_segments` lists to
reclassify a company.

### `config/high_risk_countries.yaml`

The fictional watchlist used by the CTY (high-risk-country transfer) rule.

---

## Output files

### Transactions (student file)

`transaction_id, entry_id, date, time, account_id, counterparty, amount, direction, currency,
bank_name, owner_name, payment_type, source_description, post_date, atm_id, atm_location`

`amount` is always non-negative; `direction` (`debit`/`credit`) carries the sign. `atm_id`/
`atm_location` are populated only on cash rows. No column here reveals which rows are suspicious.

### Customer info

`customer_id, account_number, type, name, address, type_of_business, bank_name`

### Answer key (grading only - not part of the student file)

`entry_id, is_laundering, rule_id, typology, role_in_typology, difficulty`

`is_laundering` is exactly what the rule injector set on its own rows - there is no downstream
taint propagation. An account that's been flagged for a rule still has ordinary, untainted
activity mixed in, on purpose: investigators are meant to reason about realistic, imperfect data,
not a dataset with perfect money-trail provenance.

---

## The `rule_id` taxonomy

Every rule is a 7-token ID, e.g. `AML-STR-CCE-INN-A-D01-DST`:

```
AML - STR - CCE - INN - A - D01 - DST
 |     |     |     |    |    |     |
 |     |     |     |    |    |     └─ Subtype (DST=structuring, EAT=excessive activity,
 |     |     |     |    |    |          EOP=burst concentration, EST=excessive single txn,
 |     |     |     |    |    |          FTR=flow-through, CTY=country, MBD=multi-branch deposit)
 |     |     |     |    |    └─ Time period (S01=single txn, D01/D05/D07/D30=days, M01=1 month)
 |     |     |     |    └─ Scope (A=account, P=party group)
 |     |     |     └─ Direction (INN=credit/in, OUT=debit/out, ALL=both, KEY=key-based)
 |     |     └─ Tran type (CCE=cash/cash-equivalent, EFT, IFT=intl EFT, ATM, ACT=account, ALL)
 |     └─ Category (STR, ATM, ECT, EBB, EBO, EFT, FTF, MLB, EXT, ...)
 └─ Business line (AML)
```

`generator/rule_taxonomy.py` parses this; `generator/rule_injectors.py` dispatches on it - one
injector function per subtype, firing on that rule's actual textual definition rather than a
generic pattern library.

---

## Validation

`validate.py` runs 12 gates before any file is written:

1. No duplicate `entry_id`
2. Debit/credit legs of a `transaction_id` balance to zero
3. `running_balance` reconciles *(skipped - field not yet in schema)*
4. `direction` is consistent with a non-negative `amount`
5. `post_date` never falls on a weekend/federal holiday
6. `post_date` never precedes the transaction timestamp
7. Fields that should be blank for a row's type are blank (e.g. `atm_id` only on cash rows)
8. Every BIC is 8 or 11 characters *(skipped - not yet exported)*
9. No BIC matches a real institution *(skipped - not yet exported)*
10. Injected rows fall within the configured date range
11. No negative `running_balance` *(skipped - field not yet in schema)*
12. **Leakage** - no column predicts `is_laundering` above base rate

Only a hard `FAIL` blocks export (gates 1-2, 4-7, 10 - real correctness bugs). Leakage (gate 12)
returns a `WARN` and still writes output: a leak-free answer key isn't the goal, and at small
account counts some incidental correlation (e.g. a specific ATM branch, from a small per-bank
branch pool) is close to unavoidable. `payment_type` and `date` are exempt from the leakage check
entirely - both are legitimately part of a rule's own evidentiary signature (a cash-equivalent
rule concentrating on `payment_type='cash'`, a structuring rule clustering on specific days),
not an incidental leak.

Run it standalone against an already-exported file:

```bash
./aml-env/bin/python3 validate.py data/aml_dataset_combined.xlsx --start_date 2025-01-01 --end_date 2025-03-31
```

---

## Project layout

```
main.py                          CLI entry point - sampling, injection dispatch, export
generator/
  transactions.py                Core engine: legit activity, payroll, self-employment/
                                  unearned income, company revenue, payment-rail selection
  rule_injectors.py               One injector per built rule subtype (DST/EAT/CTY/MBD)
  rule_taxonomy.py                Parses the 7-token rule_id
  exporter.py                     Per-bank CSV/XLSX writers
utils/
  helpers.py                      Seeded UUIDs, timestamps, double-entry ledger splitting,
                                  transaction descriptions
validate.py                       Pre-export validation gates
thresholds.yaml                   Per-rule firing parameters
config/
  payment_ranges.yaml              Payment-rail segment calibration
  high_risk_countries.yaml         CTY rule watchlist
agents/
  agent_profiles.xlsx              Identity source (Combined_Data sheet)
scripts/                          One-off calibration/migration scripts already run against
                                  agent_profiles.xlsx (kept for reference, not part of a normal run)
```

---

## Known limitations

- 9 of the 23 Alert Matrix rules are documented in `thresholds.yaml` but have no injector yet
  (`built: false`); 6 Historical Profile Deviation rules are deprioritized entirely.
- Every threshold number besides the $10,000 CTR figure is a DRAFT judgment call pending sign-off.
- `running_balance` and exported `swift_code` don't exist in the schema yet, so 4 of the 12
  validation gates are permanently `SKIP` until that's built.
- At small account counts (roughly `n_persons + n_companies` under ~15), expect occasional
  leakage warnings from a small per-bank ATM/branch pool - this is accepted small-exercise noise,
  not a defect, and no longer blocks output.
