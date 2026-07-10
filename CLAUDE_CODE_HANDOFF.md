# Project Handoff: AML Synthetic Data & Alert-Clearing Exercise Generator

**Owner:** Rio Miner — FCI Tradecraft
**Purpose:** Build a reproducible generator that produces Actimize-style alert-clearing
exercises for AML investigator training.
**Handoff date:** 2026-07-08

---

## 1. Read this first

You are picking up a project mid-stream. Three artifacts exist. They are three
attempts at the same thing. Your job is to fuse them into one working system.

| Artifact | What it is | Verdict |
|---|---|---|
| `Actimize_Rules_Alert_Clearing_Ex.xlsx` | 23-rule Actimize alert matrix + customer roster + 1 of 23 data sheets populated | **The spec.** Keep. |
| `github.com/fcitradecraft/Synthetic_Modeling_Network_Data` | Python generator, YAML pattern injection, double-entry ledger, taint propagation | **The engine.** Fix and keep. |
| A series of long ChatGPT prompts | Direct CSV generation, ~60 numbered instructions each | **Abandoned.** Non-reproducible. |

Do not start from scratch. The repo has the right architecture. The workbook has
the right specification. Neither is finished.

---

## 2. Mission

Produce a seeded Python generator that, from a config file, emits:

1. `transactions.csv` — the student file. Indistinguishable from a core banking
   or Actimize extract. Contains **no** field that reveals which rows are suspicious.
2. `answer_key.csv` — every injected row, keyed by `entry_id`, labeled with
   `rule_id`, `typology`, `role_in_typology`, `difficulty`.
3. `thresholds.yaml` — the parameter set each rule fires on. Currently missing entirely.
4. `watchlist.csv` — fictional screening list. No real designated persons or entities.
5. A **PASS/FAIL validation block** that refuses to write files if any gate fails.

Same seed → byte-identical output. Every time.

---

## 3. Non-negotiable constraints

### No answer leakage
No column in `transactions.csv` may predict the label above chance. This has
already failed twice:

- **Repo:** `bank_name` was populated on 41 of 41 laundering rows and 0 of 1,553
  clean rows. `P(laundering | bank_name populated) = 1.0`. Also `payment_type ==
  'credit_card'` and certain `source_description` string formats were perfect
  predictors. And `is_laundering` sat in the student file as a boolean column.
- **Workbook:** the substring `ABOR` appears in 8 of 15 invented SWIFT/BIC codes.
  All 16 rows containing `ABOR` are foreign wires; 0 domestic rows contain it.
  Sort the column, the offshore activity separates itself.

**Build a leakage test and wire it into the validator.** For every column,
compute mutual information (or just conditional probability) against the label.
Any column that predicts materially above base rate is a build failure.

### Reproducibility
`random.seed()`, `np.random.seed()`, `Faker.seed()`. Expose `--seed` on the CLI.
The current repo has **no seed anywhere** — two identical commands produced
705 and 668 rows.

### No real identifiers
No real BICs (the workbook currently contains `UPNBUS44` = Regions Bank and
`VALLMTMT` = Bank of Valletta). No real designated persons, terrorist
organizations, or cartels. Fictional counterparties only; screening is taught
via a fictional watchlist file, not via name recognition.

### Regulatory accuracy
- CTRs are filed on **currency** transactions (31 CFR 1010.311). Structuring under
  31 CFR 1010.314 is defined against that currency reporting requirement.
- **Wires do not generate CTRs.** Do not place a sub-$10,000 clustering pattern in
  wire activity and call it structuring. The workbook currently does this on
  2025-09-22 through 09-24 (six wires: $9,513 / $9,553 / $9,907 / $9,804 / $9,815 /
  $9,890). Move it to cash, or rebuild it around the $3,000 funds-transfer
  recordkeeping threshold and label it accurately.

---

## 4. The `rule_id` taxonomy is the config schema

The workbook's `Alert Matrix` sheet encodes each rule as a 7-token ID:

```
AML - STR - CCE - INN - A - D01 - DST
 |     |     |     |    |    |     |
 |     |     |     |    |    |     └─ Subtype (DST=structuring, EAT=excessive activity,
 |     |     |     |    |    |          EOP=excessive over period, EST=excessive single txn,
 |     |     |     |    |    |          HBS=historic burst, FTR=flow-through, CTY=country, EXT=extension)
 |     |     |     |    |    └─ Time Period (S01=single txn, D01/D05/D07/D30=days, M01=1 month, LST=list-based)
 |     |     |     |    └─ Scope (A=account, P=party group)
 |     |     |     └─ Direction (INN=credit/in, OUT=debit/out, ALL=both, KEY=key-based)
 |     |     └─ Tran Type (CCE=cash/cash-equiv, EFT, IFT=intl EFT, ATM, ACT=account, ALL)
 |     └─ Category (STR, ATM, ECT, EBB, EBO, EFT, EXT, FTF, HBC, HBE, HBI, TSD, MBD)
 └─ Business line (AML)
```

**Parse this. Dispatch on it.** `rule_id → pattern function → injected rows`.
You do not need to hand-write 23 pattern configs. You need one parser and a
dispatch table.

---

## 5. Blocking gaps in the spec

Eleven of the 23 rules **cannot be evidenced** with the current schema and date
range. These must be resolved before generation, not after.

| Gap | Rules affected | Fix |
|---|---|---|
| No `branch_id` field | Multiple Branches Deposits | Add `branch_id` to schema |
| No `party_id` field | 3 "Party Group" rules (ATM, Burst Beneficiary, Burst Originator) | Add `party_id` linking accounts to a customer relationship |
| No pre-period history | 6 "Historical Profile Deviation" rules | Extend date range back 12 months; emit a `baseline` period flag |
| No exclusion-list artifact | Filter Accounts On Accounts White List | Emit `whitelist.csv` |
| `Review Date Range` column 100% empty | **All 23** | Populate. This defines the analyst's lookback window. |
| No thresholds anywhere | **All 23** | Build `thresholds.yaml`. Rules say "above the established threshold" — no threshold is defined in the workbook. |

**Without thresholds and a review date range, no alert in this exercise is
solvable.** The analyst has no basis on which to call true or false positive.
This is the single highest-priority gap.

---

## 6. Known defects to fix

### In the workbook (`Actimize_Rules_Alert_Clearing_Ex.xlsx`)
- 22 of 26 sheets are empty (`A1:A1`). Only `Alert Matrix`, `Sheet1`,
  `HighRisk Country`, `Customer Accounts` have content.
- `Customer Accounts`: 23 customers map 1:1 to 23 rules in matrix row order
  (accounts `4056079101`–`4056079123`). No customer triggers two rules; no
  customer is clean. Production alert populations are majority false-positive
  and routinely have one customer firing several rules. Rebuild the roster.
- `alert_reason` column in `Customer Accounts` is an answer key. Move it out.
- `HighRisk Country` sheet: 31 of 32 wires are credits, 1 is a debit. $1.3M in,
  $85K out. No layering-out leg.
- Malformed BICs: `BABORCY`, `BABORMX`, `BABORPA`, `CABORKY`, `EABORAE`,
  `FABORKY`, `MABORPH`, `PNBORPH` are all 7 characters. A BIC is 8 or 11.
- Inconsistent prefix spacing: `SWIFT:ABORAEAE` vs `SWIFT: MASHAEAE`.
- `Excessive Monthly ATM Withdrawals` description says "over a 1-day period";
  its `rule_id` says `M01` (1 month). Copy-paste error.
- `Flow Through of Funds` description contains an unresolved TODO in the cell:
  "(? is there a ratio-threshold... Amounts must be within XX% of one another?)"
- `Filter Accounts On Accounts White List` description begins "*Unclear*".
- `Cat-2?` column (note the question mark) is populated on all 23 rows but
  `Cat-2?-expl` is empty on all 23. It appears to just restate the last rule_id token.
- TADECO LLC (a trust account) shows 629 **POS debits**. POS for a bar is
  merchant settlement — a credit. This contradicts the owner's own written spec.
- Missing schema fields: `branch_id`, `party_id`, `customer_id`,
  `running_balance`, `ctr_flag`.

### In the repo (`Synthetic_Modeling_Network_Data`)

**Environment: the owner is on macOS.**

The code **runs**. All four execution paths were verified from a clean clone:
`main.py` bare, `--patterns` (all 3 YAML configs), `--agent_profiles`, and
both combined. The inability to run it is an environment problem, not a code
problem. Ranked causes:

1. **Python version floor.** `requirements.txt` pins `numpy==2.2.6`, which
   requires Python **>=3.10**. macOS ships `/usr/bin/python3` as **3.9.6**.
   Installing against system Python fails with
   `Could not find a version that satisfies the requirement numpy==2.2.6`,
   nothing installs, and `python3 main.py` then throws `ModuleNotFoundError`.
   The committed `__pycache__/*.cpython-312.pyc` files confirm the original
   author used Python 3.12.

2. **Homebrew PEP 668 guard.** If Python 3.10+ was installed via Homebrew,
   `pip3 install -r requirements.txt` outside a venv fails with
   `error: externally-managed-environment`.

3. **README's clone URL is a dead placeholder** —
   `git clone https://github.com/your-org/aml-synthetic-generator.git`.
   Following the README literally fails at step one.

4. Possible missing Xcode Command Line Tools if any dependency falls back to a
   source build (`xcode-select --install`).

**Verified working sequence on macOS:**
```bash
python3 --version                 # if < 3.10, install 3.12 (brew or python.org)
python3.12 -m venv .venv
source .venv/bin/activate
python -V                         # confirm 3.12.x, and that you are IN the venv
pip install --upgrade pip
pip install -r requirements.txt
python main.py --patterns config/patterns_smurfing.yaml --format csv --output data/test.csv
```

Note the `~$aml_smurfing_structuring.xlsx` Excel lock file committed under
`data/` — someone had that workbook open in Excel when they committed. If Excel
holds a lock on an output path, the exporter will fail on write. Close Excel
before running.

**Fix the environment first. Then fix the code:**

1. **No seed anywhere.** Add seeding + `--seed` CLI arg.
2. **`is_laundering` sits in the student output.** Split into `answer_key.csv`,
   joined on `entry_id`.
3. **Three perfect leaks:** `bank_name` (populated only on laundering rows),
   `payment_type == 'credit_card'`, and `source_description` string format.
4. **`patterns_smurfing.yaml` does not produce structuring.** Its laundering rows
   are $125 / $200 / $500 (min/median/max). Structuring is defined against the
   $10,000 CTR threshold. Rewrite: deposits $8,200–$9,800, clustered in a 5-day
   window, across ≥3 accounts, aggregating above $10,000.
5. **No `running_balance`.** Add, computed cumulatively from an opening balance.
6. **No `ctr_flag`.** Compute from same-day same-account cash aggregate > $10,000.
   Never assign it.
7. **`post_date` carries a time component** (`2025-01-27 14:47:11`) and is a random
   0–3 day lag. Should be: same day if a business day, else next business day
   (US federal holiday calendar). No time component.
8. **`--legit_txns 500` produces 638 rows.** The flag doesn't control what it claims.
9. **Amounts are wrong.** Median transaction $202. A utility bill to "Gotham Light
   and Power" is $187.29. Recalibrate against the agent profile.
10. **Repo hygiene:** `.DS_Store` (×2), `__pycache__/*.pyc` (×9), and an Excel lock
    file `~$aml_smurfing_structuring.xlsx` are all committed. Add a `.gitignore`.

**Keep from the repo:** the double-entry ledger design (each `transaction_id`
emits `{id}-D` and `{id}-C` legs with matching amounts and opposite signs — this
is how a real core system works and is better than single-entry), the YAML-driven
pattern injection architecture, taint propagation across chains, and the
agent-profile-driven transaction model.

---

## 7. The validator

Write `validate.py`. It runs before any file is written. Hard-fail on:

1. Duplicate `entry_id`
2. Debit and credit legs of a `transaction_id` that don't balance
3. `running_balance` not reconciling to `opening_balance + Σcredits − Σdebits`
4. `credit_debit` inconsistent with `transaction_type` on any row
5. `post_date` on a weekend or US federal holiday
6. `post_date` earlier than `tran_date`
7. Any field populated that the schema says must be blank (and vice versa) —
   e.g. `atm_id` on a non-ATM row, `wire_details` on a non-wire row
8. Any BIC not 8 or 11 characters
9. Any BIC matching a real institution (maintain a small deny-list)
10. Injected rows falling outside the configured date range
11. Negative `running_balance` unless `OVERDRAFT_ALLOWED`
12. **Leakage:** any column whose value predicts the label above base rate

Print a PASS/FAIL block. On FAIL, write nothing.

Gate 12 is the one that matters most. It is the defect that has now appeared
independently in two separate tools.

---

## 8. Order of work

**Phase 0 — Environment.** The owner is on **macOS**. Check `python3 --version`
first — if it is 3.9.x (Apple's system Python), the pinned `numpy==2.2.6` will
not install, because it requires >=3.10. Install Python 3.12, create a venv,
install requirements, and confirm
`python main.py --patterns config/patterns_smurfing.yaml` produces output.
Walk the owner through each command before executing it and explain what it does.
He is a step-by-step learner on terminal tasks; he is not a developer.

**Phase 1 — `validate.py`.** Write the validator first, against the *existing*
broken output. Watch it fail. That's the baseline.

**Phase 2 — Seed and de-leak.** Add seeding. Split the answer key out. Kill
`bank_name`, `credit_card`, and `source_description` leakage. Re-run validator.

**Phase 3 — Schema.** Add `branch_id`, `party_id`, `customer_id`,
`running_balance`, `ctr_flag`. Fix `post_date`. Extend date range 12 months
back for historical baselines.

**Phase 4 — `thresholds.yaml`.** Define, for all 23 rules, the firing threshold
and the review date range. This unblocks the exercise.

**Phase 5 — `rule_id` parser + dispatch.** One function per subtype
(DST, EAT, EOP, EST, HBS, FTR, CTY, EXT). Parse the ID, dispatch, inject.

**Phase 6 — Generate all 23 sheets.** One command, one seed, validator green.

**Phase 7 — Package.** `aml-datagen` becomes a reusable Skill: SKILL.md, the
rule taxonomy, the validator, the typology library, the watchlist convention.

Do not skip Phase 1. A generator without a validator is how both prior attempts
failed silently.

---

## 9. Working style

- BLUF. Military writing. Lead with the answer.
- Challenge assumptions. The owner explicitly prefers the correct answer over
  the diplomatic one. If a design choice is wrong, say so and say why.
- Walk through code and terminal steps one at a time. Show the command, explain
  it, then run it.
- Cite sources only when asked.
- No ghostwriting. The owner is the subject-matter expert on AML; you are the
  engineer, researcher, and framework builder. When a regulatory question comes
  up — thresholds, CTR mechanics, SAR triggers — surface it for his decision
  rather than inventing an answer.

---

## 10. First command

```
Read CLAUDE_CODE_HANDOFF.md. I'm on macOS.

Start by checking my Python version. Section 6 explains why that matters: the
pinned numpy needs 3.10+, and Apple's system python3 is 3.9.6.

Then set up a venv, install requirements, and get
`python main.py --patterns config/patterns_smurfing.yaml` to run.

Walk me through each step before you execute it and explain what each command
does. Do not change any code yet.
```
