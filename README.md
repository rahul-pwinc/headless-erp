# headless-erp

**A headless ERP is one where the API is the complete and authoritative surface: nothing enforced in a screen that isn't enforced in the API. If removing the UI changes what the system accepts, it was never headless.**

This repo tests that property. It starts with ERPNext, because it is the most complete open-source ERP whose source you can actually read.

## The finding

ERPNext's Desk UI derives item values before a document is ever saved. Pick an item, and the browser calls a server method that fills in the rate from the Price List, the tax template, the income account, the UOM conversion. A caller that speaks only to `/api/resource` never triggers that step.

Server-side, `AccountsController.set_missing_item_details` back-fills those values, but only when the field is `None`:

```python
# erpnext/controllers/accounts_controller.py:1127  (v16.34.1)
if (
    item.get(fieldname) is None
    or fieldname in force_item_fields
    or ...
):
    item.set(fieldname, value)
```

`force_item_fields` has exactly nine members (`accounts_controller.py:97`): `item_group`, `brand`, `stock_uom`, `is_fixed_asset`, `pricing_rules`, `weight_per_unit`, `weight_uom`, `total_weight`, `valuation_rate`.

`rate` and `price_list_rate` are in neither set.

So a caller that supplies its own `rate` keeps it. The Price List is never consulted. `Selling Settings.validate_selling_price` defaults to `0`, so nothing catches selling below cost either.

## Measured result

Same business intent both ways: 4 units of an item priced at 250.00 in `Standard Selling`.

| | UI-derivation path | Naive API path |
|---|---|---|
| `rate` | 250.00 | **1.00** |
| `grand_total` | 1000.00 | **4.00** |
| GL entries balance | ✅ | ✅ |

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

**The books balance perfectly.** Debits equal credits. The trial balance nets to zero. Every accounting invariant holds. The document is structurally flawless and semantically garbage.

That is the point of this repo. **Invariant checking cannot detect this class of defect.** A property-based test harness asserting "debits equal credits, subledgers tie to the GL, stock value reconciles" finds nothing here. Catching it requires a *differential* oracle: run the same intent down both paths and diff the documents.

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
