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
(`docker/pwd.yml`), on 2026-09-07.

The source tree checked out at `vendor/erpnext` and `vendor/frappe` is
**17.0.0-dev** (`vendor/erpnext/erpnext/__init__.py:6`,
`vendor/frappe/frappe/__init__.py:144`, commit `72fa7d0`). `vendor/` is
gitignored, so a reader cloning this repo gets neither tree.

Consequence: **every `file:line` citation below points at 17.0.0-dev**, not at
the version the numbers were measured on. The README cites v16.34.1 line numbers
(`accounts_controller.py:1127`, `:97`) that do not resolve in the tree present
here. The conditions are the same in both versions and the v17 locations are
given below, but a hostile reader is right to say that no single artifact in
this repo lets them check a v16 line number. Fixing that means either vendoring
a pinned v16 tree or restating every citation against v17.

## Layer 1: the mechanism in ERPNext

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 1.1 | `AccountsController.set_missing_item_details` writes a derived value onto an item row only when the field is `None`, or when the field is in `force_item_fields`, or for `serial_no`/`batch_no` under `use_serial_batch_fields`. | `vendor/erpnext/erpnext/controllers/accounts_controller.py:781-792` | source | A |
| 1.2 | `force_item_fields` has nine members: `item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`, `weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate`. `rate` and `price_list_rate` are not among them. | `vendor/erpnext/erpnext/controllers/accounts_controller.py:65` | source | A |
| 1.3 | Therefore a caller that supplies `rate` keeps it, and the price list is not consulted for that row. | 1.1 + 1.2, confirmed by `reports/latest.json` mode `assert`: `rate` 250.0 (UI path) vs 1.0 (API path) | both | A |
| 1.4 | This is intended behaviour, not a defect in ERPNext. | The `is None` gate is what lets a manually entered discount survive a re-save. No ERPNext issue or fix is claimed. | inference | D |
| 1.5 | Field-level `read_only` is never consulted in Frappe's server-side save or validate path. It is a rendering hint. | Exhaustive grep for `.read_only` / `"read_only"` in `vendor/frappe/frappe/model/*.py` returns only doctype-level `meta.read_only` (`document.py:1957`) and the request-level `frappe.flags.read_only` (`document.py:2217,2250`). No DocField-level check exists. | source | A |
| 1.6 | 1.5 is a general Frappe property, true of every read-only field on every doctype, and is not an ERPNext discovery. | Follows from 1.5: the check is absent framework-wide, not absent for pricing. | inference | D |
| 1.7 | ERPNext refuses to post an unbalanced voucher. | `vendor/erpnext/erpnext/accounts/general_ledger.py:397-427` calls `raise_debit_credit_not_equal_error` (`:460`) whenever `abs(debit_credit_diff) > allowance`, with a single carve-out for Exchange Gain Or Loss journal entries. | source | A |
| 1.8 | Therefore a balanced trial balance is guaranteed by construction and is evidence of nothing about the correctness of any amount. | 1.7 | inference | A |
| 1.9 | ERPNext's GL layer independently validates account structure: group accounts, inactive accounts, frozen accounts, company and cost-center mismatch. Claims that an agent can "corrupt the books" through these paths are wrong. | `gl_entry.py` validation block, cited in README as `:232-275` (v16 line numbers; not re-verified against the v17 tree for this register) | source | B |

## Layer 2: the census

`reports/census.json`, produced by `harness/run_census.py`, ERPNext v16.34.1.

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 2.1 | 196 probes across 9 transaction doctypes: 144 protected, 42 silently accepted, 10 rejected. | `reports/census.json` `by_verdict` | empirical | B |
| 2.2 | Six distinct fields were silently accepted: `rate`, `price_list_rate`, `discount_amount`, `discount_percentage`, `weight_per_unit`, `min_order_qty`. | `reports/census.json` `silently_accepted_fields` | empirical | B |
| 2.3 | The 196 is not 196 independent findings. It is a small field set multiplied by the doctypes those fields appear on. | Derived from `reports/census.json` `probes`: the 42 accepts are 6 fields, of which 4 appear on 8 doctypes each, 1 on 9, 1 on 1. | derived | A |
| 2.4 | The 10 rejections are one probe repeated, not 10 results. | All 10 are `qty` (mutated to 0.016 by `harness/census.py:57`) failing validation with HTTP 417, on 9 doctypes, plus one `delivered_by_supplier` on Sales Order. | derived | A |
| 2.5 | The 144 "protected" figure overstates the strength of ERPNext's defences. | Most protected fields are arithmetic the server recomputes as a matter of course (`amount`, `net_amount`, `net_rate`, `base_rate`, `base_amount`, `stock_qty`), not derivation decisions being defended. | derived | A |
| 2.6 | **34 of the 42 "silently accepted" verdicts had a derived value of 0.0.** | Computed over `reports/census.json` `probes`: only the 8 `price_list_rate` accepts on non-Material-Request doctypes had a non-zero derivation (250.0). All `rate`, `discount_amount`, `discount_percentage`, `weight_per_unit`, `min_order_qty` accepts, and the Material Request `price_list_rate` accept, had `derived_value: 0.0`. | derived | A |
| 2.7 | Consequence of 2.6: for 34 of the 42, the census did **not** demonstrate "the server had a derivation available and used the caller's number instead." It demonstrated "the server had nothing to say and the caller's number stuck." | 2.6 | derived | A |
| 2.8 | The strongly supported core of the census is: **`price_list_rate` is silently accepted on 8 doctypes where the server derives a real value (250.0) for it.** | `reports/census.json`, probes where `fieldname == "price_list_rate"` and `derived_value == 250.0` | empirical | B |
| 2.9 | `rate` is silently accepted on 8 of 9 doctypes and protected on Material Request. | `reports/census.json` | empirical | C. The probe's derived `rate` was 0.0 in every case (see 2.6), because `get_item_details` returns `rate = ctx.rate or price_list_rate` and the probe sends no `ctx.rate`. Claim 1.3 establishes the same behaviour cleanly through `reports/latest.json`, which is the citation to use. |
| 2.10 | `weight_per_unit` is in `force_item_fields` and is nonetheless stored as the caller supplied it. | `reports/census.json`, 8 doctypes, derived 0.0, supplied 7.0, stored 7.0 | empirical | C. The derived value was 0.0 because the fixture item has no weight (`harness/fixtures.py:50-65` sets none). The force-overwrite writing back the caller's own value is a real and interesting mechanism, but on a zero-weight fixture it is not distinguishable from the force path simply not firing. Re-run with a weighted item before relying on this. |
| 2.11 | `price_list_rate` is `read_only` on 5 of 9 item child doctypes: Quotation Item, Sales Order Item, Sales Invoice Item, Delivery Note Item, Material Request Item. | Cross-reference of `read_only` in the nine child DocType JSONs under `vendor/erpnext/erpnext/`. Recomputed for this register; matches the README table exactly. | source | A |
| 2.12 | `rate` is `read_only` on 0 of 9. `discount_amount` and `discount_percentage` on 0 of 8 (absent on Material Request Item). `weight_per_unit` on 4 of 8. `min_order_qty` on 1 of 1. | Same cross-reference | source | A |
| 2.13 | "A human cannot type into `price_list_rate`." | **Settings-dependent.** `Selling Settings.editable_price_list_rate` (default `0`, `selling_settings.json`) is consumed by `vendor/erpnext/erpnext/public/js/utils/sales_common.js:326-339` (`toggle_editable_price_list_rate`), which clears the `read_only` flag on the `price_list_rate` docfield when the setting is on. | source | C. True under default settings on selling doctypes. One checkbox makes it false. The claim must always carry "under default settings." |
| 2.14 | Therefore an API write of `price_list_rate` "produces a document no Desk session could have created." | 2.11 + 2.13 | inference | C. Only under default settings, and only for the 5 doctypes in 2.11. State it as such or drop it. |
| 2.15 | The census covers one derivation path. | Every probe calls `erpnext.stock.get_item_details.get_item_details` (`harness/census.py:70-74`). Header-level, tax-table, payment-schedule, and stock-valuation derivations are not probed. | derived | A |

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
| 3.7 | The write response contains `price_list_rate` and `discount_amount`, so a caller that reads its own response can see the list price it overrode. | `reports/latest.json` records `discount_amount: 249.0` on the API document, read back from the insert response through `harness/client.py:44-51`. | empirical | A. This retracts an earlier claim. See retractions. |

## Layer 4: the intent contract

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 4.1 | The engine derives from the server before writing, and refuses to guess a rate when nothing is derivable. | `harness/intent.py:204-220` | source | A |
| 4.2 | A refused field appearing anywhere in a line raises rather than being dropped or silently accepted. | `harness/intent.py:179-187` | source | A |
| 4.3 | An override without a non-empty reason raises. | `harness/intent.py:200-201` | source | A |
| 4.4 | The derived value and the supplied value are both recorded, as a pair, per row. | `harness/intent.py:33-39`, `:242-245` | source | A |
| 4.5 | The reason is persisted on the document. | `harness/intent.py:250-254` writes `doc["remarks"]`. | source | B. `remarks` is a free-text `Small Text` on Sales Invoice, parent level, not per line. Multiple overrides are joined into one string. |
| 4.6 | The persisted reason cannot be edited after submission. | `sales_invoice.json` `remarks` carries no `allow_on_submit`, and `vendor/frappe/frappe/model/base_document.py:1353-1361` throws on any change to a non-`allow_on_submit` field of a submitted document. | source | B. Cancel-and-amend produces a new document and is not blocked by this. |
| 4.7 | The engine's own invariant check verifies the override delta is recorded in the correct one of ERPNext's two places (`discount_amount` below list, `margin_rate_or_amount` above). | `harness/intent.py:302-321`. ERPNext's behaviour confirmed in source at `vendor/erpnext/erpnext/controllers/taxes_and_totals.py:205-221`. | both | A |
| 4.8 | 7 of 7 contract cases behave as specified. | `reports/intent_proof.json`, cases enumerated in `harness/prove_intent.py:47-72` | empirical | B |
| 4.9 | 39 of 39 corpus scenarios pass. | `reports/corpus.json` | empirical | B |
| 4.10 | The corpus asserts on accounting principles rather than on ERPNext's behaviour. | `corpus/scenarios.yaml`, each scenario carries a `principle` field | derived | C. True as written, but of the 39, 22 (`order_to_cash`, `commitments`, `collection`, `procure_to_pay`) assert properties ERPNext guarantees by construction and which no intent-layer bug could break. The 12 that actually exercise the contract are `derivation_contract` (10) and `returns` (2), plus `grd-01` and `grd-04`. |
| 4.11 | The corpus caught a real defect in its own setup (`der-08-unpriced-item-refused` passed for the wrong reason because the fixture item had a buying-list price). | Narrated in the README. The fixed state is `harness/fixtures.py:88-98`, which adds `HL-UNPRICED-001` with no price in any list. | derived | B. The original failing state is not preserved in the repo, so a reader cannot re-observe it. |

## Layer 5: real data

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 5.1 | 31.4% of 1,033,527 analysed line items transact away from the per-SKU median price, and those lines carry 50.8% of gross revenue. 88.4% of SKUs sold at more than one price. | `reports/dataset_analysis.json` | empirical | C. **The list price is defined as the per-SKU median** (`method` field in the report; the same definition at `harness/simulate.py:36`). A seller with two legitimate price points shows close to half its lines off the median by construction. The number measures dispersion around a synthetic reference, not deviation from a real price list. |
| 5.2 | 8,143 lines were excluded of 1,041,670: 6,050 non-product codes, 2,093 deviations above 500%. | `reports/dataset_analysis.json` `noise_lines_stripped`, `implausible_dropped` | empirical | A. Exclusions are stated and small (0.78%). |
| 5.3 | Post-hoc detection of an agent-supplied wrong price is not possible by comparing price to list. | 5.1, plus the observation that ERPNext models per-customer-group pricing properly (`Customer.default_price_list`, `Customer Group.default_price_list`, Pricing Rule scoped by `customer_group`), so multi-price SKUs are the expected case, not an anomaly. | inference | C. This is the load-bearing claim of the whole product and it is an inference, not a measurement. Nothing here tests a detector and reports its false-positive rate. What is measured is that the two populations overlap heavily under a reasonable reconstruction of the reference price. |
| 5.4 | 1,000 real invoices, 4,380 line items, were written twice: 1,091 lines off-list both times, 1,091 overrides recorded on the intent path, 0 invariant failures, 378 seconds. | `reports/simulation.json` | empirical | B |
| 5.5 | The replay demonstrates that the intent path loses nothing in round-trip and that the invariant check holds at volume on real price distributions. | `reports/simulation.json` `intent_invariant_failures` absent from `stats`, i.e. zero | empirical | B |
| 5.6 | The replay does **not** demonstrate detection of anything. | `harness/simulate.py:128-134` and `:141-147`: the script computes which lines are off-list, then passes exactly those as declared overrides, then reports the count. `naive_offlist_lines` and `naive_offlist_unrecorded` are incremented on the same branch and are therefore the same number by construction. | derived | A. The 1,091 = 1,091 equality is a tautology. Present it as a round-trip fidelity result, never as a detection result. |
| 5.7 | The 1,091 recorded reasons are machine-generated. | `harness/simulate.py:145-147` writes `f"source invoice {r.Invoice} priced at {r.Price} vs list {lp}"`. | derived | A. The repo's own headline demonstration is an agent writing its own justifications. See [THREAT_MODEL.md](THREAT_MODEL.md), bypass 2. |
| 5.8 | After 2,223 submitted invoices, the trial balance nets to zero: 451,856.02 debits, 451,856.02 credits, 6,466 GL entries. | `reports/trial_balance.json` | empirical | A |
| 5.9 | 5.8 is not a finding. | Balance is enforced (1.7). Its only legitimate use is the negative one at 3.5. | inference | A |
| 5.10 | Real distributions surfaced a real bug in `intent.py` (the one-directional discount check that passed every above-list line). | Fix present at `harness/intent.py:302-321`. The above-list population is 219,670 lines, 21.3% (`reports/dataset_analysis.json`). | derived | B. The pre-fix state is not preserved in the repo. |

## Layer 6: the product claim

| # | Claim | Evidence | Method | Strength |
|---|---|---|---|---|
| 6.1 | This is a write-time control, not a detector. | Positioning choice, following from 5.3. | inference | D |
| 6.2 | ERPNext offers no per-field record of whether a value was derived or asserted. | Absence argument. No such field exists on the item child doctypes; `Version` records what a value became, not what it would have been. | inference | D. An absence claim over a large codebase. It is a reasonable reading, not an exhaustive proof. |
| 6.3 | ERPNext's existing server-side price controls (`Item.max_discount`, `maintain_same_sales_rate`, `validate_selling_price`) do not close this gap. | See the table in [POSITIONING.md](POSITIONING.md), each row cited to source. Notably `taxes_and_totals.py:221` zeroes `discount_percentage` when the caller supplies `rate` below `price_list_rate`, before `selling_controller.py:266-272` reads it. | source | B. The `max_discount` interaction is read from source and has **not** been reproduced empirically. It is the single most valuable outstanding experiment in this repo: set `Item.max_discount = 10` and post `rate: 1` against a list of 250. |
| 6.4 | The control as shipped is client-side and is bypassed by writing directly to `/api/resource`. | `harness/intent.py` runs in the caller's process. Nothing in this repo installs server-side enforcement. | derived | A. See [THREAT_MODEL.md](THREAT_MODEL.md). |

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
but never whether a decision occurred. That is claim 6.1's foundation and it
does not depend on the caller being blind.

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

**Retracted as a framing, not as a number.** The number is correct
(`reports/census.json`). Presenting it as a defence ratio was misleading: most
"protected" fields are totals the server recomputes anyway (claim 2.5), and the
196 denominator is one field set multiplied across doctypes (claim 2.3). The
census's honest shape is six fields on one derivation path, of which one
(`price_list_rate`) has a strongly demonstrated non-zero derivation being
overwritten (claims 2.6 to 2.8).

### R4. Implicit: "the trial balance demonstration is a finding."

**Retracted.** Presenting a balanced trial balance as a result implies the
opposite outcome was possible. It was not: `general_ledger.py:397-427` refuses
to post an unbalanced voucher. The demonstration is only usable in the negative
direction (claim 3.5), and any presentation that lets a reader think otherwise
should be rewritten.

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
- That this control detects anything after the fact. It does not, by design
  (claim 5.3).
- That the intent layer is a security boundary in its current form
  (claim 6.4).
