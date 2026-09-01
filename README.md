# AML Alert-Clearing Exercise Generator

A seeded Python generator that produces AML System-style core-banking datasets for training AML
investigators to clear alerts. Given a headcount of persons/companies to sample and how many to
flag, it emits realistic legitimate activity plus rule-accurate suspicious patterns, validates
the result, and exports per-bank student files, a customer-info file, and a separate answer key.

Everything here dispatches off a 23-rule `Alert Matrix` in `Actimize Rules_Alert Clearing
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
   - **This is a floor, not an exact count.** A relational closure pass then pulls in any
     employer or owned company a sampled person is connected to, and any owner a sampled company
     is connected to, repeating until nothing new is needed - so an employed person's employer,
     or an owner's business, is never left dangling outside the sample. The final counts can
     exceed what you asked for; the log line says so when that happens.
2. **Generate legitimate activity.** For every sampled person and company:
   - **Payroll** - persons with an `employer` value get a fixed biweekly direct deposit (drawn
     once, same amount every period - a real paycheck doesn't change week to week), sized off
     their `income_level`/`employment_status` segment (see `config/payment_ranges.yaml`).
   - **Self-employed / unearned income** - persons with no `employer` (self-employed or
     unemployed-high-income) get an irregular client-payment or periodic-distribution channel
     instead, so no account is a pure spender with no funding source.
   - **Rent/mortgage and utilities** - every person and company pays a fixed monthly rent/
     mortgage, plus three separate monthly bills (electricity, phone, internet), each with its
     own small variance around a per-profile baseline.
   - **Owner distributions** - persons who own (solely or jointly) one of the ~18 owner-mapped
     companies (`scripts/assign_business_ownership.py`) get a monthly distribution on top of
     whatever salary/self-employment income they already have, split across joint owners by
     ownership percentage.
   - **Purchases** - every person/company spends at merchants per their `merchant_patterns`/
     `merchant_frequency` columns, at an amount drawn from the merchant's `average_expense`. The
     payment rail (P2P, ACH, wire, check, cashier's check, cash, or one of the card sub-types) is
     then chosen only from among the merchant's accepted methods whose typical range for that
     payer's segment actually contains the amount - so a $30,000 purchase can't roll as P2P.
   - **Company revenue and restocking** - companies receive incoming revenue sized off
     `revenue_monthly_target`. Rail is chosen *first* (weighted by the company's archetype's
     `revenue_mix_weights`), amount drawn from *that rail's own range* second - a size-tiered
     range for wire/check (small/mid/large, by `transaction_scaler`), an aggregated
     daily-card-batch settlement for RESTAURANT/RETAIL/GAS_STATION instead of one row per walk-in
     sale. Restocking/COGS is sized as a percentage of actual revenue - archetypes with no real
     COGS (professional services, real estate, financial services) simply don't get a restocking
     channel.
   - **International wires** - IMPORT_EXPORT/MANUFACTURING/WHOLESALE_DISTRIBUTION companies send
     a share of their legitimate wire volume to an ordinary real-country counterparty
     (`config/trade_countries.yaml`), so genuine cross-border activity exists independent of the
     CTY rule's high-risk-country injections.
   - **ATM vs. over-the-counter (OTC) cash** - ATM withdrawals round to the nearest $20 (no
     change) and are capped per account per day by income segment (`source_description` says
     "ATM"); once the cap is hit, further cash need routes to a branch/teller withdrawal instead
     - exact amount, uncapped, business hours only (`source_description` says "CASH"). Businesses
     never use an ATM - their cash is always OTC. Cash-heavy companies (RESTAURANT/RETAIL/
     GAS_STATION) also get a weekly bulk OTC till deposit in the thousands of dollars, plus a
     smaller OTC withdrawal to restock the register with change. Persons occasionally (~12%
     chance) make one large, one-off OTC withdrawal not tied to any purchase - cash for a big-
     ticket item like a used car.
3. **Inject suspicious activity.** `--n_flagged` picks that many distinct sampled accounts and
   fires a rule injector each, weighted by `account_type_weight` in `thresholds.yaml` (e.g.
   international wire activity is weighted toward companies). Most flagged accounts get one
   rule; per `thresholds.yaml`'s `rule_overlaps.pair_probability`, some instead get a pair of
   rules from a small, explicit, editable list of plausible overlaps (e.g. structuring cash
   deposits also tripping the excessive-daily-deposit rule), fired within a shared ~30-day
   window so both genuinely cluster into one alert period - real alerts package overlapping
   rule hits together, they don't treat them as unrelated cases. A handful of rules are
   **party-group scoped** (`Acct/Party = P` in the rule_id) rather than account-scoped - the
   injected activity spreads across a flagged account and any other sampled account sharing its
   `party_id` (e.g. a business owner's personal and business accounts), not just the one account.
   `--rule_config` is a power-user override for an exact rule_id + count mix instead of a random
   weighted pick.
4. **Validate.** `validate.py` runs 15 gates against the full transaction set before anything is
   written - see [Validation](#validation) below. Only a hard `FAIL` blocks export.
5. **Export.** Transactions and customer info are partitioned **per bank** (a bank's own extract
   only shows its own accounts' legs), plus one combined workbook across all banks and a unified
   answer key.

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
Company rows also carry `owners` (a comma-separated `entity_id:pct` list, e.g.
`"PERS1002:60,PERS1008:40"` for joint ownership) on the ~18 companies with a named owner from the
person roster - see `scripts/assign_business_ownership.py`. The other ~16 companies stay unowned
by any roster person, which is itself realistic for larger/institutional entities. Every company
now has a real 3-digit `naics_code`/`naics_description` (`scripts/backfill_legacy_naics.py`
closed the last gap on 11 legacy rows). `party_id` (`scripts/add_party_id.py`) groups a person
with their employer under one shared ID - most rows are their own singleton party, but ~14
accounts across 6 parties share an ID with a linked employer/business - used by the party-group
rules (see "How a run works" above).

### `thresholds.yaml`

One entry per `rule_id`, each with `built: true/false` and its firing parameters (amounts,
window days, minimum counts, `account_type_weight`). **16 of the 23 Alert Matrix rules are built
and injectable today.** The remaining 7 are deliberately out of scope right now, not "coming
soon": `AML-EXT-ACT-KEY-A-LST-EXT` (a whitelist/suppression filter, architecturally different from
every other rule - it isn't a typology injector at all) and 6 Historical Profile Deviation rules
(would need genuine rolling-baseline modeling nothing in this codebase does yet) are both
deprioritized per explicit call. Only the $10,000 CTR threshold (31 CFR 1010.311) is settled
regulation - every other number, including every rule added in the most recent build pass, is a
DRAFT business/risk judgment call. See the file's own header comment before treating any number
as final. Also holds `rule_overlaps`: the small, explicit list of rule pairs allowed to fire
together on the same flagged account (see "How a run works" above) - add/remove pairs and retune
`pair_probability` here as the concept gets validated.

### `config/payment_ranges.yaml`

Segment-based typical value/volume ranges per payment rail, researched from Federal Reserve,
Nacha, BLS, and small-business cash-flow data. Persons are segmented `P1`-`P6` by `income_level`
× `employment_status` (computed, not stored). Companies are segmented into 11 industry archetypes
(RESTAURANT, RETAIL, GAS_STATION, MANUFACTURING, PROFESSIONAL_SERVICES, REAL_ESTATE,
DELIVERY_LOGISTICS, FINANCIAL_SERVICES, IMPORT_EXPORT, WHOLESALE_DISTRIBUTION, FACILITY_SERVICES)
by an explicit `entity_id` list - a deliberate classification, not derived from `naics_code` (a
company can carry a real NAICS code today and still need its archetype set by hand here). Also
holds per-archetype rent/utilities/restocking/revenue-mix tables, `size_tiered_payment_ranges`
(company wire/check amount bands by size tier - small/mid/large, via `transaction_scaler` - not
by archetype, since a wire's realistic floor/ceiling is driven by business size regardless of
industry), and the ATM daily-cap-by-segment table. Edit ranges directly to recalibrate a segment;
move an `entity_id` between the `company_segments` lists to reclassify a company.

### `config/transaction_categories.yaml`

The regularity taxonomy (fixed_recurring, variable_recurring, variable_discretionary,
mechanically_constrained) behind rent/utilities/restocking/payroll/ATM cash/etc. - documents
*how* each category behaves and points to where its actual numbers live in
`payment_ranges.yaml`, without duplicating them.

### `config/high_risk_countries.yaml`

The fictional watchlist used by the CTY (high-risk-country transfer) and EOP (international
wire-burst) rules - real, public FATF-style high-risk jurisdictions; counterparty names/BICs
generated against it are fictional.

### `config/trade_countries.yaml`

A separate list of 12 ordinary major US trading-partner countries (Canada, Mexico, Germany,
Japan, etc.), used only for **legitimate** international wire activity
(IMPORT_EXPORT/MANUFACTURING/WHOLESALE_DISTRIBUTION companies) and EST's occasional international
counterparty - kept entirely disjoint from `high_risk_countries.yaml` so "this wire went overseas"
isn't itself a tell; only the specific high-risk codes are meant to correlate with a rule firing.

---

## Output files

### Transactions (student file)

`transaction_id, entry_id, date, time, account_id, counterparty, amount, direction, currency,
bank_name, owner_name, payment_type, source_description, post_date, in_package, swift_code,
travel_rule_info, originator_beneficiary_info, counterparty_country_code, atm_id, atm_location`

`amount` is always non-negative; `direction` (`debit`/`credit`) carries the sign. `atm_id`/
`atm_location` are populated only on cash rows. `in_package` is `y` if `account_id` is one of the
sampled persons/companies (the same roster `customer_info` is built from), `n` for everything
else - a merchant, a supplier, a synthesized landlord/utility company, any external counterparty.
It's independent of the answer key: a real alert drags in related accounts/parties for review
whether or not they personally turn out to have flagged transactions, so most `in_package=y` rows
have no injected activity at all.

The last 4 fields are populated only on `payment_type == 'wire'` rows, modeled on real SWIFT
MT103/Travel Rule practice: `swift_code` (this row's own account's bank BIC), `travel_rule_info`
(the 31 CFR 1010.410(f) transmittal record - originator/beneficiary name, address, account, and
beneficiary institution+BIC - identical on both legs), `originator_beneficiary_info` (a SWIFT
Field 70-style purpose string, drawn from one shared template pool regardless of `is_laundering`
so it carries no tell on its own), and `counterparty_country_code` (`"US"` for a domestic wire,
the real ISO code for an international one). No column here reveals which rows are suspicious.

### Customer info

`customer_id, account_number, type, name, address, type_of_business, bank_name, alert_rule`

`type_of_business` shows a company's NAICS description or a person's own occupation
(`scripts/assign_occupations.py`) - same column, content depends on row type.

`alert_rule` is a comma-separated list of the rule_id(s) that fired on that account (blank for
the large majority with none) - standard in real AML alert systems, and it's what lets a student
search for/recognize a specific typology in the data instead of only discovering it by
re-deriving the answer key. An account can list more than one rule_id if it got a paired alert
(see "How a run works" above).

### Answer key (grading only - not part of the student file)

Rows with no `rule_id` (the vast majority) keep a thin format: `entry_id, is_laundering, rule_id,
typology, role_in_typology, difficulty`. Rows that actually contributed to a rule firing
(`rule_id` populated) carry the **full transaction record** inline - every student-file column
plus the label fields - so the grading file is self-contained and doesn't require a join back to
the transaction files to see what the flagged transaction actually was.

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
injector function per subtype (`inject_dst`, `inject_eat`, `inject_cty`, `inject_mbd`,
`inject_est`, `inject_eop`, `inject_ftf`), firing on that rule's actual textual definition rather
than a generic pattern library. A `Scope = P` (party group) rule's injector spreads its activity
across every sampled account sharing the flagged account's `party_id`, not just the one account -
`EXT` (a suppression filter, not a typology) is the one subtype with no injector at all.

---

## Validation

`validate.py` runs 15 gates before any file is written:

1. No duplicate `entry_id`
2. Debit/credit legs of a `transaction_id` balance to zero
3. `running_balance` reconciles *(skipped - field not yet in schema)*
4. `direction` is consistent with a non-negative `amount`
5. `post_date` never falls on a weekend/federal holiday
6. `post_date` never precedes the transaction timestamp
7. Fields that should be blank for a row's type are blank (e.g. `atm_id` only on cash rows)
8. A person's `ccard`/`credit`/`debit`/`pos` rows are always `direction == debit` (a person
   receiving a card-network credit isn't a modeled flow)
9. `swift_code`/`travel_rule_info`/`originator_beneficiary_info` are populated iff
   `payment_type == wire`
10. Every BIC is 8 or 11 characters *(skips only if the run has zero wire transactions at all)*
11. No BIC matches a real institution *(same skip condition as above)*
12. Injected rows fall within the configured date range
13. No negative `running_balance` *(skipped - field not yet in schema)*
14. **Leakage** - no column predicts `is_laundering` above base rate
15. **Clearing float gap** - a check/c_check's credit leg posts on or after its debit leg, within
    a sane window

Only a hard `FAIL` blocks export (gates 1-2, 4-9, 12 - real correctness bugs). Gates 14-15 return
`WARN` and still write output: a leak-free answer key isn't the goal, and at small account counts
some incidental correlation (e.g. a specific bank's `swift_code`, an ATM branch from a small
per-bank pool) is close to unavoidable, same as a check's clearing leg occasionally landing just
past the exercise's own date window. `payment_type`, `date`, `in_package`,
`counterparty_country_code`, `travel_rule_info`, and `originator_beneficiary_info` are exempt
from the leakage check entirely - each is legitimately part of a rule's own evidentiary signature
or identity information (a cash-equivalent rule concentrating on `payment_type='cash'`, a
structuring rule clustering on specific days, the CTY rule being *defined* around specific
high-risk country codes), not an incidental leak. `swift_code`/`atm_id`/`atm_location` are **not**
exempt, so a small run can still show a leakage `WARN` on one of those - expected small-sample
noise, not a defect (see Known limitations).

Run it standalone against an already-exported file:

```bash
./aml-env/bin/python3 validate.py data/aml_dataset_combined.xlsx --start_date 2025-01-01 --end_date 2025-03-31
```

---

## Project layout

```
main.py                          CLI entry point - sampling + relational closure, injection
                                  dispatch, export, answer-key assembly
generator/
  transactions.py                Core engine: legit activity, payroll, self-employment/
                                  unearned income, rent/utilities/restocking, owner
                                  distributions, company revenue, payment-rail selection,
                                  ATM mechanics
  rule_injectors.py               One injector per built rule subtype (DST/EAT/CTY/MBD/EST/EOP/FTR)
  rule_taxonomy.py                Parses the 7-token rule_id
  exporter.py                     Per-bank CSV/XLSX writers
utils/
  helpers.py                      Seeded UUIDs, timestamps, double-entry ledger splitting,
                                  transaction descriptions, wire/BIC generation
validate.py                       Pre-export validation gates
thresholds.yaml                   Per-rule firing parameters
config/
  payment_ranges.yaml              Payment-rail/segment/archetype/size-tier calibration
  transaction_categories.yaml      Regularity taxonomy for recurring categories
  high_risk_countries.yaml         CTY/EOP rule watchlist
  trade_countries.yaml             Ordinary international trading partners (legitimate wires)
agents/
  agent_profiles.xlsx              Identity source (Combined_Data sheet)
scripts/                          One-off calibration/migration scripts already run against
                                  agent_profiles.xlsx (kept for reference, not part of a normal run)
```

---

## Known limitations

- `AML-EXT-ACT-KEY-A-LST-EXT` (a whitelist/suppression filter, not a typology injector) and 6
  Historical Profile Deviation rules are deliberately deprioritized, not scheduled - see
  `thresholds.yaml`.
- Every threshold number besides the $10,000 CTR figure is a DRAFT judgment call pending sign-off
  - this now includes every parameter on the more recently built rules (EST/EOP/FTF), not just the
  original 8.
- `running_balance` doesn't exist in the schema yet, so 2 of the 15 validation gates
  (`running_balance_reconciliation`, `no_negative_running_balance`) are permanently `SKIP` until
  that's built. The BIC gates only skip in the edge case of a run with zero wire activity at all.
- At small account counts (roughly `n_persons + n_companies` under ~15), expect occasional
  leakage warnings - a small per-bank ATM/branch pool, or (since more wire-heavy rules were added)
  a bank's own `swift_code` showing mild correlation purely from a handful of wire rows landing on
  the same bank by chance. Accepted small-exercise noise, not a defect, and doesn't block output.
- `account_type_weight` in `thresholds.yaml` is set **per rule_id**, not per rule category - a
  category with more rule_id variants (e.g. ATM's 3, EOP's 4) gets proportionally more total
  selection weight unless each variant's own weight is deliberately divided down to compensate.
  This has already caused one real imbalance (ATM dominating person-account selection, EOP
  dominating company-account selection) that got caught and fixed 2026-07-15 - worth checking this
  math again any time a category's variant count changes.
- `rule_overlaps` in `thresholds.yaml` currently has 2 starter pairs, both built off rules from
  the original 8 - deliberately minimal, meant to be iterated on as the concept gets validated
  rather than a general "any rule can pair with any rule" model.
