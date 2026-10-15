# AML Alert-Clearing Exercise Generator — Status Export

**Prepared:** 2026-09-01 | **Repo:** `Synthetic_Modeling_Network_Data` (fcitradecraft) | **For:** pasting into another AI tool for continued work or review

---

## 1. BLUF

A seeded Python generator produces Actimize-style AML core-banking datasets for training investigators to clear alerts. It samples a headcount of persons/companies, generates realistic legitimate financial activity for all of them, injects rule-accurate suspicious patterns into a chosen subset, validates the output, and exports per-bank student files + a customer-info file + a separate answer key.

**16 of the 23 rules in the source "Alert Matrix" spec are built and injectable.** The remaining 7 are deliberately deprioritized (not blocked, not forgotten): 1 whitelist-suppression rule and 6 rolling-baseline "Historical Profile Deviation" rules that need infrastructure this codebase doesn't have yet.

**The single most important fact for whoever picks this up next: roughly five weeks of finished, verified work (2026-07-11 through 2026-07-15) is sitting uncommitted in the working tree.** It has never been committed or merged to `main`. This should be triaged before any new feature work starts — see §6.

---

## 2. What this project is

Client: Rio Miner, FCI Tradecraft — the AML/regulatory subject-matter expert on this project (not the engineer). The generator exists to produce training exercises: realistic bank transaction data where a known subset of accounts exhibits specific, spec-defined money-laundering typologies, so investigators can practice clearing alerts against it.

Everything is dispatched off a 23-rule **Alert Matrix** defined in `Actimize Rules_Alert Clearing Ex.xlsx` (the ground-truth spec). Non-negotiable constraints that have held since the project's rebuild:
- No answer-leaking columns in the student-facing file
- Full reproducibility (same `--seed` + same args → byte-identical output)
- No real BICs, real institutions, or real designated persons
- CTRs filed on currency only (31 CFR 1010.311); wires never generate CTRs

---

## 3. Architectural history — why it looks the way it does

**2026-07-08 — the rebuild.** A prior implementation (`generator/patterns.py`) was a generic "typology pattern library" (cycles, fan-out, scatter-gather, fan-in primitives) that got reactively patched bug-by-bug until a validator passed, without ever tying back to what a real investigator would need to see to clear a specific rule. Rio's explicit correction: stop patching, rebuild from the Alert Matrix outward. This produced the current architecture:
- `generator/rule_taxonomy.py` — parses the 7-token `rule_id` format
- `thresholds.yaml` — one entry per `rule_id`, firing parameters, `built: true/false` flag
- `generator/rule_injectors.py` — one injector function per rule *subtype*, each firing on that rule's actual textual definition

**Working principle since then:** every new typology feature must trace to a specific `rule_id`'s textual definition in the spec. If a change would only make an abstract pattern "more realistic" without tying to a specific rule, that's a signal to stop and ask, not to keep building.

**2026-07-09 through 07-10 — realism pass.** Fixed silent non-determinism (`uuid.uuid4()` and Faker each had their own RNG stream outside the `--seed` control — reproducibility looked like it worked but didn't until both were wired to the seed). Built relational-sampling closure (sampling a person pulls in their employer/owned companies so nobody's counterpart is missing). Added business ownership, person occupations, ATM-vs-branch cash split, multi-rule paired alerts, and the `in_package`/`alert_rule` columns that make an exercise file self-describing.

**2026-07-11 — check/cashier's-check overhaul.** Fixed a bug where check/ACH descriptions were built from a random unrelated fake name instead of the real counterparty (present across 5+ generator functions — found once, but recurred in ACH and again flagged-not-fixed in P2P). Added realistic clearing float (credit leg posts days after debit leg) and a "cashier's-check-via-cash-GL" mechanic that intentionally produces a legitimate-but-alert-shaped false positive (cash withdrawal + same-day GL deposit) — this is deliberate: teaching investigators to correctly clear a false alarm is as much the point as teaching them to catch a real one.

**2026-07-13 — audit and cleanup.** Found and fixed the same fake-name bug in ACH (the highest-value fix — ACH is the most common rail in the system). Removed dead code (`generate_legit_transactions`, orphaned since an earlier class hierarchy was deleted), a debug print firing on every transaction, and stale scratch files.

**2026-07-14 — data-quality and injector realism.** Root-caused a broken revenue mix on one company (Arrakis Cantina & Bar showing 94% ACH, $4,600–7,500 pieces — reads as an alert, not a restaurant) to a miscalibrated `average_expense`/`transaction_scaler` pair that pushed every transaction above card/cash range ceilings, forcing ACH by the amount-fallback logic every time. Fixed the instance and added a systemic safeguard (clip `average_expense` to the archetype's own rail ceiling). Fixed ATM rule injectors to reuse the same denomination-rounding/daily-cap logic legitimate ATM activity already used (previously produced physically impossible amounts like `$591.82` from an ATM). Rebalanced `account_type_weight` for ATM rules after confirming they were structurally over-weighted.

**2026-07-14/15 — company payment-type overhaul (research-grounded).** This was the big one. Research into Nacha, Fed payment studies, and FFIEC/FinCEN cash-intensive-business guidance revealed the whole payment-rail/amount system was structurally wrong: it drew an amount first, then found which rail's range happened to contain it — which meant wire (5–6 figures) could never coexist with a $50 card range in the same draw, so wire-heavy company archetypes (real estate, financial services, import/export) barely used wire despite 55–65% target weight. Fixed by inverting the order: pick rail first (weighted), draw amount from that rail's own range second. Added size-tiered wire/check ranges (small/mid/large business, not per-industry — a wire's floor scales with company size regardless of sector) and moved consumer-facing revenue (restaurant/retail/gas station) to aggregated daily card-batch settlement instead of one row per walk-in sale, avoiding a 15–50x row-count explosion.

**2026-07-15 — wire messaging detail.** Added SWIFT/BIC, Travel Rule transmittal data (31 CFR 1010.410(f)), and SWIFT Field-70-style purpose strings to every wire row, grounded in actual MT103 field conventions. Found and fixed a real architectural bug along the way: validation was running *before* bank/BIC fields were resolved on the transaction rows, so two validation gates had been silently no-op SKIPping since they were added.

**2026-07-15 (continued, same day, multiple sub-sessions) — international wires, the last 6 rules, and a rebalancing bug.**
- Added legitimate (non-injected) international wire activity for trade-heavy archetypes, so "this wire went overseas" isn't itself a tell — only specific high-risk country codes should correlate with a rule firing.
- Resolved the one rule that had been blocked on an open interpretation question (FTF — "in/out must be within 90% of each other" — Rio resolved this as a symmetric ratio band, not a one-sided floor) and built it.
- Built the remaining party-group and burst-concentration rules (EOP, EST), bringing the total to **16 of 23 rules built** — every rule that isn't explicitly deprioritized is now built.
- Found and fixed a real selection-weight bug: `account_type_weight` in `thresholds.yaml` is set *per rule_id*, not per rule *category*. A category that had grown more `rule_id` variants than its peers (ATM: 3 variants, EOP: 4 variants) was silently getting proportionally more total selection probability every time — confirmed via direct observation of skewed real output ("ATM firing most often, others not firing at all"), not from code reading. Fixed by dividing each variant's weight down to match peer categories' total share. **This is now a standing gotcha documented in the README's Known Limitations — must be re-checked any time a rule category's variant count changes.**
- README fully refreshed to reflect all of the above (it had drifted ~2 weeks stale).

---

## 4. Current state of the codebase (as of 2026-09-01, reflects working tree, not just last commit)

### Rule coverage: 16 / 23 built
- **Built (16):** DST (structuring, 2 variants), EAT (excessive activity: ATM D05/M01, ECT-INN/OUT — 4 variants, 2 of which are party-group scoped), EST (excessive single transaction, 2 variants), MBD (multi-branch deposit), CTY (high-risk-country transfer), EOP (burst concentration — EBB/EBO × account/party scope, 4 variants), FTF (flow-through / in-out ratio).
- **Deliberately not built (7):** `AML-EXT-ACT-KEY-A-LST-EXT` (a whitelist/suppression filter — structurally not a typology injector at all, architecturally different from the other 22; parked pending Rio's prioritization) and 6 Historical Profile Deviation rules (need genuine rolling-baseline modeling — nothing in this codebase does that yet; deprioritized by Rio's earlier explicit call).

### Pipeline (`main.py` orchestrates)
1. **Sample** persons/companies from `Combined_Data` in `agents/agent_profiles.xlsx` (pool caps: 22 persons / 34 companies today). Merchants, bank branches (`BEnt`), and banks are never sampled down — always fully available as counterparties. Sample count is a *floor*: relational closure pulls in employers/owned companies so nobody's employer or business is left dangling.
2. **Generate legitimate activity** for every sampled account: payroll or self-employment/unearned income, rent/mortgage + utilities, owner distributions, merchant purchases (rail chosen only from methods whose range actually contains the amount), company revenue + restocking (rail-first, size-tiered for B2B, daily-batch for consumer-facing), legitimate international wires for trade-heavy archetypes, ATM vs. branch cash mechanics.
3. **Inject suspicious activity** into `--n_flagged` accounts, one rule injector each (weighted by `account_type_weight`), with some paired into a shared alert window and some spread across a `party_id` group instead of a single account.
4. **Validate** — 15 gates, only a hard FAIL blocks export (duplicate IDs, ledger balance, date/weekend logic, field-population-by-type, wire-field consistency, BIC format/no-real-institutions, leakage, clearing-float sanity). Leakage and clearing-float gates are advisory WARNs, not blockers, with explicit documented exemptions (e.g. `payment_type`, `date` — these *are* a rule's own evidentiary signature, not incidental leakage).
5. **Export** — per-bank CSV/XLSX split (mirrors real core-banking visibility: a bank only sees its own accounts' legs), one combined multi-sheet workbook, one unified answer key.

### Known open items (not yet started)
1. `AML-EXT-ACT-KEY-A-LST-EXT` whitelist filter — deprioritized, parked until Rio asks for it.
2. Total company revenue lands ~1.3x over each archetype's target (root cause identified: residual ACH/check weight draws from a disproportionately wide range relative to its small transaction count) — flagged as DRAFT-quality, not chased further.
3. `p2p` description bug in `describe_transaction` (`utils/helpers.py`) — same fake-unrelated-counterparty-name bug that was already fixed in check and ACH, same fix pattern proven twice, just not applied here yet.
4. MSB/third-party check-cashing mechanic for gas stations — scoped in detail 2026-07-11, ready to implement, not started.
5. Extend weighted payment-type selection to self-employment income's client-payment table and restocking's supplier payment — both still use flat `random.choice`.
6. A full DRAFT-number calibration pass across all 16 built rules' thresholds/windows/ratios now that all 23 rules are resolved (only the $10,000 CTR figure is settled regulation; everything else is a placeholder pending Rio's sign-off).

---

## 5. Working style / constraints established with Rio (useful context for another AI tool)

- Rio is the AML subject-matter authority; an AI assistant should not invent answers to regulatory questions (CTR thresholds, SAR triggers, structuring mechanics) — surface them for his decision.
- He is not a developer; wants terminal/setup steps explained and run one at a time, not batched ahead.
- Prefers BLUF, direct correction over diplomatic hedging.
- Build features from the rule spec outward — never extend a generic pattern library and then patch it until a validator passes.
- After any change to generation/injection/labeling logic, verify with a multi-seed sweep (8–10 seeds), not a single passing run — this project has twice shipped a change that passed on one seed and failed on 60%+ of others (a `--seed` reproducibility gap, and a schema change that exposed real label leakage).

---

## 6. Recommended next steps

**Before any new feature work:**
1. **Triage the uncommitted working tree.** `git status` shows ~5 weeks of verified, working changes (README, both core generators, `main.py`, `thresholds.yaml`, `validate.py`, config, plus ~9 untracked one-off calibration scripts and a `data/` output directory) sitting unstaged on branch `codex/add-instructions-for-modifying-output-in-readme-knvmys`, which is 3 commits ahead of `main` with nothing merged back. This is the single biggest risk to the project right now — it's one bad `git checkout` or stale-branch cleanup away from losing real work. Recommend: review the diff, commit in logical chunks (or as one squashed commit against the session log above), and open a PR against `main`.
2. Decide whether the 9 untracked `scripts/*.py` one-off calibration scripts (already run once against `agent_profiles.xlsx`, not part of a normal run) should be committed for provenance/reference or left out — README currently documents them as "kept for reference."
3. Confirm whether `data/` (currently untracked, contains a full generated exercise + answer key, several MB) should be gitignored rather than left as untracked working output.

**After the tree is clean, in priority order per the open-items list above:** the DRAFT-number calibration pass (item 6) is probably highest-leverage now that all 23 rules are resolved — it's the one thing standing between "functionally complete" and "ready to hand to a real trainee," and needs Rio's judgment rather than more engineering.

---

*This file was generated by researching git history, project memory notes, README.md, thresholds.yaml, and the current working tree diff. Source of truth for anything time-sensitive is the repo itself, not this snapshot.*
