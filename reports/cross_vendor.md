# Do the eight findings generalise? — Odoo (source), NetSuite and SAP (public docs)

**Question.** Are the eight findings in `derivation_map.md` a property of agent-driven ERP, or a
property of Frappe?

**Bottom line.** Mostly a property of Frappe — but not entirely, and the exception is the one that
matters commercially.

- **Odoo inverts Frappe's default.** A computed stored field in Odoo is `readonly=True` unless the
  author says otherwise (`odoo/fields.py:443`), and a caller-supplied value for such a field is
  **discarded at create** (`odoo/models.py:4737-4742`). Frappe's default is the opposite: keep
  whatever the caller sent unless a rule says otherwise. Same problem, opposite defaults.
- **Our headline finding does not survive the crossing.** `sale.order.currency_rate` is *not*
  marked `readonly=False`, so Odoo throws away a caller-supplied FX rate at create and recomputes it
  (`addons/sale/models/sale_order.py:182-186`). ERPNext's 87x GL error is a Frappe defect, not an
  ERP-general one. **Do not sell finding #1 as universal.**
- **The `rate` finding does survive.** `sale.order.line.price_unit` is explicitly
  `readonly=False` (`addons/sale/models/sale_order_line.py:141-145`), so Odoo keeps a caller's price
  exactly as ERPNext does. Two independent vendors made the same deliberate choice, which is
  evidence it is a design position rather than an oversight.
- **NetSuite and SAP are largely undetermined from public documentation.** I found two concrete
  NetSuite behaviours and essentially nothing field-level for SAP. Section 4 says so plainly rather
  than inferring.

---

## 0. Method and provenance

| Vendor | Basis | What I actually did |
|---|---|---|
| **Odoo 17.0** | **source, code-read** | `git clone --depth 1 --branch 17.0 --filter=blob:none https://github.com/odoo/odoo` → `vendor/odoo`, HEAD `aa89dc2d`, `heads/17.0`. Line numbers below are from that tree. |
| **NetSuite** | **doc-read** | Oracle NetSuite Applications Suite help pages. |
| **SAP S/4HANA** | **doc-read, thin** | SAP Help Portal is a JavaScript SPA; `WebFetch` retrieved only the page shell for the two most relevant pages, so I could not read the field metadata. |

Tags used throughout: **[code]** read in source, **[doc]** stated in vendor documentation,
**[inference]** my reasoning from the above, flagged as such.

**I did not run Odoo.** Every Odoo claim is code-read. Section 6 lists what a live Odoo instance
would settle.

---

## 1. Odoo's mechanism, and why it is the mirror image of Frappe's

Three pieces of the ORM decide everything.

### 1.1 Computed fields default to `readonly=True`

```python
# odoo/fields.py:435-443
if attrs.get('compute'):
    # by default, computed fields are ... readonly (unless inversible)
    attrs['readonly'] = attrs.get('readonly', not attrs.get('inverse'))
```
**[code]** A field with a `compute=` and no `inverse=` is readonly unless the author explicitly
writes `readonly=False`.

### 1.2 A caller value for a `precompute` + `readonly` field is discarded at create

```python
# odoo/models.py:4737-4742   (inside _prepare_create_values)
# also discard precomputed readonly fields (to force their computation)
bad_names.extend(
    fname
    for fname, field in self._fields.items()
    if field.precompute and field.readonly
)
```
**[code]** `bad_names` are `pop`ped from the caller's `vals` at `models.py:4750-4751`. The comment
states the intent outright: *force their computation*.

### 1.3 Otherwise, compute runs only for fields the caller omitted

```python
# odoo/models.py:4791-4799   (inside _add_precomputed_values)
for fname, field in precomputable.items():
    if fname not in vals:
        vals[fname] = field.convert_to_write(record[fname], self)
```
**[code]** `if fname not in vals` — **this is Frappe's "only if the caller left it None", expressed
per-field.** The two systems share the same underlying rule. What differs is the default: Frappe
opts fields *out* of derivation one at a time (`force_item_fields`, nine members); Odoo opts fields
*in* to caller control one at a time (`readonly=False`).

### 1.4 The important asymmetry: Odoo re-derives on write

`models.write()` calls `self.modified(vals)` (`odoo/models.py:4469`), which marks every stored
computed field depending on a changed field for recomputation (`models.py:6775-6805`).

So in Odoo a manually supplied `price_unit` is **wiped** the next time the caller writes
`product_id`, `product_uom` or `product_uom_qty` — those are exactly `_compute_price_unit`'s
dependencies (`sale_order_line.py:462`). In ERPNext the manual `rate` survives every re-save; that
is the documented purpose of the "only if None" rule.

The failure modes are therefore *different*, not merely differently-sized:

| | ERPNext | Odoo |
|---|---|---|
| caller supplies a price | kept, silently, forever | kept at create |
| caller later edits quantity | price still kept | **price silently overwritten from the pricelist** |
| what the caller must fear | its value is honoured and looks deliberate (laundering) | its value vanishes without notice |

**[inference]** Odoo's version is less dangerous for an audit trail and more dangerous for an
integration: a bulk price import that later touches quantity loses its prices. Neither system tells
the caller which happened.

### 1.5 `readonly` is *not* enforced on write — an important limit on the above

`check_field_access_rights` (`odoo/models.py:3470-3495`) checks field **groups** only. There is no
`readonly` check in the write path. So §1.2's protection is specifically a **create-time**
protection for `precompute and readonly` fields.

**Flagged uncertainty:** I did not establish what happens if a caller `write()`s `currency_rate`
directly on an existing `sale.order`. It is plausibly accepted and then recomputed on the next
dependency change. **This needs a live Odoo test before anyone claims "Odoo protects the FX rate"
without qualification.** What I can state is narrower and is what §2 says: *at create*, the value is
discarded.

---

## 2. Odoo, field by field, against our eight findings

All **[code]**, Odoo 17.0.

| our finding | Odoo counterpart | declaration | caller value at create |
|---|---|---|---|
| #1 `conversion_rate` (87x GL error) | `sale.order.currency_rate` | `sale_order.py:182-186` — `compute`, `store=True`, `precompute=True`, **no `readonly=False`** | **DISCARDED, recomputed** from `res.currency._get_conversion_rate` (`sale_order.py:415-421`) |
| #1 (invoice layer) | `account.move.line.currency_rate` | `account_move_line.py:125-128` — computed, **not stored** | **cannot be persisted at all**; computed on every read (`:684-695`) |
| — `rate` | `sale.order.line.price_unit` | `sale_order_line.py:141-145` — **`readonly=False`** | **KEPT** |
| #3 pricing-rule discount | `sale.order.line.discount` | `sale_order_line.py:147-151` — **`readonly=False`** | **KEPT** |
| #7 `conversion_factor` (stock qty) | *no counterpart exists* | factor lives on the `uom.uom` **master** (`addons/uom/models/uom_uom.py:59-62`); lines carry only `product_uom` (a link) and `product_uom_qty` | **not forgeable — there is no per-line factor field** |
| #7 (stock layer) | `stock.move.product_qty` | `stock_move.py:50-53`, computed via `move.product_uom._compute_quantity(...)` (`:278-282`) | derived from the UOM master, not from a line-level number |
| #5 Item Price write-back | *none found* | — | Odoo pricelists are not written back from transactions in the code I read |
| #8 `weight_per_unit` circular | *no counterpart* | Odoo reads weight from `product.product` | — |

### The two that matter

**#1 does not generalise.** This is the finding the project now leads with, and Odoo defends it by
construction. The `currency_rate` field simply never opted into caller control. Any claim that
"agent-driven ERP launders FX rates" is false as stated; the true claim is narrower and still
valuable — *ERPNext does, and the reason is a framework default that inverts Odoo's*.

**The `rate`/`discount` finding does generalise.** Both vendors explicitly opted these fields into
caller control — Odoo by writing `readonly=False` on exactly those two fields among the pricing set,
ERPNext by omitting them from `force_item_fields`. **[inference]** Two independent teams reaching the
same conclusion is good evidence that a manually-supplied price is a requirement, not a bug — which
matches the README's own position that the defect is invisibility, not the behaviour.

### The structural difference worth naming

ERPNext's original bug class is *derivation that lives in the browser*: Desk JS calls
`get_item_details`, `/api/resource` does not. **Odoo has almost no equivalent for these fields**,
because the derivation lives in ORM computed fields that run identically for the web client and for
XML-RPC/JSON-RPC — `execute_kw` calls `create()`/`write()` directly, with no separate API layer.
The `@api.onchange` methods remaining on `sale.order.line` are two, and both are advisory:
`_onchange_product_id_warning` (`sale_order_line.py:956`) and `_onchange_product_packaging_id`
(`:973`). **[code]** Neither sets a price.

**[inference]** Odoo appears to have migrated pricing out of `onchange` and into computed fields in
earlier versions, which would be the same defect class being fixed architecturally. I did not verify
that history — it would need a look at Odoo 13/14 and I did not clone them.

---

## 3. NetSuite — two concrete behaviours, the rest undetermined

**[doc]** Two documented cases of the server intervening on caller-supplied values:

1. **`grossAmt` triggers recalculation (2026.1+).** "when you set the **grossAmt** field for a
   taxable transaction line through REST web services, NetSuite recalculates the line **amount**
   field based on the applicable tax code, tax rate, and tax rounding rules. This behavior matches
   the NetSuite UI. In previous releases, REST web services ignored submitted **grossAmt** values."
   — [Sales Order, NetSuite Applications Suite](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159665260887.html)

   Note what this admits: until 2026.1, the REST surface and the UI **disagreed**, and the fix is
   described as making REST "match the NetSuite UI". That is our thesis stated by the vendor.

2. **Item group member lines cannot be priced on create.** "it is not possible to change or update
   the quantity, rate, and amount value of the member items using the create request. You can only
   update the quantity, rate, and amount values using the PATCH request." — same page.

**What I could not establish.** Whether a plain `salesOrderItem.rate` supplied on create is kept or
re-derived from the price level. The REST API Browser carries the field metadata, but it is served
from inside an authenticated NetSuite account and I have no instance.

**A tempting quote I am deliberately not using as evidence.** An Oracle page states: "By default,
NetSuite Connector overrides the prices in NetSuite and uses the item prices from the order... If
you've set up your Order Item mappings to use a price level other than -1 or Custom, NetSuite prices
will be used instead, which can cause mismatched order totals."
([Ensuring That NetSuite and Storefront Order Totals Are Matching](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162563576130.html))

This is suggestive of a *sourcing* model — rate derived from the price level unless the caller
declares the sentinel `Custom (-1)` — which would be a genuinely better design than ERPNext's,
because the override is **explicit and recorded in a field**. But the page is scoped to **NetSuite
Connector**, a specific integration product, not to SuiteTalk generally. **Treating it as a
statement about the API would be exactly the kind of smoothing this project is trying to avoid.**
Verdict: **undetermined**, with a strong hypothesis worth one hour of a NetSuite account to settle.

---

## 4. SAP S/4HANA — undetermined from public documentation

I could not read the field-level metadata. `help.sap.com` is a JavaScript SPA and returned only the
page shell for both `API_SALES_ORDER_SRV` (Sales Order A2X, OData V2) and the Item Pricing Element
entity. The `apis.io` mirror 404s.

What is established **[doc]**, and it is thin:

- `API_SALES_ORDER_SRV` is the OData service for creating/reading sales orders, and exposes pricing
  elements as a sub-entity ("Item Pricing Element" — "data related to prices, discounts, surcharges,
  freight, or taxes").
- `ConditionIsManuallyChanged` exists as a property on the pricing element entity, indicating a
  condition was manually changed. **[inference]** — a field of exactly this name implies SAP records
  *whether a price was overridden*, which is the audit marker ERPNext lacks. I am not claiming SAP
  enforces anything; I am noting the field exists.
- OData services generally mark computed/system-determined fields with `Necessity: read-only`, and
  read-only fields "are not filled during a Post request". This is a **general OData convention**
  statement, not a statement about any specific pricing field.

**Verdict: undetermined.** To settle it, read the EDMX metadata for `API_SALES_ORDER_SRV` from SAP
Business Accelerator Hub (needs a login) and check the `sap:creatable` / `sap:updatable` annotations
on `NetAmount`, `ConditionRateValue` and `ConditionAmount`. That is a bounded task; I flag it rather
than guess.

---

## 5. The eight findings, classified

Per your (a)/(b)/(c). "General" means *the mechanism, not the exact field*.

| # | finding | class | basis |
|---|---|---|---|
| 1 | **`conversion_rate` accepted verbatim → 87x GL error** | **(a) Frappe-specific** | Odoo discards it at create (`sale_order.py:182-186` + `models.py:4737-4742`); `account.move.line.currency_rate` is not even stored. SAP/NetSuite undetermined. **[code]** |
| 2 | **Tax template named but rows not applied** | **(c) undetermined** | No Odoo counterpart read — Odoo taxes are a many2many on the line (`tax_id`), a different model entirely. SAP's condition-record model is structurally unlike a "template", so the finding may not even be expressible there. |
| 3 | **Pricing rule computes discount off caller's `price_list_rate`** | **(b) likely general** | Odoo's `discount` is `readonly=False` (`sale_order_line.py:147-151`), so a caller-supplied discount is likewise kept. Whether Odoo also *feeds it back* into its own pricelist computation — the circular part — I did not trace. Half-verified. |
| 4 | **`fetch_from` enforced; `fetch_if_empty` the hole** | **(a) Frappe-specific** | `fetch_from` is a Frappe construct. Odoo's `related=` fields are the analogue and default to `readonly=True` (`fields.py:444-450`). **[code]** |
| 5 | **Caller's rate written back into the Price List master** | **(a) Frappe-specific**, and even then opt-in | Gated on `Stock Settings.auto_insert_price_list_rate_if_missing`. No equivalent found in Odoo's pricelist code. |
| 6 | **Item GL accounts / cost centres caller-supplied** | **(b) likely general** | **[inference]** Any ERP that accepts a full document in one call must let the caller name accounts, because legitimate integrations need to. Not verified in Odoo — `account_id` on `account.move.line` is computed with `readonly=False`-style semantics I did not confirm. |
| 7 | **`conversion_factor` → stock qty (28 instead of 40)** | **(a) Frappe-specific, structurally** | Odoo has **no per-line conversion factor to forge**; the factor is on `uom.uom` (`uom_uom.py:59-62`) and quantity is computed through it (`stock_move.py:278-282`). This is immunity by data model, not by a guard. **[code]** |
| 8 | **`weight_per_unit` in `force_item_fields` yet circular** | **(a) Frappe-specific** | `force_item_fields` is a Frappe/ERPNext construct; the circularity is a bug in that specific list. |

**Score: 5 of 8 are Frappe-specific, 2 likely general, 1 undetermined.**

### What survives as a general claim

Two things, and they are narrower than "agent-driven ERP is broken":

1. **The set-if-absent rule is not unique to Frappe.** Odoo's `_add_precomputed_values` does exactly
   the same thing at `models.py:4794` (`if fname not in vals`). What differs is which fields opt in.
   **The general defect is the absence of a signal, not the presence of the rule** — neither system
   tells a caller "you supplied a value we would otherwise have derived, and it differs." That claim
   holds for both vendors and is the one worth making.
2. **A manually-supplied price is deliberately honoured by at least two independent vendors.** So
   "the API accepted a wrong price" is not by itself a defect claim anywhere. The README already
   concedes this for ERPNext; it now has cross-vendor support.

### What does not survive

**"Agent-driven ERP launders exchange rates."** Odoo does not. This was our strongest single number
and it is vendor-specific. It remains an excellent finding about **ERPNext** — arguably a better one
now, because "Odoo's ORM defaults make this impossible and Frappe's make it easy" is a sharper,
more checkable statement than "ERPs are bad at this."

---

## 6. Not verified — read this before quoting anything above

1. **I did not run Odoo.** No instance, no XML-RPC call, no `create()` executed. Everything in §1–2
   is reading. The single most valuable next step is a live Odoo container and one RPC call
   supplying `currency_rate` and `price_unit` on a `sale.order` — perhaps two hours, and it would
   convert the central claim of this document from code-read to measured.
2. **Odoo `write()` semantics for `currency_rate`.** §1.5: `readonly` is not checked on write.
   A caller may well be able to set the FX rate on an existing order. Until tested, say "discarded
   at create", never "Odoo protects the FX rate".
3. **The circular half of finding #3 in Odoo** — whether Odoo's pricelist computation reads back a
   caller-supplied price. Not traced.
4. **Odoo's account/analytic account defaults (finding #6)** — asserted by inference only.
5. **NetSuite `rate` on create.** Undetermined; the Connector page is not evidence about SuiteTalk.
6. **SAP entirely.** Blocked on SPA rendering; needs the EDMX metadata behind a login.
7. **Odoo 13/14 history** — the claim that Odoo migrated pricing out of `onchange` is inference from
   the current tree's shape, not from reading old versions.
8. **Version scope.** Odoo 17.0 only. Odoo 18 changed parts of the sale/account stack; I did not check.
9. **No writes were made to the ERPNext instance for this task**, per instruction. The only
   filesystem change is the `vendor/odoo` clone (1.1 GB on disk) and this file.

## Sources

- [Sales Order — NetSuite Applications Suite](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159665260887.html)
- [Ensuring That NetSuite and Storefront Order Totals Are Matching](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162563576130.html)
- [Sales Order (A2X, OData V2) — SAP Help Portal](https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/19d48293097f4a2589433856b034dfa5/00d244581efca007e10000000a441470.html) (shell only)
- [Item Pricing Element — SAP Help Portal](https://help.sap.com/docs/SAP_S4HANA_CLOUD/03c04db2a7434731b7fe21dca77440da/e904310f517a477ebdbb9d45fdacf3cb.html) (shell only)
- Odoo 17.0 source, `vendor/odoo` @ `aa89dc2d`
