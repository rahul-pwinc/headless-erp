# Limitations

What has not been tested, what is tested weakly, and what cannot be tested.
Nothing here is a promise of future work. It is a statement of the current
boundary so that a reader does not have to discover it themselves.

Claim numbers refer to [CLAIMS.md](CLAIMS.md).

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
surface.

Not measured:

- **Header-level derivation.** `conversion_rate`, `plc_conversion_rate`,
  `selling_price_list`, tax category resolution, payment terms template.
- **The `taxes` child table.** A caller supplying its own tax rows is not
  examined at all, by the census or by the intent engine.
- **Payment schedule rows.**
- **Stock valuation.** `incoming_rate`, `valuation_rate` on documents that
  actually move stock.
- **Non-numeric derived fields.** `harness/census.py:58` returns `None` for
  anything that is not a number, so every link field the engine treats as derived
  (`income_account`, `expense_account`, `cost_center`, `item_tax_template`,
  `warehouse`) was **never probed**. By claim 1.1 they should behave the same
  way, but that is an inference. `income_account` is the one that matters: it
  decides which GL account revenue lands in.

A derivation map covering more than `get_item_details` is the obvious next
piece of work. Until it exists, "the census" means one mechanism, and the
README's framing should say so wherever the number 196 appears.

## 3. The UI column is a replay, not a browser

`harness/oracle.py:159-183` reconstructs what the Desk UI would have produced by
calling the same whitelisted server method the browser calls, with a hand-built
44-key `ctx` copied from `transaction.js` (`harness/oracle.py:105-156`).

**No browser is driven anywhere in this repo.** Playwright is installed in the
virtualenv and is not used by any harness file.

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
column in the README should be read as "what the server returns when asked the
way the browser asks," which is a weaker statement than "what a human would have
seen."

## 4. No stock-item coverage, so two intents are declared and untested

`intents/catalog.yaml` declares 11 intents with 27 invariants. Two of them,
`fulfil` (Delivery Note) and `receive` (Purchase Receipt), carry the stock
invariants:

```
stock ledger qty x valuation == Stock In Hand GL balance
COGS posted uses valuation rate, never selling rate
GRNI balance equals goods received and not yet invoiced
```

None of these has ever executed. Both fixture items are created with
`is_stock_item: 0` (`harness/fixtures.py:61`, `:96`), as are the 150 items built
from the real dataset (`harness/simulate.py:52`). The corpus contains no
scenario using `fulfil` or `receive`: the 39 scenarios use `bill` (23),
`collect` (8), `expense` (7), `quote` (3), `pay` (3), `return` (2), `sell` (1),
`procure` (1), `source` (1), and one deliberately unknown intent.

So six of the 27 declared invariants belong to intents that no test touches, on
document types that need stock to exist at all. They are prose.

**More broadly, the catalog's invariants are prose.** The engine mechanically
checks three forms (`harness/intent.py:268-321`): debits equal credits,
`grand_total == sum(net_amount) + taxes`, and the override delta being recorded
in the correct field. The other 24 catalog invariants (AR subledger ties to the
control account, GRNI nets to zero once every receipt is billed, cumulative
returned qty never exceeds original, unallocated payment stays visible as an
advance, and the rest) are not linked to any executable check. The corpus
separately asserts a set of accounting properties, but there is no mapping
between a corpus scenario and the catalog invariant it is supposed to
demonstrate. A reader cannot answer "which of these 27 are actually enforced"
without reading all three files.

## 5. One company, one currency, one price list, single-line-dominant

- One company, `Headless Test Co`, created by the setup wizard
  (`harness/fixtures.py:24-47`).
- One currency, INR, with `conversion_rate: 1` and `plc_conversion_rate: 1`
  hardcoded in every header the harness builds. **Multi-currency is entirely
  untested.** This matters more than it sounds: the derivation path for
  `price_list_rate` and `base_price_list_rate` diverges under a price list
  currency different from the company currency, and none of that code has run.
- One selling price list (`Standard Selling`) and one buying list
  (`Standard Buying`). Per-customer and per-customer-group price lists exist in
  ERPNext (`Customer.default_price_list`,
  `Customer Group.default_price_list`) and are the correct model for the
  multi-price case that dominates the real dataset. The harness never uses them,
  and neither does the catalog.
- **Pricing Rules are never exercised.** `taxes_and_totals.py:199-201` short-circuits
  the rate reconciliation when `has_pricing_rules` is set, taking a different
  branch from the one this project's invariant check
  (`harness/intent.py:302-321`) was written against. On a deployment that uses
  Pricing Rules, the override-delta invariant's behaviour is unknown.
- The differential and the contract proof are single-line. Only `otc-06` and the
  simulation use multiple lines (up to 5, `harness/simulate.py:84`).
- One tax configuration: none. No tax template is applied anywhere, so
  `total_taxes_and_charges` is zero in every result, and the
  `grand_total == net + taxes` invariant has never been tested with a non-zero
  taxes term.

## 6. The override reason cannot be verified, and never will be

The contract requires a non-empty reason (`harness/intent.py:200-201`) and
nothing more. There is no check on its content, no separate approver identity,
no threshold above which a write is queued rather than executed.

The repo demonstrates the limit itself: in the Phase 6 result, all 1,091
recorded overrides carry reasons generated by a format string
(`harness/simulate.py:145-147`). The headline number for "documents that explain
themselves" is a script explaining itself.

This is not a bug to be fixed. It is the boundary of what a write-time control
can establish. What the control produces is a contemporaneous, frozen statement
that the caller was deviating and by how much from a value computed at that
moment. It does not produce evidence that a human decided anything. Any claim
that it does is false, and [POSITIONING.md](POSITIONING.md) and
[THREAT_MODEL.md](THREAT_MODEL.md) both say so.

## 7. The dataset analysis measures dispersion, not deviation from a price list

`reports/dataset_analysis.json` defines the list price as the per-SKU median
(`harness/simulate.py:36`, and the `method` field in the report). The UCI Online
Retail II dataset contains no price list. There is nothing else to use.

Consequences a reader should hold onto:

- The 31.4% off-list figure measures how far lines sit from a synthetic central
  tendency. A seller with a genuine trade price and a genuine retail price shows
  close to half its lines away from the median **by construction**.
- The same definition is used to pick which lines become overrides in the replay
  (`harness/simulate.py:130`, `:144`), so the replay's 24.9% off-list figure
  inherits the same artefact.
- The dataset is one UK wholesaler, 2009 to 2011, retail giftware. Its price
  dispersion is not evidence about any other industry.

The conclusion the number supports (a post-hoc detector cannot separate
legitimate off-list pricing from an agent that never looked) survives all three
caveats, because the artefact and the real effect both push in the same
direction. But the specific figure should never be quoted as "a third of this
company's sales were unauthorised discounts." It is not that.

## 8. The replay proves round-trip fidelity, not detection

`harness/simulate.py:128-134` and `:141-147`: the script computes which lines are
off-list, passes exactly those lines as declared overrides, and then reports how
many overrides were recorded. `naive_offlist_lines` and
`naive_offlist_unrecorded` are incremented on the same branch and are
necessarily equal.

The 1,091 = 1,091 result therefore establishes that the intent path loses nothing
across 1,000 real invoices and that the invariant check holds on real price
distributions at volume, both of which are worth having. It establishes nothing
about detecting anything, and it never could, because the script already knows
the answer.

## 9. Version and citation drift

The live results were produced on ERPNext v16.34.1. The source tree available
for reading is 17.0.0-dev, and `vendor/` is gitignored, so a reader who clones
this repo has neither. README citations use v16 line numbers that do not resolve
in the tree that is present locally.

Nothing in the repo is currently reproducible from a clean clone without first
standing up Docker and re-deriving every line number. Pinning a vendored source
tree, or restating every citation against a single version, is unresolved.

## 10. Single-run, single-instance results

Every number in `reports/` is one run against one local Docker instance
(`docker/pwd.yml`), with the default `Administrator` / `admin` credentials and
the default Selling Settings. Nothing has been re-run for stability, nothing has
been run against a second deployment, and nothing has been run against a
deployment with customisations, which is what every real ERPNext installation
is.

Two settings in particular change the results and have never been varied:

- `Selling Settings.editable_price_list_rate` (default `0`). Turning it on makes
  `price_list_rate` editable in the Desk UI and invalidates the "no Desk session
  could produce this" framing (claim 2.13).
- `Selling Settings.maintain_same_sales_rate` (default `0`). Turning it on, with
  `maintain_same_rate_action` at its default of `Stop`, makes ERPNext itself
  reject a downstream rate that differs from the upstream document
  (`transaction_base.py:145-186`). Some of what this project positions as an
  unfilled gap is filled by that checkbox for the subset of writes that
  reference a prior document.

Neither has been tested in the on position.

## 11. The `Item.max_discount` interaction is read, not run

`selling_controller.py:266-272` bounds `discount_percentage` against
`Item.max_discount`. `taxes_and_totals.py:221` sets `discount_percentage` to `0`
when the caller supplied a `rate` below `price_list_rate`, and that assignment
runs before the validation (`selling_controller.py:52-56` calls
`super().validate()` first). Reading the source, a caller-supplied `rate` should
therefore not be bounded by `max_discount`.

**This has not been reproduced against a live instance.** It is the single most
consequential untested reading in the repo, because if it is wrong then ERPNext
already ships a server-side control that covers the flagship demonstration, and
the positioning changes. The experiment is small: set `Item.max_discount = 10`
on `HL-WIDGET-001` and post `rate: 1.0` against a list price of 250. Run it
before telling any customer that ERPNext has no defence here.
