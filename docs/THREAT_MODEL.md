# Threat model

Written adversarially against this project's own control. Every bypass below is
real and most of them are open in the current implementation.

Evidence for anything asserted here is in [CLAIMS.md](CLAIMS.md).

## The one-line summary

**As shipped, this control is a convention, not a boundary.** `harness/intent.py`
runs in the caller's process. Any writer that does not import it is entirely
unconstrained. The control becomes a boundary only when the engine runs
server-side and the caller's ERP role is denied direct `create` on the
transaction doctypes. That deployment does not exist in this repo.

Read the rest of this document with that as the frame. Bypasses 1 and 2 are the
ones that matter; the others are refinements.

## What is being protected

Not the ledger. ERPNext defends the ledger perfectly well: it refuses
unbalanced vouchers (`general_ledger.py:397-427`), blocks postings to group,
inactive, and frozen accounts, and rejects company and cost-center mismatch.
Nothing here improves on that and nothing here needs to.

What is being protected is a single property of the record:

> For each field the server could have derived, the record shows whether the
> stored value came from the server's derivation or from the caller's assertion,
> and in the second case what the derived value would have been and why the
> caller deviated.

That property does not exist in ERPNext. Everything below is about how an
adversary or a careless writer destroys it.

## Actors

| Actor | Capability | Intent | Realistic |
|---|---|---|---|
| **Careless agent** | Full API write credentials, no malice, incomplete context. Holds a price from a stale cache, a PDF, or a prior conversation and posts it. | None | The primary case. This is what the product is for. |
| **Naive integration** | Same credentials, hand-written mapper, ships whatever the source system had in the price column. | None | Very common. Structurally identical to the careless agent. |
| **Prompt-injected agent** | Same credentials, instructions from a hostile document, invoice, or email the agent read. | Attacker's | Increasingly realistic and the agent has every credential it needs. |
| **Insider with API credentials** | Full API write credentials, knows the system, wants a specific record to read as legitimate. | Malicious | The hardest case, and largely out of scope. See below. |
| **Compromised MCP server** | Whatever the ERP user it authenticates as can do. | Attacker's | In scope only for what the ERP role permits. |

**Explicitly out of scope**: an attacker with database access, an attacker with
`Administrator`, an attacker who can install Frappe apps or edit Server Scripts,
and anyone who can change `Selling Settings`. All four can defeat everything
here trivially. If your threat model includes them, this product is not a
control you should count on.

## What the control stops

For a caller that goes through the engine, and only for that caller:

| Attempt | Result | Where |
|---|---|---|
| Send `price_list_rate` in a line, any value | Refused before any HTTP write | `harness/intent.py:179-187` |
| Send `discount_amount` or `discount_percentage` | Refused | same |
| Send `weight_per_unit` | Refused | same |
| Send `min_order_qty` on a Material Request | Refused (intent-level) | `intents/catalog.yaml:151-154` |
| Send `rate` inside the line body | Refused. `rate` is overridable, but only as a declared override, never smuggled into the payload | `harness/intent.py:179-187` |
| Declare a `rate` override with no reason, or whitespace | Refused | `harness/intent.py:200-201` |
| Declare an override of a field that is neither overridable nor refused (`net_amount`, and every other computed field) | Refused | `harness/intent.py:195-199` |
| Declare a `rate` override on a `return` | Refused. The intent-level refuse beats the default override | `intents/catalog.yaml:238-244`, `harness/intent.py:77-80` |
| Order an item for which no price is resolvable in any list | Refused. The engine will not guess a rate | `harness/intent.py:215-220` |
| Allocate a payment above an invoice's outstanding | Refused | `harness/intent.py:111-115` |
| Supply `outstanding` on a payment allocation instead of letting it be read | Refused | `harness/intent.py:106-109` |
| Allocate more in total than `paid_amount` | Refused | `harness/intent.py:123-126` |
| Override `rate` with a reason | **Allowed.** Derived value computed first, both values plus the reason recorded, and an invariant confirms ERPNext recorded the delta in the correct place | `harness/intent.py:239-245`, `:302-321` |

The last row is the design. The control does not prevent off-list pricing, which
is normal commerce. It prevents off-list pricing that does not say so.

## Bypasses

### Bypass 1: write directly to /api/resource

**Severity: total. Open.**

The engine is a client library. `POST /api/resource/Sales Invoice` with a
hand-built body goes nowhere near it. `frappe/api/v1.py:45-48` constructs the
document and inserts it. Nothing in the request path consults `intents/catalog.yaml`,
because nothing on the server knows it exists.

This is not the only such route. Every one of these is an independently
whitelisted write endpoint in stock Frappe:

| Endpoint | Location |
|---|---|
| `POST /api/resource/{doctype}` | `frappe/api/v1.py:47` |
| `PUT /api/resource/{doctype}/{name}` | `frappe/api/v1.py:53` |
| `frappe.client.insert` | `frappe/client.py:229` |
| `frappe.client.insert_many` | `frappe/client.py:239` |
| `frappe.client.save` | `frappe/client.py:252` |
| `frappe.client.submit` | `frappe/client.py:276` |
| `frappe.client.bulk_update` | `frappe/client.py:310` |
| `frappe.client.set_value` | `frappe/client.py:183` |
| `execute_doc_method` | `frappe/api/v1.py:118` |

Plus Data Import, the bench console, and any custom whitelisted method already
installed on the site.

**The only fix.** Frappe checks `create` permission inside `Document.insert()`
(`frappe/model/document.py:730`), and that check covers every route in the table
above because they all reach `insert()` or `save()`. So the boundary is
constructible:

1. Move the engine server-side, as a Frappe app exposing whitelisted intent
   methods (`bill`, `collect`, and so on).
2. Give the agent's ERP user a role with **no** `create` or `write` permission on
   Sales Invoice, Purchase Invoice, Sales Order, Purchase Order, Delivery Note,
   Purchase Receipt, Material Request, or Payment Entry.
3. Have the intent methods construct documents with elevated permission
   internally.

Until all three are done, an honest description of the control is: it binds
callers that choose to be bound. That is genuinely useful against the careless
agent and the naive integration, which are the primary actors. It is worth
nothing against the insider and nothing against a prompt-injected agent that has
been told to use the raw API.

**Why the intent path cannot be distinguished on the wire.** Even a server-side
observer cannot currently tell the two apart. `harness/intent.py:221` copies
every non-null field returned by `get_item_details` into the row it POSTs,
including `price_list_rate`. The resulting request body is a superset of what a
naive caller sends. There is no marker, no header, and no signature. A network
or database-level control cannot enforce "must have come through the engine"
even in principle, as things stand. Fixing this means either the server-side
deployment above, or a signed intent envelope, and the second is weaker because
the signing key sits in the same process as the agent.

### Bypass 2: the reason is free text and nothing verifies it

**Severity: high. Structural, and probably not fully fixable.**

The override contract requires a reason string
(`harness/intent.py:200-201`). It requires nothing about the reason's content,
its author, or its truth. `harness/intent.py:250-254` concatenates the reasons
into the document's `remarks` field.

The repo demonstrates the weakness itself. In the headline Phase 6 result, all
1,091 recorded overrides carry reasons written by a script:

```python
# harness/simulate.py:145-147
"reason": f"source invoice {r.Invoice} priced at {r.Price} vs list {lp}"
```

An agent asked to supply a reason will supply one. It will be fluent, plausible,
and generated by the same process that chose the price. What the control
actually produces is therefore not "a human decided this" but "a caller was
forced to state, at write time, that it was deviating and by how much, and that
statement is now frozen on the document." Those are different guarantees and the
weaker one is what is on offer.

What the weaker guarantee is still worth:

- It is a *contemporaneous* statement, not a reconstruction. It records the
  derived value as of the write, which cannot be recovered later.
- It is frozen. `remarks` carries no `allow_on_submit`, and
  `frappe/model/base_document.py:1353-1361` throws on any post-submit change to
  such a field. Changing it requires cancel and amend, which leaves a trail.
- It is a hook for approval. A reason field that must be populated is where a
  human approval requirement can later be attached, per customer, per threshold.

What would strengthen it, none of which is implemented: structured reason codes
rather than free text; a required approver identity distinct from the writing
user; a threshold above which the write is queued rather than executed; and
per-line storage rather than a concatenated parent-level field.

**Do not claim the reason proves a human decided.** It does not, and the repo's
own numbers prove it does not.

### Bypass 3: fields outside the catalog

**Severity: medium. Open.**

The catalog covers six fields, all discovered through one derivation path
(`erpnext.stock.get_item_details.get_item_details`, `harness/census.py:70-74`).
The following are not covered and a caller may assert them freely:

- Header-level fields: `conversion_rate`, `plc_conversion_rate`,
  `selling_price_list`, `posting_date`, `due_date`.
- The `taxes` child table in full. A caller supplying its own tax rows is not
  examined at all.
- Payment schedule rows.
- Stock valuation and `incoming_rate` on stock-moving documents.
- Every non-numeric derived field. `harness/census.py:58` returns `None` for
  anything that is not a number, so link fields (`income_account`,
  `expense_account`, `cost_center`, `item_tax_template`, `warehouse`) were never
  probed even though the engine treats them as derived.

The last item is the most significant. `income_account` determines which GL
account revenue lands in. It is derived, it is not in `force_item_fields`, and by
claim 1.1 a caller-supplied value would therefore stick. That has not been
tested, and the census could not have tested it. A caller that picks its own
income account produces a balanced, valid, correctly-priced invoice that posts
revenue to the wrong account.

**A caller can also change the price list.** `selling_price_list` is a header
field. Point it at a list where the item is cheap and the engine derives the
cheap price with no override at all, and the record reads as fully derived.
Nothing in the catalog constrains the choice of price list. For an agent with
customer-scoped context this is arguably the single largest gap, because it
defeats the control while satisfying it.

### Bypass 4: derive-then-write is not atomic

**Severity: low. Open.**

The engine reads the derived value (`harness/intent.py:210-214`) and then writes
in a separate HTTP request (`:256`). Between the two, the Item Price can change.
The recorded `derived_value` would then differ from what the server would derive
at write time, and the two values on the record disagree with each other.

This is a genuine time-of-check-to-time-of-use gap, but it is a correctness
nuisance rather than an attack: exploiting it requires the ability to change
Item Prices, and anyone with that ability can simply set the price they want and
write a fully derived document with no override at all. That is bypass 5.

### Bypass 5: poison the derivation

**Severity: medium. Out of scope by design, worth stating.**

A caller with write access to Item Price, Pricing Rule, or
`Customer.default_price_list` does not need to override anything. Set the list
price to 1.00, write a fully derived invoice, set it back. The record shows
`rate == price_list_rate`, no override, no reason, and reads as a clean derived
document. The control's own logic certifies it.

This is a master-data permission problem, not a transaction problem, and the
answer is that agents should not hold write permission on Item Price. But the
control gives no signal here at all, and a customer who assumes "no override
recorded means it was derived from the correct price" is assuming something the
control does not deliver. The correct statement is "derived from whatever the
price list said at write time."

### Bypass 6: cancel and amend

**Severity: low. Open.**

`remarks` is immutable after submit, but a submitted invoice can be cancelled and
amended. The amended document is a new document. If the amendment is written
directly through `/api/resource` (bypass 1), the reason does not carry forward.
Nothing in this repo examines the amendment path, and no corpus scenario covers
it.

### Bypass 7: fields the census marked accepted on weak evidence

**Severity: low. Correctness, not attack.**

34 of the 42 "silently accepted" census verdicts had a derived value of 0.0
(claim 2.6). For those, the census did not establish that a real derivation was
overwritten. The refuse rules for `weight_per_unit`, `discount_amount`, and
`discount_percentage` rest on that weaker evidence.

The risk this creates is false refusal, not false acceptance: the engine may
refuse a field the server would have handled fine, breaking a legitimate caller.
That is the safe direction to be wrong in, but the catalog's `evidence` strings
currently overstate what the census showed and should be reworded.

Related: `intents/catalog.yaml:28` states `price_list_rate` was
"silently_accepted on 8/9 doctypes." The census recorded 9 of 9
(`reports/census.json`). The catalog's 8 is the more defensible figure but for a
reason the catalog does not give: the ninth (Material Request) derived 0.0, so
nothing was overwritten there. One of the two numbers is wrong as stated and the
discrepancy should be resolved.

## What this control explicitly does not attempt

- **Authentication and authorisation.** Frappe's problem. If an agent has
  credentials it should not have, nothing here helps.
- **Preventing an unbalanced ledger.** Impossible to trigger
  (`general_ledger.py:397-427`) and therefore not a control anyone needs.
- **Detecting bad records after the fact.** Argued impossible in
  [POSITIONING.md](POSITIONING.md) and claim 5.3.
- **Bounding how far off list a price may be.** That is
  `Item.max_discount` (`selling_controller.py:266-272`), and note that the
  interaction described in claim 6.3 means it does not fire on a caller-supplied
  `rate`. Verify that against a live instance before telling a customer either
  way.
- **Anything about quantity, customer, dates, or which items appear.** Those are
  caller-asserted by definition and no derivation exists to compare against.

## Residual risk, honestly

Deploy this exactly as it exists in the repo today and you get: agents that use
the library write self-describing documents, and agents that do not are
unaffected. That closes the careless-agent case for callers you control, which
in practice is most of the exposure at most companies, and closes nothing else.

Deploy it server-side behind a permission boundary and you additionally close
bypass 1, which is what turns it into a control worth paying for. Bypasses 2, 3,
and 5 remain open in that deployment, and bypass 3 in particular (`income_account`,
`selling_price_list`, the tax table) should be closed before the boundary is
described as complete.
