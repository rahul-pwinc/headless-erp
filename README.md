# headless-erp

**A headless ERP is one where the API is the complete and authoritative surface: nothing enforced in a screen that isn't enforced in the API. If removing the UI changes what the system accepts, it was never headless.**

This repo tests that property. It starts with ERPNext, because it is the most complete open-source ERP whose source you can actually read.

## The finding

ERPNext's Desk UI derives item values before a document is saved. Pick an item, and the browser calls a whitelisted server method that fills the rate from the Price List, the tax template, the income account, the UOM conversion. A caller that speaks only to `/api/resource` never triggers that step.

Server-side, `AccountsController.set_missing_item_details` back-fills those values, but **only when the field is `None`**:

```python
# erpnext/controllers/accounts_controller.py:1127  (v16.34.1)
if (
    item.get(fieldname) is None
    or fieldname in force_item_fields
    or ...
):
    item.set(fieldname, value)
```

`force_item_fields` has exactly nine members (`accounts_controller.py:97`): `item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`, `weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate`. `rate` and `price_list_rate` are in neither set.

So a caller that supplies its own `rate` keeps it, and the Price List is never consulted for that row.

## Measured result

Same business intent both ways: 4 units of an item priced at 250.00 in `Standard Selling`, against a live ERPNext v16.34.1.

| field | UI-derivation path | Naive API path |
|---|---|---|
| `rate` | 250.00 | **1.00** |
| `price_list_rate` | 250.00 | 250.00 |
| `discount_amount` | 0.00 | **249.00** |
| `grand_total` | 1000.00 | **4.00** |
| GL entries balance | yes | yes |

```
=== UI-path invoice: ACC-SINV-2026-00007 ===
  Debtors - HTC       Dr    1000.00   Cr       0.00
  Sales - HTC         Dr       0.00   Cr    1000.00
  TOTAL               Dr    1000.00   Cr    1000.00   balanced=True

=== API-path invoice: ACC-SINV-2026-00008 ===
  Debtors - HTC       Dr       4.00   Cr       0.00
  Sales - HTC         Dr       0.00   Cr       4.00
  TOTAL               Dr       4.00   Cr       4.00   balanced=True
```

## What this does and does not show

**It does not show that the API accepts something the UI forbids.** `rate` is not read-only (`sales_invoice_item.json`), so a human can type 1.00 deliberately. The server then computes `discount_amount = 249.00` exactly as the browser would (`transaction.js:70-90`). The resulting document is **internally consistent**.

**It does not show that the caller is blind.** An earlier version of this README claimed an API caller "receives no indication that a Price List entry existed." That was wrong, and it is retracted. The write response returns the saved document, which contains `price_list_rate: 250.0` and `discount_amount: 249.0`. A caller that reads its own response can see exactly what the list price was. Verified empirically.

**What survives is the post-hoc point, and only that.**

Six months later, the record reads `price_list_rate=250, rate=1, discount_amount=249`. That is indistinguishable from an authorised 99.6% discount. Nothing in the document, the ledger, or any log separates *"a salesperson approved this"* from *"an agent never looked."* The system does not record whether a decision occurred, only its arithmetic consequence.

That is a narrower claim than the one this repo opened with. It is also the only one the evidence supports.

**On novelty, plainly:** a caller-supplied rate winning over the price list is documented, intended ERPNext behaviour, not a bug. And "the API does not enforce field-level `read_only`" is a general property of Frappe, true of every read-only field on every doctype, not something specific to ERPNext or to pricing. Neither is a discovery. What is worth naming is the *consequence*: the class of ERP field where the server holds a correct value, the caller may overwrite it, and the resulting record is downstream-indistinguishable from a deliberate human decision.

## Phase 2 — the census

The single-field finding generalises. `harness/run_census.py` asks the server what it *would* derive for an item row, then supplies a different value for each derived field and reads back what was stored. Nine transaction doctypes, no browser.

```
probes run          : 196
  protected         : 144
  silently accepted :  42
  rejected          :  10

silently accepted   : 6 distinct fields
  rate, price_list_rate, discount_amount, discount_percentage,
  weight_per_unit, min_order_qty
```

**Read that 144 honestly.** Most of the "protected" fields are arithmetic the server recomputes anyway: `amount`, `net_amount`, `base_rate`, `base_amount`, `net_rate`, `stock_qty`. Those are not a derivation contract defending itself, they are a total being recalculated. Counting them alongside genuine derivation decisions inflates the ratio, and an earlier version of this section did exactly that.

The honest shape is smaller: **6 distinct fields, on 8 of 9 doctypes, from one derivation path** (`get_item_details`). The 196 figure is 6 fields times the doctypes they appear on, not 196 independent findings. All 10 "rejected" probes are the same probe — `qty` mutated to 0.016, failing validation — not 10 separate results.

`reports/derivation_map.md` extends this beyond `get_item_details`; until it lands, treat the census as covering one mechanism.

### `weight_per_unit` is in the force list and still accepted

This looked like a contradiction and was left unexplained in an earlier version. It is not a broken probe. `get_item_details.py:620`:

```python
"weight_per_unit": ctx.weight_per_unit or item.get("weight_per_unit"),
```

The derivation reads the caller's own value back out of the row. So the force-overwrite does fire, and it writes the caller's number. `total_weight` then computed 28.0 from the fabricated 7.0.

**Membership in `force_item_fields` guarantees the derived value wins. It does not guarantee the derived value is independent of the caller.** That is a sharper statement of the mechanism than "the server declines to overwrite," and it is the more interesting failure: a protection list that is circular for at least one of its nine members.

### The part the UI cannot do

Cross-referencing the six accepted fields against `read_only` in the child DocType JSON:

| field | read-only in Desk UI | accepted over API |
|---|---|---|
| `price_list_rate` | **5 of 9** child doctypes | yes |
| `weight_per_unit` | **4 of 8** child doctypes | yes |
| `min_order_qty` | **1 of 1** (Material Request) | yes |
| `rate` | 0 of 9 | yes |
| `discount_amount` | 0 of 8 | yes |
| `discount_percentage` | 0 of 8 | yes |

`price_list_rate` is rendered read-only on Sales Invoice, Sales Order, Quotation, Delivery Note and Material Request. **A human cannot type into it.** The API accepts it.

That matters more than the `rate` case. `price_list_rate` is the *reference* price against which `discount_amount` is computed. Forge it and the discount is measured against a baseline that never existed — the audit trail's own reference point is fabricated, so the override cannot be detected even in principle by comparing rate to list.

The `rate` case is laundering: a machine's missing context becomes a record that reads like human judgment. The `price_list_rate` case is stronger: it produces a document no Desk session could have created.

## The control matters

The harness runs two modes, and the contrast is what makes the finding falsifiable:

- **`omit`** — caller leaves `rate` out. Server fills it from the Price List. **0 gaps across 14 compared fields.**
- **`assert`** — caller supplies `rate: 1`. Server keeps it. **1 gap.**

Showing `assert` alone would not distinguish "the server never derives" from "the server declines to overwrite." It is the latter, and that is a deliberate design choice — the field is how manual discounts survive a re-save. The problem is not that the behavior is wrong. **The problem is that it is invisible to the caller, and callers are increasingly not human.**

## Verified

- ERPNext **v16.34.1** (current stable, via `frappe_docker` `pwd.yml`) — live instance, reproduced end to end
- Source condition identical on **v17.0.0-dev** (`accounts_controller.py:785`, same nine `force_item_fields`)
- Frappe's own docs state the general case: *"Client Script validations only apply to the standard form view accessible through the browser. To apply validations through API or System Console access, you need to use Server Scripts instead."*
- ERPNext's GL layer **does** defend structural integrity — `gl_entry.py:232-275` blocks group accounts, inactive accounts, frozen accounts, and cost-center/company mismatch. Claims that agents can "corrupt the books" via these paths are wrong. The defect here is different and narrower.

## Not yet verified

Stated explicitly so nothing here is read as more than it is.

- **Playwright anchor.** The UI path is currently reconstructed by calling `erpnext.stock.get_item_details.get_item_details` with the exact 44-field `ctx` that `transaction.js` builds (`public/js/controllers/transaction.js:818`). That reconstruction has **not yet been proven equal to a real browser session.** Until it is, treat the UI column as a faithful replay, not as observed browser output.
- **Other doctypes.** Only Sales Invoice, single line, one company, one currency. Purchase Invoice, Delivery Note, Payment Entry, and multi-currency are untested.
- **The 484 `set_query` filters.** ERPNext client scripts contain 484 `set_query`/`get_query` calls constraining which linked records are selectable. Whether each has a server-side backstop is **unmeasured**. Do not assume they don't.
- **Public MCP servers.** Several public MCP servers wrap this REST API for AI agents. Whether any specific one exhibits this gap has **not been tested here.** The architectural argument suggests they inherit it; that is a hypothesis, not a result.

## Phases 3 and 4 — the intent layer

`intents/catalog.yaml` declares 11 business intents (`quote`, `sell`, `fulfil`, `bill`, `collect`, `source`, `procure`, `receive`, `expense`, `pay`, `return`) with 27 invariants. It deliberately does not restate DocType structure, which is already introspectable. It adds what metadata does not carry: preconditions, what the system derives, what it refuses, what holds afterwards, and how each is reversed.

The refuse/override split is not a taste call. It falls out of the census:

| census result | contract | why |
|---|---|---|
| accepted **and** read-only in the UI | **refuse** | no Desk session could set it, so no caller may |
| accepted **and** editable in the UI | **override** | a human may do it deliberately, so a caller may, but not silently |
| recomputed server-side regardless | derive | never accepted |

`harness/intent.py` enforces it. Every intent derives from the server first, then applies declared overrides on top of the derived value, recording the delta.

```
7/7 cases behaved as specified

PASS  1. clean bill(): derives the list price, no caller input
        ACC-SINV-2026-00034  grand_total=1000.0  overrides=0  invariants=2 failures=0
PASS  2. bill() with price_list_rate supplied  ->  REFUSED (read-only in the UI)
PASS  3. bill() with rate smuggled into the line  ->  REFUSED (must be a declared override)
PASS  4. bill() override with no reason  ->  REFUSED
PASS  5. bill() override WITH a reason  ->  allowed, recorded, still consistent
        recorded: rate 250.0 -> 1.0 :: goodwill credit, approved by finance
PASS  6. bill() override of a derived-only field  ->  REFUSED
PASS  7. return with a rate override  ->  REFUSED (intent-level refuse beats the default)
```

Case 5 is the point. The same 4.00 invoice ERPNext accepted in silence is still reachable, because sometimes a business genuinely does discount 99.6%. What changed is that it now carries what the price should have been, that someone overrode it, and why. Case 7 shows an intent can be stricter than the default: a return reprices history, so it gets no override path at all.

## Phase 5 — the use-case corpus

`corpus/scenarios.yaml` holds 39 scenarios across 7 categories. Each states an accounting **principle** and asserts against it. The oracle is double-entry bookkeeping, not ERPNext's behaviour and not the Desk UI.

```
  collection             7/7     partial payments, overallocation, settlement to zero
  commitments            5/5     quotes and orders post nothing
  derivation_contract   10/10    refuse / override / derive
  guards                 4/4     drafts, unknown intents, docstatus
  order_to_cash          6/6     revenue recognition, receivables, multi-line
  procure_to_pay         5/5     payables, supplier payment, part payment
  returns                2/2     a return may not reprice history

  TOTAL                 39/39
```

Scenarios read as accounting, not as API calls:

```yaml
- id: col-02-partial-payment-leaves-remainder
  principle: A part payment reduces the receivable by exactly what was paid, no more.
  when:
    - {intent: bill,    as: inv, lines: [{item: item_code, qty: 4}]}
    - {intent: collect, allocate: [{doc: $inv, amount: 400}]}
  then:
    - {assert: outstanding, doc: $inv, value: 600}
    - {assert: balanced}
```

One scenario failed on the first run: `der-08-unpriced-item-refused` expected the engine to refuse an item with no resolvable price, and it did not. The cause was the scenario, not the engine — the fixture item had a price in the buying list, so a price *was* resolvable. Fixed by adding an item with no price in any list, which is what the principle actually requires. That is the corpus doing its job: it caught a lie in its own setup.

## Why this is not just a test suite

The corpus is the oracle the invariant approach cannot be. Consider what each layer catches:

| approach | catches a 4.00 invoice that should be 1000.00? |
|---|---|
| accounting invariants (debits == credits) | **no** — it balances perfectly |
| ERPNext's own 4,153 tests | **no** — the document is valid |
| diffing against the Desk UI | only if you accept the UI as truth |
| **a principle: "the total is the agreed price times quantity"** | **yes** |

That last row is the whole project. Correctness in an ERP is defined by the business and by accounting, not by what a particular implementation happens to do.

## Phase 6 — real data, at scale

A single fixture item proves a mechanism. It does not prove the mechanism matters. So: [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii) — two years of a real UK wholesaler, 1,067,371 line items, 53,628 invoices, 5,305 SKUs, 5,942 customers, 43 countries, GBP 20.97M gross.

### First, how often does real commerce sell off list?

Treating the median price per SKU as the price list, and excluding non-product codes (postage, manual adjustments, bank charges) and deviations above 500% as data-quality noise — both exclusions stated, 8,143 lines dropped of 1,041,670:

```
  lines analysed  : 1,033,527
  at list price   :   709,344   68.6%
  below list      :   104,513   10.1%
  above list      :   219,670   21.3%
  OFF LIST        :   324,183   31.4%

  revenue on off-list lines : 50.8% of gross
  SKUs sold at more than one price : 4,309 / 4,873  (88.4%)
```

**Nearly a third of real line items do not sell at list price, and they carry half the revenue.** The deviation is bimodal — this wholesaler also sells retail, so the same SKU legitimately has two price points depending on the customer.

This is the finding that makes the defect serious, and it is the opposite of what you would assume. **Off-list is not an anomaly. It is normal.**

Which kills the obvious mitigation. You cannot detect a mispriced agent write by flagging prices that differ from the list, because 31.4% of correct writes differ from the list. There is no statistical signal to separate a legitimate wholesale price from an agent that never looked up the price at all. The only place the distinction exists is at the moment of writing, in whether anyone recorded a decision.

### Then, replay it

1,000 real invoices, written twice — once by a naive caller posting the price it holds (what an MCP server does), once through the intent layer.

```
  invoices written        : 1,000 naive + 1,000 intent
  line items              : 4,380
  off-list lines          : 1,091  (24.9%)

  naive  -> unrecorded off-list prices : 1,091
  intent -> recorded overrides         : 1,091
  intent -> invariant failures         :     0
  elapsed                              :   378s
```

Same invoices. Same totals. Same ledger. The only difference is that 1,091 line items either explain themselves or do not.

### The trial balance proves nothing, and that is the point

An earlier version of this README presented a balanced trial balance as a finding. It is not one, and presenting it that way was wrong.

ERPNext **refuses to post an unbalanced voucher**. `raise_debit_credit_not_equal_error` in `erpnext/accounts/general_ledger.py` throws before anything reaches the ledger. So:

```
  GL entries       : 6,466
  submitted invoices: 2,223
  total debits     :  451,856.02
  total credits    :  451,856.02
  difference       :        0.00
```

That result was **guaranteed by construction**. It could not have come out any other way. Running it and reporting it as a discovery would insult anyone who knows the system.

The only legitimate use of it is the inverse: **balance is enforced, therefore balance carries no information about correctness.** A control that cannot fail is not a control. Any assurance process whose ledger check is "do debits equal credits" will pass this company, and would pass it no matter what prices were written. That was always the argument; the demonstration added nothing to it and has been demoted to an illustration.

### What the replay does and does not measure

Equally plainly: **the replay is a round-trip test, not a detection test.**

`harness/simulate.py` computes which lines differ from list price, passes exactly those as overrides, and then reports that the engine recorded that many overrides. Of course it did. It proves the override path preserves information end to end at volume. It does **not** show the system detected anything, because the system was told.

This is not a defect that can be engineered away, and that matters more than the demo:

**Post-hoc detection of this class of error is impossible.** If a third of legitimate lines price off-list, no rule over stored documents can separate a real wholesale price from an agent that never looked one up. Both produce the same row. The information that would distinguish them, namely whether anybody decided, exists only at the moment of writing and only if it is captured then.

So this is not a scanner and it is not an anomaly detector. **It is a write-time control.** That is a narrower product than "we find your bad data," and it is the only honest one.

### What the real data found in our own code

The 21.3% of lines priced *above* list broke an invariant in `intent.py`. ERPNext records the delta in two different places depending on direction — `discount_amount` below list, `margin_rate_or_amount` above (`transaction.js:70-90`, confirmed on live documents) — and the check only knew the discount case. It passed every premium sale silently, which is the same class of bug this project exists to find, in the tool built to find it.

The fixture data could never have surfaced it. Real distributions did.

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install requests
docker compose -f docker/pwd.yml -p headless-erp up -d      # ~5 min first run
./.venv/bin/python harness/run.py          # the original differential
./.venv/bin/python harness/run_census.py   # Phase 2: silent-acceptance census
./.venv/bin/python harness/prove_intent.py # Phase 4: the contract, 7 cases
./.venv/bin/python harness/run_corpus.py   # Phase 5: the corpus, 39 scenarios
./.venv/bin/python harness/simulate.py     # Phase 6: replay real invoices at scale
```

Defaults to `http://localhost:8080`, `Administrator` / `admin`. Report lands in `reports/latest.json`.

## Layout

```
harness/client.py     Frappe REST client (session auth, same path a browser uses)
harness/oracle.py     ctx reconstruction, both paths, the diff, GL balance check
harness/fixtures.py   idempotent master data (setup wizard, item, price, customer)
harness/run.py        CLI entry point
harness/census.py     Phase 2 - silent-acceptance probe
harness/intent.py     Phase 4 - the intent executor (refuse / override / derive)
harness/corpus.py     Phase 5 - scenario engine, asserts on accounting principles
intents/catalog.yaml  11 business intents, 27 invariants
corpus/scenarios.yaml 39 accounting-determined scenarios
harness/simulate.py   Phase 6 - replays UCI Online Retail II through both paths
docker/pwd.yml        ERPNext v16.34.1 stack
```

## License

MIT for this harness. ERPNext is GPLv3 and Frappe Framework is MIT; neither is vendored here.
