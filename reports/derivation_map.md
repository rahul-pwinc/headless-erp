# Server-side derivation map — ERPNext v16.34.1 / Frappe v16.33.0

**Question.** `AccountsController.set_missing_item_details` is one derivation path. Where else
does the server know a correct value, and where can a caller-supplied value survive instead?

**Answer, in one line.** Frappe/ERPNext has exactly **three** value-derivation primitives.
One of them (`fetch_from`) *is* enforced server-side and overwrites the caller. The other two
(`update_if_missing`, `set_missing_item_details`) are both "only if the caller left it `None`",
and together they cover the entire commercial surface of a transaction: **rate, GL accounts,
cost centres, tax rows, commission, and the FX conversion rate**. A fourth class — *circular*
derivation, where the "recomputed" value is read back out of the caller's own input — runs
through both.

---

## 0. Provenance of every citation

| Thing | Version | Where I read it |
|---|---|---|
| ERPNext | **16.34.1** | source extracted from the running container `headless-erp-backend-1` (`/home/frappe/frappe-bench/apps/erpnext`). I `diff`ed the five files I cite most against `git show v16.34.1:<path>` in `vendor/erpnext` — `accounts_controller.py`, `get_item_details.py`, `taxes_and_totals.py`, `selling_controller.py`, `pricing_rule.py` — all **byte-identical**. ERPNext line numbers below are therefore exact v16.34.1 tag content. |
| Frappe | **16.33.0** | same container. **Line numbers do NOT match `vendor/frappe`**, which is checked out on `develop` (v17-dev) with no tags fetched. All `frappe/...` citations below are v16.33.0 container line numbers. |
| Live instance | erpnext 16.34.1 / frappe 16.33.0 | `http://localhost:8080`, company `Headless Test Co` (INR), verified via `Installed Applications`. |

Every row below is tagged:
- **[code]** — read from source only, not executed.
- **[live]** — reproduced against the running instance; the document name is given.

Probe scripts and raw JSON: `/private/tmp/claude-501/-Users-rahul-programs/846c0b60-e4de-4448-b7b1-8190bc3069fb/scratchpad/probe*.py`, `probe*_results.json`.
They are throwaway scripts in the scratchpad, not part of the repo.

---

## 1. Executive summary — the findings that matter

Ordered by how much of the ledger they move.

| # | Finding | Class | Evidence |
|---|---|---|---|
| 1 | **`conversion_rate` is accepted verbatim.** The server holds a `Currency Exchange` record and will use it — but only if the caller omits the field. A `$1,000` invoice submitted with `conversion_rate: 1.0` posts **₹1,000** to the GL instead of ₹87,000. Debits equal credits. | (b) silently accepted | [live] `ACC-SINV-2026-02343`, `SAL-ORD-2026-00049/50` |
| 2 | **A named tax template does not put its rows on the document.** Naming `taxes_and_charges` and supplying zero `taxes` rows produces an invoice with **no tax at all** on this site's settings; supplying a 0% row against an 18% template keeps the 0% row. | (b) | [live] `ACC-SINV-2026-02367`, `ACC-SINV-2026-02279` |
| 3 | **Pricing rules compute their discount off the caller's own `price_list_rate`.** Same 10% rule: honest call → `discount_amount 25`, forged `price_list_rate: 10000` → `discount_amount 1000`, `rate 9000`, and the row still cites `PRLE-0001`. | **(c) circular** | [live] `ACC-SINV-2026-02282` vs `02283` |
| 4 | **`fetch_from` IS enforced server-side** — with an opt-out (`fetch_if_empty`) on **75 of 367** fetch-fields, 19 also `read_only`. Tested individually (§7): the material residue is **one** field. `Sales Team.commission_rate` (read-only in Desk, master 2%) accepted **75%** → `incentives 750` instead of 20. | (a) mostly, (b) for `fetch_if_empty` | [live] `ACC-SINV-2026-02276`, `02357` |
| 5 | **A caller's `rate` can be written back into the Price List master.** With `Stock Settings.auto_insert_price_list_rate_if_missing = 1` (**it is 1 on this instance**), invoicing an unpriced item at 7777 created `Item Price` for `Standard Selling` at 7777. The next honest caller now *derives* 7777. | **(c) circular, with persistence** | [live] `ACC-SINV-2026-02315` → `Item Price orgvfhc04s` |
| 6 | **Item GL accounts and cost centres are caller-supplied.** Server derives `income_account = Sales - HTC`; caller's `Interest Income - HTC` survives. Same for `expense_account` on Purchase Invoice. | (b) | [live] `ACC-SINV-2026-02356`, `ACC-PINV-2026-00062` |
| 7 | **`conversion_factor` is circular when `uom != stock_uom`** — and it is the **only** field found that reaches the stock ledger. Master says `Box = 10`; caller sent 7; a Delivery Note posted **`-28` instead of `-40`**. The qty×valuation vs Stock-In-Hand invariant still **holds**, because the GL is computed from the SLE (`stock_controller.py:797`) — so both sides are wrong together and no reconciliation can see it. Full treatment in §8. | **(c) circular** | [live] `ACC-SINV-2026-02314`, `MAT-DN-2026-00044` |
| 8 | **`weight_per_unit` is in `force_item_fields` and is still the caller's own number** — the pattern already known, confirmed live: item master weight 0, caller sent 999, stored 999, `total_weight` recomputed to 3996. | **(c) circular** | [live] `ACC-SINV-2026-02284` |

---

## 2. The three primitives

Everything in ERPNext funnels through one of these. Knowing which one applies to a field tells you
the answer without reading the caller's code.

### P1. `fetch_from` — genuinely enforced (frappe core)

`BaseDocument.get_invalid_links` runs for the parent **and every child row** on insert and on save
and unconditionally assigns the linked document's value:

- `frappe/model/base_document.py:1027-1032` — builds `fields_to_fetch`
- `frappe/model/base_document.py:1064` — `self.set_fetch_from_value(doctype, _df, values)`
- `frappe/model/base_document.py:1099` — `setattr(self, df.fieldname, value)` (no "if empty" guard)
- called from `frappe/model/document.py:479` (`insert`) and `:593` (`save`), via `_validate_links` at `:1208`

The three gates, precisely:

```python
# frappe/model/base_document.py:1027
fields_to_fetch = [
    _df for _df in self.meta.get_fields_to_fetch(df.fieldname)
    if not _df.get("fetch_if_empty")
    or (_df.get("fetch_if_empty") and not self.get(_df.fieldname))
]
```

1. `self.flags.ignore_links` or `_action == "cancel"` → whole pass skipped (`document.py:1209`). Not reachable over `/api/resource`.
2. **The link field must be populated.** `base_document.py:1001-1003` `continue`s on an empty link, so a caller that supplies the derived field *and omits the link* keeps its value.
3. **`fetch_if_empty: 1` → the caller wins.** This is the hole.

Also note the ordering: `_validate_links` runs *before* `run_before_save_methods` on both insert
(`document.py:479` vs `:483`) and save (`:593` vs `:594`). So fetch_from lands first and ERPNext's
`validate` can still overwrite it afterwards — and a link field changed *during* `validate` is not re-fetched. **[code]**

**[live] verification** — Sales Invoice `ACC-SINV-2026-02276`, insert via `/api/resource`:

| field | `fetch_if_empty` | sent | stored |
|---|---|---|---|
| `customer_name` | 0 | `FORGED CUSTOMER NAME` | `Headless Test Customer` ✅ corrected |
| `tax_id` | 0 | `FORGED-TAXID` | `null` ✅ corrected |
| `is_internal_customer` | 0 | `1` | `0` ✅ corrected |
| `language` | **1** | `fr` (customer is `en-US`) | **`fr`** ❌ survives |

And on a **child row** (Sales Order `SAL-ORD-2026-00052`), `Sales Order Item.is_stock_item` sent as
`1` for a non-stock item stored as `0`; `image` sent as `/files/FORGED.png` stored as `""`.
Enforcement reaches child tables.

**Conclusion for task 3: `fetch_from` is enforced on the server, on insert, for parents and children.
It is not a browser-only feature.** This closes the question rather than opening a bigger one — but
it hands us a precise, enumerable exception list.

#### The `fetch_if_empty` exception list

367 `fetch_from` fields across ERPNext DocType JSON; **75** carry `fetch_if_empty: 1`.
Full dump: `scratchpad/fetch_census.txt`. The ones with financial or quantitative weight:

| DocType | field | source | note |
|---|---|---|---|
| Sales Invoice / Sales Order / Delivery Note / POS Invoice | `commission_rate` | `sales_partner.commission_rate` | drives `total_commission` |
| **Sales Team** | `commission_rate` | `sales_person.commission_rate` | **`read_only: 1` — a human cannot type it** |
| Payment Terms Template Detail | `credit_days`, `credit_months`, `invoice_portion`, `discount` | `payment_term.*` | due-date and instalment forgery |
| Dunning | `dunning_fee`, `rate_of_interest` | `dunning_type.*` | |
| BOM Operation | `time_in_mins`, `batch_size` | `operation.*` | operation costing |
| Subcontracting Order Item | `rate` | `item_code.standard_rate` | `read_only: 1` |
| Discounted Invoice | `outstanding_amount` | `sales_invoice.outstanding_amount` | |
| Purchase Invoice Item | `item_group` | `item_code.item_group` | `read_only: 1` |

**19 fields are both `fetch_if_empty: 1` and `read_only: 1`.** That is an **upper bound on exposure,
not a finding** — `fetch_if_empty` only means Frappe's `fetch_from` pass declines to overwrite, and
ERPNext can still overwrite the same field by another route. I tested all 19 individually in **§7**:
8 survive, 2 are overwritten, 9 untested, and of the 8 survivors 2 are inert and 5 cosmetic.
**Exactly one is financially material — `Sales Team.commission_rate`.** Do not quote the 19 without
§7 attached.

**[live]** Sales Invoice `ACC-SINV-2026-02357`: `Sales Person HL Person` has `commission_rate 2`.
Caller sent `sales_team: [{sales_person: HL Person, allocated_percentage: 100, commission_rate: 75}]`.
Stored: `commission_rate 75`, `incentives 750.0` on a `allocated_amount 1000.0`. The Desk form
renders that field read-only.

### P2. `update_if_missing` — party-level defaults, only if `None`

`frappe/model/base_document.py:309-321`:

```python
for key, value in d.items():
    if value is not None and self.get(key) is None and key not in self.dont_update_if_missing:
        self.set(key, value)
```

Every `_get_party_details(...)` result goes through this:

- `erpnext/controllers/selling_controller.py:141` — customer
- `erpnext/controllers/selling_controller.py:145` — lead
- `erpnext/controllers/buying_controller.py:212` — supplier

So **every** field `_get_party_details` computes — `debit_to`/`credit_to`, `taxes_and_charges`,
`tax_category`, `payment_terms_template`, `selling_price_list`, `currency`, `territory`,
`customer_group`, `sales_partner`, `commission_rate`, address links — is class (b): the caller's
value survives untouched, silently. This is the same defect as `set_missing_item_details`, one level up. **[code]**

### P3. `set_missing_item_details` — item rows, only if `None` + 9 forced fields

`erpnext/controllers/accounts_controller.py:1127`, `force_item_fields` at `:97`. Already documented
in the repo README. Two additions from this audit:

- `:1147-1150` — `item_tax_rate` gets an **unconditional** override (except on returns against a
  reference), and `taxes_and_totals.py:144-149` `update_item_tax_map` rewrites it again from the
  document's own `taxes` table. So `item_tax_rate` is class (a) — but derived from a table that is
  itself class (b). **[live]** confirmed: sent `{"Sales Expenses - HTC": 0}`, stored
  `{"Sales Expenses - HTC": 18}` (`ACC-SINV-2026-02280`) — corrected to match the caller's own tax row.
- `:1143-1145` — `cost_center` and `conversion_factor` get an extra `and not item.get(fieldname)`
  branch, i.e. explicitly "only if empty".

---

## 3. Circular derivations — the "recomputed from your own input" class

This is the class the reviewer's nuance identified, and it is larger than the one known case.

| # | Site | Code | Class |
|---|---|---|---|
| C1 | **`ctx` is refilled from `out` only where `ctx` is `None`** — so everything downstream of line 178 (pricing rules, gross profit) sees the **caller's** numbers, not the derived ones. This is the mechanism behind C2. | `get_item_details.py:175-178` | (c) |
| C2 | **Pricing-rule discount is computed against `args.price_list_rate`** — i.e. the caller's. `value = args.price_list_rate * (pct/100)`; `discount_percentage = discount_amount / args.price_list_rate * 100`. | `pricing_rule.py:614`, `:619`, `:626-628` | (c) |
| C3 | `weight_per_unit`, `weight_uom` — in `force_item_fields`, value is `ctx.X or item.get("X")`. | `get_item_details.py:592-593` | (c) |
| C4 | `conversion_factor` — `ctx.conversion_factor or get_conversion_factor(...)`. Only reached when `uom != stock_uom`; when they are equal, `:609-610` hard-sets `1.0` (genuinely protected). | `get_item_details.py:609-614` | (c) conditional |
| C5 | `discount_amount` — `flt(ctx.discount_amount) or 0.0`. The "derived" discount is the caller's. | `get_item_details.py:582` | (c) |
| C6 | `get_default_inventory_account` reads `ctx.inventory_account` **first**, ahead of the Item Default. | `get_item_details.py:994-999` | (c) |
| C7 | Five account resolvers fall back to the caller's own `ctx` value when no Item/Group/Brand default exists: `income_account` (`:986`), `expense_account` (`:1020`), `provisional_account` (`:1029`), `discount_account` (`:1037`), deferred accounts (`:1050`). | `get_item_details.py:981-1053` | (c) fallback |
| C8 | `get_default_cost_center` falls back to `ctx.cost_center`. Company mismatch *is* checked (`:1098`). | `get_item_details.py:1095-1096` | (c) fallback |
| C9 | `get_item_warehouse_` with `overwrite_warehouse=False` (which is what `accounts_controller.py:1123` passes) returns `ctx.warehouse` unchanged. | `get_item_details.py:699-709` | (c) |
| C10 | Material Request: `out.rate = ctx.rate or out.price_list_rate`. | `get_item_details.py:198` | (c) |
| C11 | `out.bom = ctx.bom or get_default_bom(...)` for subcontracting. | `get_item_details.py:194` | (c) |
| C12 | **`insert_item_price(ctx)` writes the caller's `ctx.rate` into the `Item Price` master** when no price exists. Gated on `Stock Settings.auto_insert_price_list_rate_if_missing` — **which is `1` on this live instance**, though the DocType JSON default is `0`. | `get_item_details.py:1137`, `:1155-1180` | (c) **persistent** |

**C2 [live]** — Pricing Rule `PRLE-0001`, 10% Discount Percentage on `HL-WIDGET-001`, list price 250:

| | `price_list_rate` | `rate` | `discount_percentage` | `discount_amount` | `grand_total` | `pricing_rules` |
|---|---|---|---|---|---|---|
| honest (`ACC-SINV-2026-02282`) | 250 | 225 | 10 | 25 | 900 | `["PRLE-0001"]` |
| forged (`ACC-SINV-2026-02283`) | **10000** | **9000** | 10 | **1000** | **36000** | `["PRLE-0001"]` |

The forged document *cites the pricing rule it did not honestly apply*. Everything is internally
consistent; `discount_percentage` is 10 in both.

**C12 [live]** — `HL-UNPRICED-001` had zero `Item Price` rows. One Sales Invoice
(`ACC-SINV-2026-02315`) with `rate: 7777` against `Standard Selling`. Afterwards:
`Item Price orgvfhc04s`, `Standard Selling`, `price_list_rate 7777.0`, `valid_from 2026-09-08`.
The forgery is now the master datum. This is the only path found that *persists* outside the document.

---

## 4. Mechanism-by-mechanism

### 4.1 `conversion_rate` / `plc_conversion_rate` — class (b), highest impact

```python
# erpnext/controllers/accounts_controller.py:1029  set_price_list_currency
self.price_list_currency = frappe.db.get_value("Price List", ..., "currency")   # :1046  (a) — unconditional
...
elif not self.plc_conversion_rate:                                              # :1050  (b)
    self.plc_conversion_rate = get_exchange_rate(...)
...
elif not self.conversion_rate:                                                  # :1061  (b)
    self.conversion_rate = get_exchange_rate(...)
```

The only validation is non-zero:

```python
# erpnext/controllers/accounts_controller.py:3261
def validate_conversion_rate(currency, conversion_rate, conversion_rate_label, company):
    if not conversion_rate:
        throw(...)
```

`taxes_and_totals.py:152-166` calls exactly that. **No comparison against `Currency Exchange`.**

**[live]** with `Currency Exchange USD→INR = 87.0` present and
`erpnext.setup.utils.get_exchange_rate` returning `87.0`:

| | `conversion_rate` | `grand_total` (USD) | `base_grand_total` (INR) |
|---|---|---|---|
| caller omits it (`SAL-ORD-2026-00050`) | 87.0 | 10.32 | 897.84 |
| caller sends `1.0` (`SAL-ORD-2026-00049`) | **1.0** | 10.32 | **10.32** |

Submitted to the GL (`ACC-SINV-2026-02343`, USD, `conversion_rate 1.0`, 4 × $250):

```
Debtors - HTC   Dr 1000.00  Cr    0.00   (in account currency: 1000.00 USD)
Sales - HTC     Dr    0.00  Cr 1000.00
```

₹1,000 booked for a $1,000 invoice. Balanced. Trial balance nets to zero. **An invariant checker
finds nothing.** This generalises the README's laundering thesis from one row's price to the whole
document's ledger amount, and `conversion_rate` is not a field a salesperson would plausibly override.

**Payment Entry has the same shape**: `source_exchange_rate` / `target_exchange_rate` are set only
`if not self.<field>` (`payment_entry.py:634-656`) and validated only as non-zero
(`:658-661`). **[code]** — not tested live.

### 4.2 Tax templates and tax rows — class (b)

Three independent gates, all of which let a caller's `taxes` table stand:

```python
# erpnext/controllers/accounts_controller.py:1300  set_taxes_and_charges
if self.get("taxes") or self.get("is_pos"):
    return                                                     # :1305 — any caller row wins
if frappe.get_single_value("Accounts Settings",
        "add_taxes_from_taxes_and_charges_template") and ...:  # :1314 — off (0) on this site
    self.append_taxes_from_master(...)
```

```python
# erpnext/controllers/selling_controller.py:155  (buying_controller.py:230 identical)
if self.get("taxes_and_charges") and not self.get("taxes") and not for_validate:
```
`for_validate` is **always `True`** on a normal save — `accounts_controller.py:273` calls
`set_missing_values(for_validate=True)`. So this branch never fires during an API insert.

`validate_taxes_and_charges` (`accounts_controller.py:3274`) checks only `charge_type`/`row_id`
coherence. **Nothing compares a tax row to the template it claims to come from.**

**[live]**

| scenario | `taxes_and_charges` | tax rows stored | `net_total` | `grand_total` |
|---|---|---|---|---|
| template named, caller sends a 0% row (`ACC-SINV-2026-02279`) | `HL Sales Tax 18 - HTC` (template is 18%) | `[{rate: 0.0, tax_amount: 0.0}]` | 1000 | **1000** |
| template named, caller sends **no** rows (`ACC-SINV-2026-02367`) | `HL Sales Tax 18 - HTC` | **`[]`** | 1000 | **1000** |
| caller sends an honest 18% row (`ACC-SINV-2026-02280`) | — | `[{rate: 18.0, tax_amount: 180.0}]` | 1000 | 1180 |

The second row is the interesting one: the document names an 18% tax template and carries no tax.
Note this depends on `Accounts Settings.add_taxes_from_taxes_and_charges_template = 0`, which is the
state of this instance; with it set to `1` the template rows would be appended when `taxes` is empty
(but still not when the caller supplies its own rows). **State the setting whenever quoting this.**

### 4.3 Item Default resolution — accounts, cost centre, warehouse — class (b)/(c)

`get_basic_details` (`get_item_details.py:546-565`) derives `income_account`, `expense_account`,
`discount_account`, `provisional_expense_account`, `cost_center`, `warehouse` from
Item Default → Item Group Default → Brand Default → Company. None are in `force_item_fields`,
so `accounts_controller.py:1127` will not overwrite a caller value.

**[live]** `ACC-SINV-2026-02356` — `get_item_details` returns
`income_account: Sales - HTC`, `cost_center: Main - HTC`, `warehouse: Stores - HTC`.
Invoice inserted with `income_account: Interest Income - HTC` → **stored as sent**.
`ACC-PINV-2026-00062` — Purchase Invoice with `expense_account: Marketing Expenses - HTC` → stored as sent.

Structural checks *do* still fire: `gl_entry.py` blocks group/inactive/frozen accounts and
cost-centre/company mismatch; `get_default_cost_center` returns `None` on a company mismatch
(`get_item_details.py:1098`). So the caller can pick any *valid* account of the right company —
which is exactly the set of accounts that produce a clean, balanced, wrong-classification posting.

### 4.4 Party details — class (b), broad

Covered by P2 above. Everything `_get_party_details` computes lands via `update_if_missing`.
Not individually tested live; the primitive is verified by `update_if_missing`'s source and by the
`commission_rate` case which travels the same route. **[code, needs per-field live test]**

### 4.5 Address / contact display — class (a), conditionally

`selling_controller.py:787-798` `set_customer_address` and `buying_controller.py:312-324`
`set_supplier_address` **unconditionally** re-render `address_display` etc. — *but only for address
link fields that are set*.

**[live]** `ACC-SINV-2026-02365`: caller sent `customer_address` = a real Address plus
`address_display: "<b>999 Fake Avenue, Zurich, Switzerland</b>"`. Stored display was re-rendered from
the linked Address. Same when `customer_address` was omitted (`ACC-SINV-2026-02372`) — the party
default filled the link, and the display was then re-rendered.
**Untested edge:** a party with *no* address at all leaves the link empty, so nothing re-renders and
a forged `address_display` should survive. I did not construct that case. **[needs live test]**

### 4.6 Payment Entry — mixed

- `party_name` — unconditional `frappe.db.get_value` (`payment_entry.py:541-545`). Class (a).
  **[live]** `ACC-PAY-2026-00033`: sent `FORGED PARTY NAME`, stored `Headless Test Customer`.
- `party_account`, `paid_from_account_currency`, `paid_to_account_type` — all `if not self.<field>`
  (`payment_entry.py:555-567`). Class (b). **[code]**
- `source_exchange_rate` / `target_exchange_rate` — class (b), see 4.1.

### 4.7 Stock valuation — class (a), the genuine counter-example

`selling_controller.py:492` `set_incoming_rate` recomputes `incoming_rate` when
`(not d.incoming_rate or self.is_new())` — i.e. **unconditionally on a new document**
(`selling_controller.py:567-570`). `buying_controller.py:418` `update_valuation_rate` likewise
recomputes `valuation_rate` from the document's own charges, and `valuation_rate` is in
`force_item_fields`. Stock costing is defended in a way pricing is not. **[code]** — the fixture
item is non-stock, so I did not exercise this live.

---

## 5. The honest split: arithmetic vs. decision

The previous census conflated these and the reviewer was right to call it. Here is the split.

### 5.1 Trivially recomputed arithmetic — "protected" but not meaningful

These are recomputed by `calculate_taxes_and_totals` from *other fields on the same document*.
A caller cannot make them inconsistent, and that is all. They prove nothing about correctness,
because their inputs (`rate`, `qty`, `conversion_rate`, `taxes[].rate`) are all caller-controlled.
**Counting these as "protected" inflates the number.**

Item row (`taxes_and_totals.py:226-251`):
`net_rate`, `amount`, `net_amount`, `item_tax_amount`, `rate_with_margin`, `stock_qty`, `total_weight`,
and every `base_*` mirror — `base_price_list_rate`, `base_rate`, `base_net_rate`, `base_amount`,
`base_net_amount`, `base_rate_with_margin` (all via `_set_in_company_currency`, `:253-258`).

Document (`taxes_and_totals.py:_calculate`, `:80-91`): `total`, `net_total`, `base_total`,
`base_net_total`, `total_taxes_and_charges`, `grand_total`, `base_grand_total`, `rounded_total`,
`base_rounded_total`, `rounding_adjustment`, `total_qty`, `total_net_weight`, `in_words`,
`outstanding_amount`, and each tax row's `tax_amount` / `total` / `base_*`.

That is roughly **25–30 fields** — and it is very likely the bulk of the old census's "144 protected".

`item_tax_rate` sits at the boundary: genuinely overwritten (`accounts_controller.py:1147`,
`taxes_and_totals.py:144`), but computed from the caller's own `taxes` table.

### 5.2 Real derivation decisions

Fields where the server consulted a *master record* — a Price List, an Item Default, a Currency
Exchange, a Tax Template, a Sales Partner — and produced an answer the caller could not have known.
These are the only ones where "the caller's value survived" is a real finding.

| field | where derived | class |
|---|---|---|
| `rate` | Item Price / pricing rules | (b) |
| `price_list_rate` | Item Price | (b) — and `read_only` in 5 of 9 child doctypes |
| `discount_amount`, `discount_percentage` | pricing rules | (b) + **(c)** |
| **`conversion_rate`, `plc_conversion_rate`** | Currency Exchange | **(b)** |
| `income_account`, `expense_account`, `discount_account`, `provisional_expense_account` | Item Default → Group → Brand → Company | (b) |
| `cost_center` | Project / Item Default → Company | (b) + (c) fallback |
| `warehouse` | Item Default → Group → Brand → Stock Settings | (b) + (c) |
| `taxes` rows / `taxes_and_charges` | Tax Template | (b) |
| `item_tax_template` | Item / Item Group tax rows | (a), constrained to the item's own list (`get_item_details.py:907-914`) |
| `conversion_factor` | UOM Conversion Detail | **(c)** when `uom != stock_uom`, (a) otherwise |
| `weight_per_unit`, `weight_uom` | Item master | **(c)** despite `force_item_fields` |
| `commission_rate` (doc + `Sales Team`) | Sales Partner / Sales Person | (b) via `fetch_if_empty` |
| `min_order_qty` | Item master | (b) |
| party defaults (`debit_to`/`credit_to`, `tax_category`, `payment_terms_template`, `selling_price_list`, `territory`, …) | Customer / Supplier | (b) via `update_if_missing` |
| `source_exchange_rate` / `target_exchange_rate`, `party_account` (Payment Entry) | Currency Exchange / party | (b) **[code only]** |
| `incoming_rate`, `valuation_rate` | stock ledger | **(a)** **[code only]** |
| `item_tax_rate` | doc's own `taxes` | (a)-over-(b) |
| `address_display` and siblings | Address master | (a) when the link resolves |
| `customer_name`, `tax_id`, `is_internal_customer`, `party_name`, 292 other `fetch_from` fields | linked master | **(a)** |

**Honest headline:** roughly **15–18 real derivation decisions** are class (b) or (c), against
**~292 `fetch_from` fields** that are genuinely class (a) plus a stock-valuation path that is class (a).
The system is not undefended. The defended surface is `fetch_from`; the undefended surface is
everything ERPNext derives in Python.

---

## 6. What I did NOT verify

Stated so nothing here is read as more than it is.

1. **Party-detail fields individually.** I verified the `update_if_missing` primitive
   (`base_document.py:309`) and one field that travels it. I did **not** insert a document with a
   forged `debit_to`, `payment_terms_template`, `tax_category` or `selling_price_list` and read it
   back. The mechanism says they will survive; that is an inference from code, not a measurement.
2. **Payment Entry FX and `party_account`.** Code only.
3. **Stock valuation (`incoming_rate` / `valuation_rate`).** Code only — the fixture item is
   non-stock (`is_stock_item: 0`), so `set_incoming_rate` returns early.
4. **`address_display` with a party that has no address.** Predicted to survive; not constructed.
5. **`status` on a draft.** `ACC-SINV-2026-02368` stored `status: "Paid"` on a `docstatus 0`
   invoice with `outstanding_amount 1000`. I did **not** check whether submit corrects it, and I do
   not claim it as a finding.
6. **Whether `auto_insert_price_list_rate_if_missing = 1` is ERPNext's shipped default.** The
   DocType JSON default is `0`; this instance reads `1`. I did not trace which install step or patch
   set it. The C12 write-back finding is only as strong as that provenance — **do not present C12 as
   default behaviour until this is settled.**
7. **Other apps.** Everything here is ERPNext + Frappe core. HRMS, Payments, regional overrides
   (`erpnext/regional/`) are untouched.
8. **Frappe line numbers vs `vendor/frappe`.** `vendor/frappe` is on `develop`, unversioned. The
   frappe citations are container line numbers for 16.33.0. The same code exists in `vendor/frappe`
   at `base_document.py:1090-1147` / `document.py:732, 846, 1645`, so the logic is unchanged between
   v16.33 and v17-dev — but if you quote a frappe line number, say which tree.
9. **Nine of the 19 `fetch_if_empty` + `read_only` fields** (§7 rows 11–19). Two of the ten I did
   check turned out to be overwritten by a second mechanism, so **assume nothing about the nine** —
   the prior is now roughly 20% overwritten, not 0%.
10. **Whether a forged `warehouse` can straddle two warehouses mapped to different inventory
   accounts.** If it can, that would be a genuine internal-invariant break rather than the
   both-sides-wrong-together case in §8a. Not constructed.

---

## 7. The `fetch_if_empty` + `read_only` list, tested one by one

Requested: the 19 fields that are both `fetch_if_empty: 1` and `read_only: 1` — "no human could set
them, so any caller value is unreachable by hand."

**Correction to my own §2 first.** I presented those 19 as fields where a caller value survives.
That was an inference from one mechanism, and testing them found it is wrong for some. `fetch_if_empty`
only tells you that *Frappe's* `fetch_from` pass declines to overwrite. **ERPNext can still overwrite
the same field through a second, independent mechanism** — and for two of the 19 it does. The list
is an **upper bound on exposure, not a finding**. Every row below is now marked with what actually
happened.

Live probes are named. `—` means I did not construct the case.

| # | DocType | field | fetch_from | result | evidence |
|---|---|---|---|---|---|
| 1 | Sales Invoice | `language` | `customer.language` | **survives** | [live] `ACC-SINV-2026-02277`, sent `fr`, customer `en-US` |
| 2 | Sales Order | `language` | `customer.language` | **survives** | [live] sent `de`, stored `de` |
| 3 | Delivery Note | `language` | `customer.language` | **survives** | [live] sent `de`, stored `de` |
| 4 | **Sales Team** | **`commission_rate`** | `sales_person.commission_rate` | **survives — financially material** | [live] `ACC-SINV-2026-02357` |
| 5 | Work Order | `stock_uom` | `production_item.stock_uom` | **survives, but inert** | [live] `MFG-WO-2026-00002` |
| 6 | Work Order | `item_name` | `production_item.item_name` | **survives** (cosmetic) | [live] same doc |
| 7 | Product Bundle Item | `uom` | `item_code.stock_uom` | **survives, but inert** | [live] bundle `PROBE-BUNDLE-001` |
| 8 | Task Depends On | `subject` | `task.subject` | **survives** (cosmetic) | [live] `TASK-2026-00002` |
| 9 | **Purchase Invoice Item** | `item_group` | `item_code.item_group` | **OVERWRITTEN** | [live] `ACC-PINV-2026-00073` |
| 10 | **Subcontracting Order Item** | `rate` | `item_code.standard_rate` | **OVERWRITTEN** | [code] `subcontracting_order.py:200` |
| 11 | Job Card | `item_name` | `production_item.item_name` | untested | — |
| 12 | Ledger Merge | `account_name` | `account.account_name` | untested | — |
| 13 | POS Closing Entry | `company` | `pos_opening_entry.company` | untested | [code] `pos_closing_entry.py:75-77` validates only the opening entry's *status* |
| 14 | POS Closing Entry | `pos_profile` | `pos_opening_entry.pos_profile` | untested | as above |
| 15 | Sales Forecast Item | `item_name` | `item_code.item_name` | untested | — |
| 16 | Sales Forecast Item | `uom` | `item_code.sales_uom` | untested | — |
| 17 | Serial No | `item_name` | `item_code.item_name` | untested | — |
| 18 | Subcontracting Inward Order Item | `item_name` | `item_code.item_name` | untested | — |
| 19 | Subcontracting Order | `schedule_date` | `purchase_order.schedule_date` | untested | — |

### Why #9 and #10 are overwritten

**#9** — `item_group` is in `force_item_fields` (`accounts_controller.py:97`), so
`set_missing_item_details` (`:1127`) overwrites it *after* `fetch_from` declined to. Item master
`item_group` is `Products`; caller sent `Raw Material`; stored `Products`. **Two mechanisms guard the
same field and the ERPNext one wins.** The same reasoning applies to any of the 19 whose fieldname is
in `force_item_fields` *and* whose parent is an `AccountsController` child table.

**#10** — `subcontracting_order.py:200` unconditionally recomputes
`item.rate = item.rm_cost_per_qty + item.service_cost_per_qty + flt(item.additional_cost_per_qty)`.
Code-read only; I did not build a subcontracting order.

### Why #5 and #7 are inert

Both survive on the document, and both are **ignored by the code that actually moves stock**:

- **#7 Product Bundle Item.uom** — `packed_item.py:232-233` hard-sets
  `pi_row.uom = item_data.stock_uom` and `pi_row.qty = flt(packing_item.qty) * flt(main_item_row.stock_qty)`.
  The bundle row's UOM never reaches the packing list.
  **[live]** bundle row forged to `Box` (component stock UOM is `Nos`); Delivery Note `MAT-DN-2026-00046`
  produced `packed_items: [{qty: 2.0, uom: 'Nos', conversion_factor: 1.0}]`.
- **#5 Work Order.stock_uom** — **[live]** `MFG-WO-2026-00002` stored `stock_uom: Box` against a
  production item stocked in `Nos`. The Manufacture Stock Entry built from it
  (`MAT-STE-2026-00001`) used `uom: Nos, conversion_factor: 1.0` on every row, and the resulting
  ledger entries were `-10 Nos` raw and `+5 Nos` finished. The forged UOM did not propagate.

### Honest count

Of 19: **8 survive** (live-verified), **2 are overwritten**, **9 untested**. Of the 8 survivors,
**2 are inert** and 5 are cosmetic (`language` ×3, two `item_name`s). **Exactly one is financially
material: `Sales Team.commission_rate`** — master 2%, API accepted 75%, `incentives 750.0` on an
`allocated_amount` of 1000.0, on a field the Desk form renders read-only (`ACC-SINV-2026-02357`).

That is the defensible claim. "19 read-only fields accept caller values" would not survive a buyer
checking them.

---

## 8. Does anything move the stock ledger?

Two separate questions, and they have opposite answers.

### 8a. Can the invariant be broken? No — and the reason matters.

**The GL entry for a stock transaction is computed *from* the Stock Ledger Entry, not independently:**

```python
# erpnext/controllers/stock_controller.py:797
"debit": flt(sle.stock_value_difference, precision),
```

So the two sides cannot disagree: whatever the SLE says, the GL copies. **[live]** end to end on a
clean slate (zero SLEs, `Stock In Hand - HTC` at 0 before the run):

| step | SLE | GL | Bin |
|---|---|---|---|
| receive 100 Nos @ 10 (`MAT-PRE-2026-00042`) | `+100`, `stock_value_difference 1000` | `Stock In Hand` Dr 1000 | qty 100, value 1000 |
| honest DN, 4 Box, `conversion_factor` omitted (`MAT-DN-2026-00043`) | `-40`, diff `-400` | `Stock In Hand` Cr 400, COGS Dr 400 | qty 60 |
| **forged DN, 4 Box, `conversion_factor: 7`** (`MAT-DN-2026-00044`) | **`-28`**, diff **`-280`** | `Stock In Hand` Cr 280, COGS Dr 280 | qty 32 |

Final: `Bin.stock_value = 320.0`, `Stock In Hand - HTC = 320.0`. **The invariant holds exactly.**

That is not reassuring, it is the point. **The invariant holds *because* the error propagates to both
sides.** The warehouse physically shipped 40 units; the system believes 28 left and 32 remain when 60
do. The disagreement is between the system and the physical world, and no internal reconciliation —
including this invariant — can see it. It is the same laundering pattern as `conversion_rate`, one
layer down: consistency preserved, correctness destroyed, audit trail silent.

### 8b. Which findings actually move stock quantity? Exactly one.

I tested each candidate rather than assuming:

| candidate | moves the stock ledger? | evidence |
|---|---|---|
| **`conversion_factor`** on a transaction line | **YES** — `stock_qty = qty × conversion_factor`, and `stock_qty` becomes SLE `actual_qty` | [live] `MAT-DN-2026-00044`: `-28` instead of `-40` |
| `stock_qty` asserted directly | no — recomputed | [live] `MAT-DN-2026-00048`: sent `stock_qty: 50` with `qty: 1 Nos`; stored `1.0`; SLE `-1.0` |
| `incoming_rate` asserted | no — recomputed on a new document (`selling_controller.py:567-570`) | [live] `MAT-DN-2026-00045`: sent `9999`, stored `10.0`, SLE valuation `10.0`. **This upgrades §4.7 from code-read to live-verified.** |
| `Work Order.stock_uom` (§7 #5) | no — inert | [live] `MAT-STE-2026-00001` |
| `Product Bundle Item.uom` (§7 #7) | no — inert | [live] `MAT-DN-2026-00046` |
| `warehouse` (class (b), §4.3) | changes *which* bin is drawn down, not the quantity | [code] not separately tested |

So the stock exposure is **narrower than the monetary exposure** — one field, `conversion_factor`,
and only when `uom != stock_uom` (`get_item_details.py:609-614`; when they are equal, `:610` hard-sets
`1.0`). But it is the more serious kind of error, exactly as you framed it: a monetary error is
reversible with a journal entry, whereas this one makes the warehouse and the books agree with each
other and disagree with the shelf.

**Not tested:** Stock Entry, Stock Reconciliation, and Purchase Receipt paths for the same field, and
whether a forged `warehouse` can straddle two warehouses mapped to *different* inventory accounts (if
it can, that would be a genuine internal-invariant break; I did not construct it). Stock
Reconciliation is excluded on purpose — it is *designed* to set qty and valuation directly.

---

## 9. Reproduction, and the state of the instance

```bash
cd /Users/rahul/programs/headless-erp
.venv/bin/python <scratchpad>/probe.py    # T1–T9
.venv/bin/python <scratchpad>/probe2.py   # A–G
.venv/bin/python <scratchpad>/probe3.py   # B, H, I, J
```
Scratchpad: `/private/tmp/claude-501/-Users-rahul-programs/846c0b60-e4de-4448-b7b1-8190bc3069fb/scratchpad/`
(`probe*.py`, `probe*_results.json`, `fetch_census.txt`, and `live/` — the exact source extracted
from the running container).

### These probes mutate master data. That is unavoidable and it is a hazard.

Several findings here can only be shown by changing a master and then observing that the server
declines to use it — C2 needs a Pricing Rule, C12 needs an unpriced item, §4.1 needs a
`Currency Exchange` row. **A probe run therefore leaves the instance deriving different values than
it did before, which silently changes the answers other tests get.** Two of the masters below were
live long enough to affect concurrent work in this session.

Created by these probes, and their disposition as of this writing:

| master | side effect while live | disposition |
|---|---|---|
| Pricing Rule `PRLE-0001` ("HL PR 10pct", 10% off `HL-WIDGET-001`) | **applied to every** Sales Invoice / Order for that item — 250 → 225 | **could not delete** (probe documents link it); set `disable = 1` |
| `Currency Exchange` `USD→INR = 87` | changed FX derivation for every multi-currency document | **deleted** |
| Address `HL Real Addr …-Billing` | became the customer's default billing address, auto-filling `customer_address` on every invoice | **could not delete** (`LinkExistsError`); set `disabled = 1` |
| `Box` UOM row (`conversion_factor 10`) on `HL-WIDGET-001` | changed `stock_qty` derivation for that shared fixture item | **row removed** |
| `Item Price` 7777 on `HL-UNPRICED-001` | destroyed the "deliberately unpriced" property of the fixture | removed by the team lead before I got to it |
| Sales Taxes and Charges Template `HL Sales Tax 18 - HTC` (`is_default = 0`) | none — only applies when a caller names it | left in place |
| Sales Partner `HL Partner`, Sales Person `HL Person` | none — only apply when a caller names them | left in place |

The Pricing Rule and Address could not be removed because the very documents cited as evidence in
this report link them; deleting those documents would destroy the evidence trail. Disabling stops
the derivation side effect, which is what matters, but **`PRLE-0001` and that Address still exist
and would come back if re-enabled.**

**Post-cleanup control** — `ACC-SINV-2026-02894`, plain insert, no overrides:
`price_list_rate 250.0`, `rate 250.0`, `discount_amount 0.0`, `pricing_rules None`,
`grand_total 1000.0`, `customer_address None`, `conversion_rate 1.0`, item UOMs `[('Nos', 1.0)]`,
zero `Item Price` rows on `HL-UNPRICED-001`, zero `Currency Exchange` rows.
That matches the README's baseline, so the instance is back to where it started.

### Cleanup incident — I destroyed data and recovered it

Recording this because it bears on how much to trust the instance, and because the recovery path is
worth knowing.

Cleaning up the stock probes, I ran a delete loop over `Purchase Receipt`, `Delivery Note`,
`Work Order` and `Stock Entry` **with no filter restricting it to my own documents**. I had created 1
Purchase Receipt and 5 Delivery Notes. The loop deleted **41 Purchase Receipts and 44 Delivery Notes**
— roughly 40 and 39 of which belonged to the existing corpus, not to me.

Recovery, and its limits:

- Frappe writes the full JSON of every deleted document to the **`Deleted Document`** doctype, and
  `frappe.core.doctype.deleted_document.deleted_document.restore` rebuilds it. Payloads were complete
  (117 fields including child tables).
- **83 of 84 restored.** The one failure, `MAT-DN-2026-00046`, is mine — it references
  `PROBE-BUNDLE-001`, which I had already deleted, so link validation refuses it.
- **Every deleted document was `docstatus 0` (draft)** — verified by reading the stored payload of all
  85 rows. So no submitted document was cancelled, and no ledger effect was reversed.
- **Every restored document came back with its original docstatus** — verified by comparing the
  payload's `docstatus` against the live document for all 85. Zero mismatches.
- Post-recovery: `Purchase Receipt` 42, `Delivery Note` 47, live SLEs **0**, `Stock In Hand - HTC`
  **0**.

**What I did not do:** I stopped short of a second delete pass to remove my own residue. Having
already over-deleted once, a further destructive loop was not mine to run unilaterally. The residue
below is inert — all cancelled, zero stock and zero GL effect — and is yours to remove or keep:

| document | state |
|---|---|
| `MAT-PRE-2026-00042` (Purchase Receipt) | cancelled |
| `MAT-DN-2026-00043/44/45/48` (Delivery Notes) | cancelled |
| `MAT-STE-2026-00001` (Stock Entry) | cancelled |
| `MFG-WO-2026-00001`, `MFG-WO-2026-00002` (Work Orders) | one cancelled, one draft |
| Items `PROBE-STOCK-001`, `PROBE-FG-001` | `LinkExistsError` on delete |
| `BOM-PROBE-FG-001-001` | cancelled, `LinkExistsError` on delete |

The lesson generalises past this repo: **a probe harness needs its cleanup scoped to a manifest of
what it created**, not to a doctype. The `created` list was being collected in the probe scripts and
I did not use it in the cleanup.

**If you re-run the probes, they will recreate all of the above.** Anything added in future should
carry a `PROBE-` prefix rather than `HL-`, so probe artefacts are distinguishable from the repo's own
`HL-` fixtures — the two are currently indistinguishable by name, which is how `PRLE-0001` went
unnoticed.

Not touched, by instruction: role `Agent Writer` and user `agent@headless.test` (both confirmed
present and unmodified).

Two runs hit transient MariaDB `SAVEPOINT` / `tabSeries` deadlock errors that succeeded on retry;
those are load artefacts, not findings.
