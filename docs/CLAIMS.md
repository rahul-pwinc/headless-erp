# Claims register

Every claim this project makes, with its evidence, how it was verified, and how
strongly it is supported. Written for a reader who assumes the project is
overselling and wants to find where.

## How to read this

**Verification method**

| Method | Meaning |
|---|---|
| `source` | Read in the vendored ERPNext or Frappe source. Not executed. |
| `empirical` | Measured against a live ERPNext instance, result committed in `reports/`. |
| `both` | Source condition read *and* the behaviour reproduced empirically. |
| `derived` | Arithmetic or cross-reference over committed data in this repo. |
| `inference` | Reasoned from the above. Not measured. Treat as a hypothesis. |

**Strength**

| Rating | Meaning |
|---|---|
| A | Source and empirical agree, and the measurement has no known confound. |
| B | Supported, but on one instance, one version, or one configuration. |
| C | Supported, but the measurement has a confound named in the notes, or it is one leg of a two-leg argument. |
| D | Inference only. Argued, not measured. |
| R | Retracted. See the retraction section. |

## Version and reproducibility caveat, stated first

The live results in `reports/` were produced against **ERPNext v16.34.1**
(`docker/pwd.yml`).

The source tree checked out at `vendor/erpnext` and `vendor/frappe` is
**17.0.0-dev** (`vendor/erpnext/erpnext/__init__.py:6`,
`vendor/frappe/frappe/__init__.py:144`, commit `72fa7d0`). `vendor/` is
gitignored, so a reader cloning this repo gets neither tree.

Consequence: **every `file:line` citation below points at 17.0.0-dev**, not at
the version the numbers were measured on. The README cites v16.34.1 line numbers
(`accounts_controller.py:1127`, `:97`) that do not resolve in the tree present
here. The conditions are the same in both versions and the v17 locations are
given below, but no single artifact in this repo lets a reader check a v16 line
number. Fixing that means either vendoring a pinned v16 tree or restating every
citation against v17.

**The live instance is also not a clean instance.** It has accumulated fixtures,
2,000+ invoices from the Phase 6 replay, and at least one Pricing Rule that was
not present when the earlier report generation ran. That Pricing Rule changed
the census result between runs (claims 2.1 and 2.16). Any number in `reports/`
is a measurement of that instance in that state, not of stock ERPNext.

## Layer 1: the mechanism in ERPNext

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 1.1 | `AccountsController.set_missing_item_details` writes a derived value onto an item row only when the field is `None`, or when the field is in `force_item_fields`, or for `serial_no`/`batch_no` under `use_serial_batch_fields`. | `vendor/erpnext/erpnext/controllers/accounts_controller.py:781-792` | source | A |
| 1.2 | `force_item_fields` has nine members: `item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`, `weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate`. `rate` and `price_list_rate` are not among them. | `vendor/erpnext/erpnext/controllers/accounts_controller.py:65` | source | A |
| 1.3 | Therefore a caller that supplies `rate` keeps it, and the price list is not consulted for that row. | 1.1 + 1.2, confirmed by `reports/latest.json` mode `assert`: `rate` 250.0 (UI path) vs 1.0 (API path) | both | A |
| 1.4 | This is intended behaviour, not a defect in ERPNext. | The `is None` gate is what lets a manually entered discount survive a re-save. No ERPNext issue or fix is claimed. | inference | D |
| 1.5 | Field-level `read_only` is never consulted in Frappe's server-side save or validate path. It is a rendering hint. | Exhaustive grep for `.read_only` / `"read_only"` in `vendor/frappe/frappe/model/*.py` returns only doctype-level `meta.read_only` (`document.py:1957`) and the request-level `frappe.flags.read_only` (`document.py:2217,2250`). No DocField-level check exists. | source | A |
| 1.6 | 1.5 is a general Frappe property, true of every read-only field on every doctype, and is not an ERPNext discovery. | Follows from 1.5: the check is absent framework-wide, not absent for pricing. | inference | D |
| 1.7 | ERPNext refuses to post an unbalanced voucher. | `process_debit_credit_difference` (`erpnext/accounts/general_ledger.py:473-503`) calls `raise_debit_credit_not_equal_error` (`:536`) whenever `abs(debit_credit_diff) > allowance`, with a single carve-out for Exchange Gain Or Loss journal entries. Verified against erpnext 16.34.1 by `make citations`. | source | A |
| 1.8 | Therefore a balanced trial balance is guaranteed by construction and is evidence of nothing about the correctness of any amount. | 1.7 | inference | A |
| 1.9 | ERPNext's GL layer independently validates account structure: group accounts, inactive accounts, frozen accounts, company and cost-center mismatch. Claims that an agent can "corrupt the books" through these paths are wrong. | `gl_entry.py` validation block, cited in README as `:232-275` (v16 line numbers; not re-verified against the v17 tree for this register) | source | B |
| 1.10 | ERPNext mutates item values during `validate`, after the caller's values are set, when a Pricing Rule applies. | `vendor/erpnext/erpnext/controllers/taxes_and_totals.py:170-221`. Reproduced: `reports/census.json` records Sales Invoice and Delivery Note probes where the caller supplied `rate: 7.0` and the stored value is `225.0`, which is 10% off the 250.00 list price and is neither the caller's value nor the derived value. | both | A |

## Layer 2: the census

`reports/census.json`, produced by `harness/run_census.py`, ERPNext v16.34.1.

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 2.1 | 203 probes across 9 transaction doctypes: 156 protected, 37 silently accepted, 10 rejected. | `reports/census.json` `by_verdict` | empirical | B. **These numbers moved between runs.** An earlier generation of the same report recorded 196 / 144 / 42 / 10 and six accepted fields. The difference is a Pricing Rule that now exists on the instance (claim 2.16), not a change in ERPNext. Any citation of these figures must name the report generation it came from. |
| 2.2 | Seven distinct fields were silently accepted: `rate`, `price_list_rate`, `discount_amount`, `discount_percentage`, `weight_per_unit`, `min_order_qty`, `last_purchase_rate`. | `reports/census.json` `silently_accepted_fields` | empirical | B |
| 2.3 | The 203 is not 203 independent findings. It is a small field set multiplied by the doctypes those fields appear on. | Derived from `reports/census.json` `probes` | derived | A |
| 2.4 | The 10 rejections are one probe repeated, not 10 results. | All 10 are `qty` (mutated to 0.016 by `harness/census.py:57`) failing validation with HTTP 417, on 9 doctypes, plus one `delivered_by_supplier` on Sales Order. | derived | A |
| 2.5 | The 156 "protected" figure overstates the strength of ERPNext's defences. | Most protected fields are arithmetic the server recomputes as a matter of course (`amount`, `net_amount`, `net_rate`, `base_rate`, `base_amount`, `stock_qty`, `gross_profit`), not derivation decisions being defended. | derived | A |
| 2.6 | **28 of the 37 "silently accepted" verdicts had a derived value of 0.0.** | Computed over `reports/census.json` `probes`. The nine with a real derivation are the 8 `price_list_rate` accepts (250.0 on selling doctypes, 120.0 on buying) and 1 `last_purchase_rate` accept on Purchase Order (120.0). | derived | A |
| 2.7 | Consequence of 2.6: for 28 of the 37, the census did **not** demonstrate "the server had a derivation available and used the caller's number instead." It demonstrated "the server had nothing to say and the caller's number stuck." | 2.6 | derived | A |
| 2.8 | The strongly supported core of the census is: **`price_list_rate` is silently accepted on 8 doctypes where the server derives a real value for it** (250.0 on the four selling doctypes, 120.0 on the four buying doctypes). | `reports/census.json`, probes where `fieldname == "price_list_rate"` and `derived_value != 0` | empirical | B. This is the strongest single census result and it has strengthened between runs: the buying-side derivations are now non-zero where they previously were not. |
| 2.9 | `rate` is silently accepted on 6 of 9 doctypes. | `reports/census.json`: accepted on Quotation, Sales Order, Supplier Quotation, Purchase Order, Purchase Receipt, Purchase Invoice; recorded protected on Sales Invoice, Delivery Note, Material Request. | empirical | C. Derived `rate` was 0.0 in every probe, because `get_item_details` returns `rate = ctx.rate or price_list_rate` and the probe sends no `ctx.rate`. Two of the three "protected" verdicts are the Pricing Rule confound (claim 2.16), not a defence. Claim 1.3 establishes the same behaviour cleanly through `reports/latest.json`, which is the citation to use. |
| 2.10 | `weight_per_unit` is in `force_item_fields` and is nonetheless stored as the caller supplied it, on 8 doctypes. | `reports/census.json`, derived 0.0, supplied 7.0, stored 7.0 | empirical | C. The derived value was 0.0 because the fixture item has no weight (`harness/fixtures.py:50-65` sets none). The force-overwrite writing back the caller's own value is a real mechanism, but on a zero-weight fixture it is not distinguishable from the force path simply not firing. Re-run with a weighted item before relying on this. |
| 2.11 | `price_list_rate` is `read_only` on 5 of 9 item child doctypes: Quotation Item, Sales Order Item, Sales Invoice Item, Delivery Note Item, Material Request Item. | Cross-reference of `read_only` in the nine child DocType JSONs under `vendor/erpnext/erpnext/`. Recomputed for this register; matches the README table exactly. | source | A |
| 2.12 | `rate` is `read_only` on 0 of 9. `discount_amount` and `discount_percentage` on 0 of 8 (absent on Material Request Item). `weight_per_unit` on 4 of 8. `min_order_qty` on 1 of 1. | Same cross-reference | source | A |
| 2.13 | "A human cannot type into `price_list_rate`." | **Settings-dependent.** `Selling Settings.editable_price_list_rate` (default `0`, `selling_settings.json`) is consumed by `vendor/erpnext/erpnext/public/js/utils/sales_common.js:326-339` (`toggle_editable_price_list_rate`), which clears the `read_only` flag on the `price_list_rate` docfield when the setting is on. | source | C. True under default settings on selling doctypes. One checkbox makes it false. The claim must always carry "under default settings." |
| 2.14 | Therefore an API write of `price_list_rate` "produces a document no Desk session could have created." | 2.11 + 2.13 | inference | C. Only under default settings, and only for the 5 doctypes in 2.11. State it as such or drop it. |
| 2.15 | The census covers one derivation path. | Every probe calls `erpnext.stock.get_item_details.get_item_details` (`harness/census.py:70-74`). Header-level, tax-table, payment-schedule, and stock-valuation derivations are not probed. | derived | A |
| 2.16 | **The census's binary verdict cannot distinguish "the server defended its derivation" from "an unrelated Pricing Rule overwrote the caller."** | Six probes make this visible directly: `rate`, `discount_amount`, and `discount_percentage` on Sales Invoice and Delivery Note are all recorded `protected`, with stored values of `225.0`, `25.0`, and `10.0` against a supplied `7.0` and a derived `0.0`. Those are the outputs of a 10% Pricing Rule on a 250.00 list price. The caller's value was replaced, but not by the derivation the census was testing. | derived | A. This is a methodological defect in `harness/census.py`, not in ERPNext. The probe asks "did the caller's value stick," which conflates every reason it might not have. Reading a `protected` verdict as evidence of a derivation defence is unsafe on any instance carrying Pricing Rules. |

## Layer 3: the differential

`reports/latest.json` and `reports/gl-evidence.txt`, one Sales Invoice,
list price 250.00, qty 4.

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 3.1 | Omitting `rate` produces zero gaps across 17 compared fields. | `reports/latest.json`, mode `omit`, `gap_count: 0`. Field list at `harness/oracle.py:39-57` (17 entries; the README says 14). | empirical | B |
| 3.2 | Supplying `rate: 1.0` produces 3 gaps: `rate` (250.0 vs 1.0), `discount_amount` (0.0 vs 249.0), `net_rate` (250.0 vs 1.0). Grand total 1000.00 vs 4.00. | `reports/latest.json`, mode `assert` | empirical | B |
| 3.3 | The omit/assert contrast is what makes the finding falsifiable: it distinguishes "the server never derives" from "the server declines to overwrite." | 3.1 + 3.2 | derived | A |
| 3.4 | Both documents post balanced GL entries. | `reports/gl-evidence.txt`: 1000.00/1000.00 and 4.00/4.00, both `balanced=True` | empirical | B |
| 3.5 | 3.4 is not a discovery. Balance is enforced (1.7), so it can only be used in the negative direction: balance is guaranteed, therefore balance proves nothing about correctness. | 1.7 + 1.8 | inference | A |
| 3.6 | The "UI-derivation path" column is a replay, not a browser observation. | `harness/oracle.py:159-183` calls `get_item_details` directly with a hand-built 44-key `ctx` (`:105-156`) reconstructed from `transaction.js`. No browser is driven anywhere in this repo. | derived | A. The replay's fidelity to a real browser session is **unverified**. See [LIMITATIONS.md](LIMITATIONS.md). |
| 3.7 | The write response contains `price_list_rate` and `discount_amount`, so a caller that reads its own response can see the list price it overrode. | `reports/latest.json` records `discount_amount: 249.0` on the API document, read back from the insert response through `harness/client.py:44-51`. | empirical | A. This retracts an earlier claim. See R1. |

## Layer 4: the intent contract

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 4.1 | The client engine derives from the server before writing, and refuses to guess a rate when nothing is derivable. | `harness/intent.py:204-220` | source | A |
| 4.2 | A refused field appearing anywhere in a line raises rather than being dropped or silently accepted. | `harness/intent.py:179-187` | source | A |
| 4.3 | An override without a non-empty reason raises. | `harness/intent.py:200-201` | source | A |
| 4.4 | The derived value and the supplied value are both recorded, as a pair, per row. | `harness/intent.py:33-39`, `:242-245` | source | A |
| 4.5 | The reason is persisted on the document. | `harness/intent.py:250-254` writes `doc["remarks"]`. | source | B. `remarks` is a free-text `Small Text` on Sales Invoice, parent level, not per line. Multiple overrides are joined into one string. The enforced endpoint uses a Comment instead (claim 7.5). |
| 4.6 | The persisted reason cannot be edited after submission. | `sales_invoice.json` `remarks` carries no `allow_on_submit`, and `vendor/frappe/frappe/model/base_document.py:1353-1361` throws on any change to a non-`allow_on_submit` field of a submitted document. | source | B. Cancel-and-amend produces a new document and is not blocked by this. |
| 4.7 | The engine's own invariant check verifies the override delta is recorded in the correct one of ERPNext's two places (`discount_amount` below list, `margin_rate_or_amount` above). | `harness/intent.py:302-321`. ERPNext's behaviour confirmed in source at `vendor/erpnext/erpnext/controllers/taxes_and_totals.py:205-221`. | both | A |
| 4.8 | 10 of 10 contract cases behave as specified. | `reports/intent_proof.json`, cases enumerated in `harness/prove_intent.py:47-72` | empirical | B |
| 4.9 | 49 of 52 corpus scenarios pass (3 fail by design, documenting native ERPNext behaviour via the raw path). | `reports/corpus.json` | empirical | B |
| 4.10 | The corpus asserts on accounting principles rather than on ERPNext's behaviour. | `corpus/scenarios.yaml`, each scenario carries a `principle` field | derived | C. True as written, but of the 39, 22 (`order_to_cash`, `commitments`, `collection`, `procure_to_pay`) assert properties ERPNext guarantees by construction and which no intent-layer bug could break. The 12 that actually exercise the contract are `derivation_contract` (10) and `returns` (2), plus `grd-01` and `grd-04`. |
| 4.11 | The corpus caught a real defect in its own setup (`der-08-unpriced-item-refused` passed for the wrong reason because the fixture item had a buying-list price). | Narrated in the README. The fixed state is `harness/fixtures.py:88-98`, which adds `HL-UNPRICED-001` with no price in any list. | derived | B. The original failing state is not preserved in the repo, so a reader cannot re-observe it. |

## Layer 5: real data

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 5.1 | The off-reference rate depends entirely on the definition of the reference price, and ranges from 3.4% to 31.4% over the same 1,033,527 lines. | `reports/dataset_analysis.json` carries the pooled-median (31.4%) and per-customer-modal (3.4%) figures. | empirical | A |
| 5.2 | Restricting the per-customer-modal reference to (customer, SKU) pairs bought at least twice gives 5.97%; at least six times gives 8.95%. 42.87% of lines carrying a customer ID are the only purchase of that SKU by that customer. | Reproduced independently for this register from `data/sales_clean.csv` using the same modal definition and tolerance (`harness/prepare_data.py:93` `TOL = 0.005`, `:197-202` mode function): 801,418 lines with a customer ID; 457,813 lines in pairs with 2+ purchases, 27,340 off reference; 139,690 lines in pairs with 6+ purchases, 12,501 off reference; 343,605 singleton lines. | derived | A. Matches the README's 6.0 / 8.9 / 42.9 to rounding. Not currently written to any report file, so it is reproducible but not committed. |
| 5.3 | **The defensible off-reference figure is 6 to 9 percent, not a third.** | 5.1 + 5.2. The pooled median is partly an artifact for a bimodal seller. The unrestricted per-customer modal is flattered by singletons. The 2+ and 6+ cuts remove both distortions in opposite directions and agree within three points. | derived | B. Still one seller, one industry, 2009 to 2011. See [LIMITATIONS.md](LIMITATIONS.md) section 7. |
| 5.4 | Exclusions before analysis: 6,050 non-product codes and 2,093 deviations above 500%, out of 1,041,670. | `reports/dataset_analysis.json` `noise_lines_stripped`, `implausible_dropped`; logic at `harness/prepare_data.py:135-153` | empirical | A. 0.78% of lines, stated. |
| 5.5 | Post-hoc detection of an agent-supplied wrong price is feasible but expensive and imprecise. On a million lines a year, a detector at 6 to 9 percent queues 60,000 to 90,000 lines, almost all legitimate, and precision does not improve with volume because the deviation is real business behaviour. | 5.3 plus arithmetic. | inference | D. The queue-size arithmetic is trivial; the "precision does not improve" half is an argument, not a measurement. **No detector was built, run, or scored anywhere in this repo.** Nothing here reports a real precision or recall figure. |
| 5.6 | Post-hoc detection and write-time capture answer different questions: "is this price unusual?" versus "did anybody decide this?" | 5.5, plus the observation that a price can be unusual and correct or ordinary and unconsidered. | inference | D. This is the load-bearing product claim and it is an argument, not a result. It replaces the retracted impossibility claim (R5). |
| 5.7 | 1,000 real invoices, 4,380 line items, were written twice: 1,091 lines off-list both times, 1,091 overrides recorded on the intent path, 0 invariant failures, 378 seconds. | `reports/simulation.json` | empirical | B |
| 5.8 | The replay demonstrates that the intent path loses nothing in round-trip and that the invariant check holds at volume on real price distributions. | `reports/simulation.json` `intent_invariant_failures` absent from `stats`, i.e. zero | empirical | B |
| 5.9 | The replay does **not** demonstrate detection of anything. | `harness/simulate.py:128-134` and `:141-147`: the script computes which lines are off-list, then passes exactly those as declared overrides, then reports the count. `naive_offlist_lines` and `naive_offlist_unrecorded` are incremented on the same branch and are therefore the same number by construction. | derived | A. The 1,091 = 1,091 equality is a tautology. Present it as a round-trip fidelity result, never as a detection result. |
| 5.10 | The 1,091 recorded reasons are machine-generated. | `harness/simulate.py:145-147` writes `f"source invoice {r.Invoice} priced at {r.Price} vs list {lp}"`. | derived | A. The repo's own headline demonstration is an agent writing its own justifications. See [THREAT_MODEL.md](THREAT_MODEL.md), bypass 2. |
| 5.11 | After 2,223 submitted invoices, the trial balance nets to zero: 451,856.02 debits, 451,856.02 credits, 6,466 GL entries. | `reports/trial_balance.json` | empirical | A |
| 5.12 | 5.11 is not a finding. | Balance is enforced (1.7). Its only legitimate use is the negative one at 3.5. | inference | A |
| 5.13 | Real distributions surfaced a real bug in `intent.py` (the one-directional discount check that passed every above-list line). | Fix present at `harness/intent.py:302-321`. The above-list population under the pooled-median reference is 219,670 lines, 21.3% (`reports/dataset_analysis.json`). | derived | B. The pre-fix state is not preserved in the repo. |

## Layer 6: the product claim

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 6.1 | This is a write-time control, not a detector. | Positioning choice, following from 5.6. | inference | D |
| 6.2 | ERPNext offers no per-field record of whether a value was derived or asserted. | Absence argument. No such field exists on the item child doctypes; `Version` records what a value became, not what it would have been. | inference | D. An absence claim over a large codebase. It is a reasonable reading, not an exhaustive proof. |
| 6.3 | ERPNext's existing server-side price controls (`Item.max_discount`, `maintain_same_sales_rate`, `validate_selling_price`) do not close this gap. | See the table in [POSITIONING.md](POSITIONING.md), each row cited to source. Notably `taxes_and_totals.py:221` zeroes `discount_percentage` when the caller supplies `rate` below `price_list_rate`, before `selling_controller.py:266-272` reads it. | source | B. The `max_discount` interaction is read from source and has **not** been reproduced empirically. It is the single most valuable outstanding experiment in this repo: set `Item.max_discount = 10` and post `rate: 1` against a list of 250. |
| 6.4 | The client-side control (`harness/intent.py`) is bypassed by writing directly to `/api/resource`. | It runs in the caller's process. Nothing about it is enforced server-side. | derived | A. Superseded for the `bill` intent by layer 7, and still true everywhere else. |

## Layer 7: the enforcement boundary

`harness/enforce.py`, `harness/server_scripts/bill_intent.py`,
`harness/prove_boundary.py`, `reports/boundary.json`.

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 7.1 | A boundary exists that makes the contract non-bypassable for a constrained identity. 10 of 10 checks pass. | `reports/boundary.json` | empirical | B |
| 7.2 | An unconstrained identity (`Administrator`) can still POST a Sales Invoice with `rate: 1.0` straight to `/api/resource` and it is accepted. | `reports/boundary.json` check `admin direct write allowed`, driven by `harness/prove_boundary.py:29-36` | empirical | A. This is the control case. Without it the 403 in 7.3 would prove nothing about the boundary and only that something was broken. |
| 7.3 | The constrained identity gets HTTP 403 on the same direct write. | `reports/boundary.json` check `agent direct write blocked`; the request is made with a plain `requests.Session` after logging in as the agent user (`harness/prove_boundary.py:38-46`), so nothing about the check depends on the client library behaving. | empirical | B |
| 7.4 | The permission denial is enforced by Frappe itself, not by anything in this repo. | `Document.insert()` calls `check_permission("create")` at `vendor/frappe/frappe/model/document.py:477`, and every REST and `frappe.client` write route reaches `insert()` or `save()`. | source | A |
| 7.5 | The server-side endpoint enforces the same four rules as the client engine: refuse a refused field, refuse a directly supplied `rate`, require a reason on an override, refuse an unpriceable item. | `harness/server_scripts/bill_intent.py:13-42`; the four corresponding checks in `reports/boundary.json` | both | B |
| 7.6 | The boundary covers one intent. | `bill_intent` is the only Server Script installed (`harness/enforce.py:67-76`). The other ten intents in `intents/catalog.yaml` have no server-side counterpart. | derived | A |
| 7.7 | The boundary binds only identities somebody deliberately constrained. | `harness/enforce.py:21-33` creates one role with read-only permission on 24 master doctypes and no write permission on any transaction doctype. Every pre-existing ERPNext role is untouched. | derived | A. Any user holding a normal role, including every human, writes directly as before. This is a deployment property, not a defect, but it must never be described as "the API is now closed." |
| 7.8 | The endpoint runs with `ignore_permissions=True`. | `harness/server_scripts/bill_intent.py:50`, `:74` | source | A. The endpoint is a privilege boundary, so its correctness is load-bearing in a way the client library's never was. A bug in it is an escalation, not an inconvenience. Nothing in this repo fuzzes it or reviews it as security-sensitive code. |

## Layer 8: intent is not evidence, the stored document is

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 8.1 | ERPNext can rewrite a caller's value during `validate`, after the endpoint has set it and before the document is persisted, with no error and no signal. | `vendor/erpnext/erpnext/controllers/taxes_and_totals.py:170-221` applies Pricing Rules during the validate cycle. | source | A |
| 8.2 | This has been observed replacing a caller's explicit reasoned override of 1.0 with 225.0, a rule-derived 10% off the 250.00 list price. | Reported from a live run of the enforced endpoint. Corroborated by `reports/census.json`, where Sales Invoice and Delivery Note probes supplying `rate: 7.0` have a stored value of `225.0`, together with `discount_percentage` `10.0` and `discount_amount` `25.0` on the same documents. | empirical | B. The corroboration is strong and independent, but **the specific 1.0-to-225.0 event is not preserved in any committed report**. `reports/boundary.json` records only pass/fail per check and contains no drift field. A reader cannot re-observe the exact event described. |
| 8.3 | An audit record built from the writer's intent would therefore assert a decision that never took effect. | 8.1 + 8.2 | inference | A |
| 8.4 | The endpoint now reads the persisted value back off the saved document and reports three values per line: `derived`, `intended`, and `stored`. | `harness/server_scripts/bill_intent.py:52-62` reads `doc.items[i["row"]].rate` after `doc.insert()`. | source | A |
| 8.5 | When `stored` and `intended` disagree the record carries a `drift` note and the response carries a `drift_detected` count. | `harness/server_scripts/bill_intent.py:59-61`, `:70-71`, `:76-77` | source | A |
| 8.6 | The general rule: an audit record is a statement about a stored document, so it must be read from the stored document. | 8.3 | inference | D. Stated as a design principle. It follows from 8.3 for this system; its generality to other ERPs is asserted, not tested. |
| 8.7 | The same mutation-during-validate mechanism silently corrupts the census's own results. | Claim 2.16. Six probes recorded `protected` were in fact overwritten by the Pricing Rule, not defended by a derivation. | derived | A. The lesson generalises within the repo: any measurement that compares "what I sent" to "what was stored" and infers a mechanism from the difference is unsound on an instance carrying rules. |

## Claims we retracted

### R1. "The caller receives no indication that a Price List entry existed."

**Retracted. The claim was false.**

The original README stated that an API caller supplying `rate: 1.0` "receives no
indication that a Price List entry existed at all" and that "nothing in the
response says you skipped derivation."

The write response returns the saved document, which contains
`price_list_rate: 250.0` and `discount_amount: 249.0`. This is visible in
`reports/latest.json`, which was produced by reading the insert response
(`harness/client.py:44-51`). The evidence contradicting the claim was in the
repo's own committed output for the entire time the claim stood.

A caller that reads its own response can see exactly what the list price was and
exactly how far it overrode it. Any argument built on caller blindness is void.

What survives is the post-hoc claim only, and it is narrower: months later,
`price_list_rate=250, rate=1, discount_amount=249` is indistinguishable from an
authorised discount. The system records the arithmetic consequence of a decision
but never whether a decision occurred.

### R2. "The API accepts documents the UI cannot produce."

**Retracted as stated. Survives only with conditions attached.**

The claim rested on `price_list_rate` being `read_only` on 5 of 9 child
doctypes. It is (claim 2.11). But `Selling Settings.editable_price_list_rate`
exists specifically to make that field editable in the Desk UI
(`sales_common.js:326-339`), and it is a checkbox. The correct statement is:

> Under ERPNext's default Selling Settings, a Desk session cannot set
> `price_list_rate` on Quotation, Sales Order, Sales Invoice, Delivery Note or
> Material Request. The API accepts it regardless of that setting, because
> field-level `read_only` is never checked server-side (claim 1.5).

"A document no Desk session could have created" is only true for a deployment
that has left the default in place, and it is one administrator action away from
being false. It should not be used as a headline.

### R3. "144 of 196 probes were protected. This is not a system with no defences."

**Retracted as a framing, and the numbers have since moved.** The current report
records 156 of 203 (claim 2.1). Presenting either ratio as a defence measure was
misleading on two counts: most "protected" fields are totals the server
recomputes anyway (claim 2.5), and on an instance carrying Pricing Rules a
`protected` verdict does not mean the derivation defended itself (claim 2.16).

The census's honest shape is a small field set on one derivation path, of which
`price_list_rate` on 8 doctypes is the one result with a demonstrated non-zero
derivation being overwritten (claims 2.6 to 2.8).

### R4. "The trial balance demonstration is a finding."

**Retracted.** Presenting a balanced trial balance as a result implies the
opposite outcome was possible. It was not: `general_ledger.py:473-503` refuses
to post an unbalanced voucher. The demonstration is only usable in the negative
direction (claim 3.5), and any presentation that lets a reader think otherwise
should be rewritten.

### R5. "Post-hoc detection of this class of error is impossible."

**Retracted. The claim was an overclaim and it does not survive better
measurement of the underlying data.**

The impossibility argument was built on the 31.4% off-list figure: if a third of
legitimate lines price off list, no rule over stored documents can separate a
real wholesale price from an agent that never looked one up.

That figure is definition-dependent (claim 5.1). Against a per-customer modal
reference restricted to pairs with a real purchase history, the off-reference
rate is 6.0% to 8.9% (claim 5.2). At that rate a detector is feasible. It is
expensive and imprecise, but "expensive and imprecise" is not "impossible," and
the two should never have been conflated.

The replacement argument is narrower and does not depend on any rate at all
(claim 5.6): detection and write-time capture answer different questions. A
detector answers "is this price unusual," and a price can be unusual and correct
or ordinary and unconsidered. Only a contemporaneous record answers "did
anybody decide this."

Note that the replacement is an argument, not a measurement. No detector was
ever built or scored in this repo, so no precision or recall figure exists here
to compare against. Anyone quoting the 6 to 9 percent as a detector's false
positive rate should say that it is a deviation rate over a single seller's
historical data and that no detector was run on it.

### R6. "The enforcement boundary passes 8 of 8 checks."

**Published in:** every commit from `d3d5edf` (which introduced the boundary) up to the commit that adds this entry, in README, POSITIONING and
`reports/boundary.json`.
**Retracted. Every boundary figure published in that range was wrong**, and not
by a small margin. Two independent defects:

**The endpoint never submitted anything.** `bill_intent.py` called
`doc.submit()`, but `insert()` takes `ignore_permissions` as a parameter while
`submit()` reads it off `doc.flags`. The call was a silent no-op: it neither
submitted nor raised. Every Sales Invoice written through the control sat at
`docstatus 0`, nothing reached the general ledger through the control path, and
drift was checked exactly once at insert. A draft window was left open in which
anyone with a normal role could edit the rate and submit, while the audit record
went on describing the draft. **The control we were describing had never posted
an invoice.**

**The boundary test measured two sites at once.** `prove_boundary.py` routed its
Administrator half through `FrappeClient`, which honours `ERPNEXT_SITE`, but
built a bare `requests.Session()` for the agent half with no `Host` header. Under
`ERPNEXT_SITE=clean.local` the privileged half ran against the clean site and the
constrained half against the default site. The two halves of a test whose entire
purpose is to compare privileged and constrained behaviour were not comparing the
same system.

**Fixed in the commit that adds this entry.** `doc.flags.ignore_permissions = True` before
submit; the raw session carries the Host header; and `prove_boundary.py` now
re-reads the document and fails the check when `docstatus != 1`, so it cannot
regress silently. Verified: a submitted invoice at `docstatus 1` with GL entries
posted comes out of the endpoint.

**The general lesson, applied repo-wide:** any check that asserts success must
re-read the state it claims to have changed. `IntentEngine` now re-reads after
every submit rather than trusting the response, and records
`submit actually posted (re-read)` as a checked invariant. `harness/corpus.py`
already re-read and asserted `docstatus` and GL entries; it was audited and is
clean. This class of bug is invisible to any harness that trusts its own writes.


## Claims we are not making

Listed so their absence is not read as an oversight.

- That ERPNext has a security vulnerability. It does not, on this evidence.
- That any specific public MCP server exhibits this behaviour. Untested. The
  architectural argument that they inherit it is an inference and nothing here
  measures it.
- That the 484 `set_query` client-side link filters lack server-side backstops.
  Unmeasured. Do not assume they are missing.
- That an agent can produce an unbalanced ledger, a negative outstanding
  balance, or a posting to a frozen account. ERPNext blocks all three
  (claims 1.7, 1.9).
- That post-hoc detection is impossible (retracted, R5), or that any detector's
  precision has been measured (claim 5.5).
- That the boundary closes the API. It binds one intent, for identities somebody
  deliberately constrained (claims 7.6, 7.7).
- That a recorded reason is evidence a human decided anything (claim 5.10 and
  [THREAT_MODEL.md](THREAT_MODEL.md), bypass 2).
