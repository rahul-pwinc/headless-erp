# Limitations

What has not been tested, what is tested weakly, and what cannot be tested.
Nothing here is a promise of future work. It is a statement of the current
boundary so that a reader does not have to discover it themselves.

Claim numbers refer to [CLAIMS.md](CLAIMS.md).

## Concurrency: safe, not available

Measured, not assumed. `make concurrency` fires 20 simultaneous overrides through
the enforced endpoint and then verifies the hash chain
(`reports/concurrency.json`).

**Integrity holds.** No duplicate sequence numbers, no gaps, no forked
`prev_hash`, and `verify_chain` reports ok. The fork this test was written to
find does not happen.

**Availability does not.** Typically 3 or 4 of 20 writes land; the rest are
rejected with `QueryDeadlockError`. MariaDB's row locking is what prevents the
fork, and it prevents it by refusing most of the concurrent writes. The endpoint
does not retry, and it cannot usefully retry inside its own request because a
deadlock has already aborted the transaction. **A caller issuing parallel
overrides will silently lose them unless it retries.**

For an audit chain, integrity is the property that matters and it is the one that
holds. Availability is a real operational limit and it is stated here rather than
hidden behind a green check.

One incidental finding from building the test: Frappe's own login races. Four of
five concurrent `/api/method/login` calls for the same user return 500. That is a
fact about frappe, not about this control, but any client planning to parallelise
should authenticate once and share the session rather than logging in per worker.


## 1. One vendor

Everything in this repo is ERPNext. One ERP, one framework, one API shape.

The general thesis (a headless ERP must enforce in the API everything it
enforces in a screen) is stated as if it were vendor-neutral. It has been tested
on exactly one vendor, chosen because its source is readable. Whether SAP,
NetSuite, Dynamics, Odoo, or Business Central have the same derive-then-accept
gap is **unknown and unmeasured**. Do not present the finding as an industry
pattern. Present it as one system's behaviour with an argument, not evidence,
that the shape generalises.

Odoo is the obvious second target because its source is also readable and its
`onchange` mechanism is architecturally analogous to `get_item_details`. Nothing
here says what would be found.

## 2. One derivation path

Every census probe calls `erpnext.stock.get_item_details.get_item_details`
(`harness/census.py:70-74`). That single method is the entirety of the measured
surface. The catalog's seven fields all come from it.

Not measured:

- **Header-level derivation.** `conversion_rate`, `plc_conversion_rate`,
  tax category resolution, payment terms template.
- **The `taxes` child table.** A caller supplying its own tax rows is not
  examined at all, by the census, the client engine, or the enforced endpoint.
- **Payment schedule rows.**
- **Stock valuation.** `incoming_rate`, `valuation_rate` on documents that
  actually move stock.
- **Non-numeric derived fields.** `harness/census.py:58` returns `None` for
  anything that is not a number, so every link field the engine treats as derived
  (`income_account`, `expense_account`, `cost_center`, `item_tax_template`,
  `warehouse`) was **never probed**. By claim 1.1 they should behave the same
  way, but that is an inference. `income_account` is the one that matters: it
  decides which GL account revenue lands in.

`reports/derivation_map.md` exists and extends beyond `get_item_details`. It has
not been reconciled against the catalog, so the catalog still rests on the
single-path census.

## 3. The census verdict is unsound on an instance carrying Pricing Rules

The probe asks one question: did the value I sent survive to storage. It reads
"no" as "the server asserted its own derivation."

That conflates every reason a value might not survive. On the current instance a
10% Pricing Rule fires during `validate` and rewrites the caller's value, so six
probes (`rate`, `discount_amount`, `discount_percentage` on Sales Invoice and
Delivery Note) are recorded `protected` with stored values of `225.0`, `25.0`
and `10.0` against a supplied `7.0` and a derived `0.0` (claim 2.16). Those
fields were overwritten, but not by the mechanism under test.

Two consequences:

- **The census numbers moved between report generations** with no change to
  ERPNext: 196 / 144 / 42 / 10 across six accepted fields became 203 / 156 / 37 /
  10 across seven (claim 2.1). Anyone citing a census figure must name which
  generation it came from.
- **A `protected` verdict is not evidence of a derivation defence** on any
  instance that has rules configured, which is every real deployment.

Fixing this means the probe has to distinguish "stored equals the derived value"
from "stored equals something else entirely," which it currently does not.

## 4. The UI column is a replay, not a browser

`harness/oracle.py:159-183` reconstructs what the Desk UI would have produced by
calling the same whitelisted server method the browser calls, with a hand-built
44-key `ctx` copied from `transaction.js` (`harness/oracle.py:105-156`).

**No browser is driven anywhere in this repo.** Playwright is installed in the
virtualenv and is not imported by any harness file.

The replay is a careful reconstruction and its `ctx` is field-for-field, but its
equivalence to a real Desk session is **asserted, not demonstrated**. Specific
ways it could be wrong:

- `transaction.js` may call `get_item_details` more than once per row (on item
  selection, on qty change, on UOM change), and the merge order of successive
  responses could differ from the single call the replay makes.
- Other client scripts may write to the row after the `get_item_details`
  callback returns.
- Custom client scripts on a real deployment certainly do.

Until a browser run is diffed against the replay, the "UI-derivation path"
column should be read as "what the server returns when asked the way the browser
asks," which is weaker than "what a human would have seen."

## 5. No stock-item coverage, so two intents are declared and untested

`intents/catalog.yaml` declares 11 intents with 27 invariants. Two of them,
`fulfil` (Delivery Note) and `receive` (Purchase Receipt), carry the stock
invariants:

```
stock ledger qty x valuation == Stock In Hand GL balance
COGS posted uses valuation rate, never selling rate
GRNI balance equals goods received and not yet invoiced
```

None of these has ever executed. Both fixture items are created with
`is_stock_item: 0` (`harness/fixtures.py:61`, `:96`), as are the items built
from the real dataset (`harness/simulate.py:52`). The corpus contains no
scenario using `fulfil` or `receive`: the 52 scenarios use `bill` (23),
`collect` (8), `expense` (7), `quote` (3), `pay` (3), `return` (2), `sell` (1),
`procure` (1), `source` (1), and one deliberately unknown intent.

So six of the 27 declared invariants belong to intents that no test touches, on
document types that need stock to exist at all. They are prose.

**More broadly, the catalog's invariants are prose.** The client engine
mechanically checks three forms (`harness/intent.py:268-321`): debits equal
credits, `grand_total == sum(net_amount) + taxes`, and the override delta being
recorded in the correct field. The other 24 catalog invariants (AR subledger
ties to the control account, GRNI nets to zero once every receipt is billed,
cumulative returned qty never exceeds original, unallocated payment stays
visible as an advance, and the rest) are not linked to any executable check. The
corpus separately asserts a set of accounting properties, but there is no
mapping between a corpus scenario and the catalog invariant it demonstrates. A
reader cannot answer "which of these 27 are actually enforced" without reading
all three files.

## 6. The enforcement boundary is narrow

`reports/boundary.json` records 8 of 8 checks passing. What that does and does
not cover:

- **One intent.** `bill_intent` is the only Server Script installed
  (`harness/enforce.py:67-76`). The other ten intents have no server-side
  counterpart, so the constrained identity cannot perform them at all through a
  compliant route.
- **Four rules of twelve.** The endpoint reimplements refuse-a-refused-field,
  refuse-a-supplied-rate, require-a-reason, and refuse-an-unpriceable-item. It
  does not implement the payment allocation rules, the intent-level `return`
  refuse, or the "not overridable" check: an override naming a field other than
  `rate` is **silently ignored** rather than refused
  (`harness/server_scripts/bill_intent.py:33-38`).
- **No scope validation.** The endpoint accepts whatever `customer` and
  `company` the caller names (`bill_intent.py:44-49`) and runs with
  `ignore_permissions=True` (`:50`). Nothing checks that this identity should be
  allowed to bill that customer for that company.
- **One constrained role, provisioned by the harness.** Every pre-existing
  ERPNext role is untouched and writes directly as before (claim 7.7).
- **The endpoint is security-sensitive code that nobody has reviewed as such.**
  It is a privilege boundary running with elevated permission. Nothing in this
  repo fuzzes it, tests it against malformed input, or checks its behaviour on
  concurrent calls.

## 7. Drift is detected, not prevented, and only for one field

`harness/server_scripts/bill_intent.py:52-62` reconciles intent against the
saved document and flags disagreement. It does so after `doc.insert()` has
already run, so a drifted document exists and is reported rather than refused.
Only `rate` is reconciled: if a rule changes `discount_amount`,
`item_tax_template`, or `income_account`, nothing notices.

The client engine has no equivalent reconciliation at all. It records
`supplied_value` from the request rather than from the response
(`harness/intent.py:242-245`), so on any instance with Pricing Rules every
override it logs may describe a value that was never stored. All 1,091 overrides
in `reports/simulation.json` were recorded that way.

The specific drift event this design was built from (1.0 replaced by 225.0) is
**not preserved in any committed report** (claim 8.2). It is corroborated
independently by the census probes, but a reader cannot re-observe the exact
event.

## 8. One company, one currency, one price list, single-line-dominant

- One company, `Headless Test Co`, created by the setup wizard
  (`harness/fixtures.py:24-47`).
- One currency, INR, with `conversion_rate: 1` and `plc_conversion_rate: 1`
  hardcoded in every header the harness builds. **Multi-currency is entirely
  untested.** The derivation path for `price_list_rate` and
  `base_price_list_rate` diverges under a price list currency different from the
  company currency, and none of that code has run.
- One selling price list (`Standard Selling`) and one buying list
  (`Standard Buying`). Per-customer and per-customer-group price lists exist in
  ERPNext (`Customer.default_price_list`, `Customer Group.default_price_list`)
  and are the correct model for the multi-price case that dominates the real
  dataset. The harness never uses them, and neither does the catalog.
- **Pricing Rules are now present on the instance but were never designed into
  any test.** They arrived between report generations and their only visible
  effect so far has been to corrupt the census (section 3) and to surface the
  drift finding (section 7). No test asserts anything about correct behaviour
  under a Pricing Rule, and `taxes_and_totals.py:199-201` takes a different
  branch when `has_pricing_rules` is set from the one the override-delta
  invariant (`harness/intent.py:302-321`) was written against.
- The differential and the contract proof are single-line. Only `otc-06` and the
  simulation use multiple lines (up to 5, `harness/simulate.py:84`).
- One tax configuration: none. No tax template is applied anywhere, so
  `total_taxes_and_charges` is zero in every result, and the
  `grand_total == net + taxes` invariant has never been tested with a non-zero
  taxes term.

## 9. The override reason cannot be verified, and never will be

Both implementations require a non-empty reason and nothing more. There is no
check on its content, no separate approver identity, no threshold above which a
write is queued rather than executed.

The repo demonstrates the limit itself: in the Phase 6 result, all 1,091
recorded overrides carry reasons generated by a format string
(`harness/simulate.py:145-147`). The headline number for "documents that explain
themselves" is a script explaining itself.

This is not a bug to be fixed. It is the boundary of what a write-time control
can establish. What the control produces is a contemporaneous, durable statement
that the caller was deviating and by how much from a value computed at that
moment, attributed to an identity (`bill_intent.py:69` records
`frappe.session.user`). It does not produce evidence that a human decided
anything.

## 10. The dataset gives a range, not a number, and the range is one seller

The off-reference rate depends entirely on the definition of the reference
price. Over the same 1,033,527 lines from `data/sales_clean.csv`:

| reference | off-reference | why it is wrong on its own |
|---|---|---|
| per-SKU median, pooled | 31.4% | Bimodal seller. A two-price SKU shows roughly half its lines off by construction. |
| per-customer modal for that SKU | 3.4% | 42.9% of lines are the only purchase of that SKU by that customer. |
| per-customer modal, pairs bought 2+ times | 6.0% | The honest cut. |
| per-customer modal, pairs bought 6+ times | 8.9% | Where a usual price genuinely exists. |

The first two are in `reports/dataset_analysis.json`. The last two are
reproducible from `data/sales_clean.csv` (reproduced for claim 5.2) but are
**not written to any report file**, so they are the only figures in this
document set that a reader must recompute rather than read.

Remaining caveats on all four:

- The dataset contains no price list. Every reference is a reconstruction.
- It is one UK wholesaler, 2009 to 2011, retail giftware. Its price dispersion
  is not evidence about any other industry, and the 6 to 9 percent figure should
  never be presented as an industry rate.
- The replay's off-list selection (`harness/simulate.py:130`, `:144`) still uses
  the pooled median, so `reports/simulation.json` inherits the artifact that the
  per-customer reference removes. Its 24.9% figure is on the old definition.

## 11. No detector was ever built or scored

The positioning compares post-hoc detection against write-time capture and
attributes a 6 to 9 percent false positive rate to the former. That figure is a
deviation rate over historical data. **No detector exists in this repo, none was
run, and no precision or recall figure was ever measured** (claim 5.5).

The claim that "precision does not improve with volume because the deviation is
real business behaviour" is an argument. It is a reasonable one and it follows
from the data being a real price distribution rather than noise, but it has not
been demonstrated, and a hostile reader is entitled to ask for the experiment.

## 12. The replay proves round-trip fidelity, not detection

`harness/simulate.py:128-134` and `:141-147`: the script computes which lines are
off-list, passes exactly those lines as declared overrides, and then reports how
many overrides were recorded. `naive_offlist_lines` and
`naive_offlist_unrecorded` are incremented on the same branch and are
necessarily equal.

The 1,091 = 1,091 result establishes that the intent path loses nothing across
1,000 real invoices and that the invariant check holds on real price
distributions at volume. It establishes nothing about detecting anything, and it
never could, because the script already knows the answer.

## 13. Version and citation drift

The live results were produced on ERPNext v16.34.1. The source tree available
for reading is 17.0.0-dev, and `vendor/` is gitignored, so a reader who clones
this repo has neither. README citations use v16 line numbers that do not resolve
in the tree that is present locally.

Nothing in the repo is currently reproducible from a clean clone without first
standing up Docker and re-deriving every line number. Pinning a vendored source
tree, or restating every citation against a single version, is unresolved.

## 14. Single-run results on an accumulated instance

Every number in `reports/` is one run against one local Docker instance
(`docker/pwd.yml`), with the default `Administrator` / `admin` credentials.
Nothing has been re-run for stability, and nothing has been run against a second
deployment.

The instance is not clean. It carries the fixtures, 2,000+ invoices from the
Phase 6 replay, the boundary role and user, and at least one Pricing Rule that
was not present for the earlier report generation. Section 3 covers what that
did to the census.

Two settings change the results and have never been varied:

- `Selling Settings.editable_price_list_rate` (default `0`). Turning it on makes
  `price_list_rate` editable in the Desk UI and invalidates the "no Desk session
  could produce this" framing (claim 2.13).
- `Selling Settings.maintain_same_sales_rate` (default `0`). Turning it on, with
  `maintain_same_rate_action` at its default of `Stop`, makes ERPNext itself
  reject a downstream rate that differs from the upstream document
  (`transaction_base.py:145-186`). Some of what this project positions as an
  unfilled gap is filled by that checkbox for writes that reference a prior
  document.

Neither has been tested in the on position.

## 15. The `Item.max_discount` interaction is read, not run

`selling_controller.py:266-272` bounds `discount_percentage` against
`Item.max_discount`. `taxes_and_totals.py:221` sets `discount_percentage` to `0`
when the caller supplied a `rate` below `price_list_rate`, and that assignment
runs before the validation (`selling_controller.py:52-56` calls
`super().validate()` first). Reading the source, a caller-supplied `rate` should
therefore not be bounded by `max_discount`.

**This has not been reproduced against a live instance.** It is the most
consequential untested reading in the repo, because if it is wrong then ERPNext
already ships a server-side control covering the flagship demonstration, and the
positioning changes. The experiment is small: set `Item.max_discount = 10` on
`HL-WIDGET-001` and post `rate: 1.0` against a list price of 250. Run it before
telling any customer that ERPNext has no defence here.
