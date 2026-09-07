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

That consistency is the actual problem:

1. **The caller never sees the derived value.** A human watches 250.00 populate, then overrides it. They know what they changed and by how much. An API caller that supplies 1.00 receives no indication that a Price List entry existed at all. Nothing in the response says "you skipped derivation."

2. **The document is indistinguishable from a deliberate decision.** After the fact, `price_list_rate=250, rate=1, discount_amount=249` reads as an authorised 99.6% discount. There is no field, flag, or log entry separating *"a salesperson approved a discount"* from *"an agent did not know the list price."*

3. **Every accounting invariant holds.** Debits equal credits. The trial balance nets to zero. A property-based harness asserting ledger integrity finds nothing here.

The failure mode for agent-driven ERP is therefore not corruption. It is **laundering**: the system converts a machine's missing context into a record that looks like human judgment, and the audit trail actively conceals the difference. Catching it needs a differential oracle — run the same intent down both paths and diff — not an invariant checker.

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

**144 of 196 probes were protected.** ERPNext recomputes `amount`, `net_rate`, `base_rate`, `stock_qty`, `conversion_factor` and 16 other fields regardless of what the caller sends. This is not a system with no defences. The gap is specific and small, which is what makes it worth naming.

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

## Run it

```bash
python3 -m venv .venv && ./.venv/bin/pip install requests
docker compose -f docker/pwd.yml -p headless-erp up -d      # ~5 min first run
./.venv/bin/python harness/run.py
```

Defaults to `http://localhost:8080`, `Administrator` / `admin`. Report lands in `reports/latest.json`.

## Layout

```
harness/client.py     Frappe REST client (session auth, same path a browser uses)
harness/oracle.py     ctx reconstruction, both paths, the diff, GL balance check
harness/fixtures.py   idempotent master data (setup wizard, item, price, customer)
harness/run.py        CLI entry point
docker/pwd.yml        ERPNext v16.34.1 stack
```

## License

MIT for this harness. ERPNext is GPLv3 and Frappe Framework is MIT; neither is vendored here.
