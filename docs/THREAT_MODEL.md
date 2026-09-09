# Threat model

Written adversarially against this project's own control. Every bypass below is
real and most of them are open.

Evidence for anything asserted here is in [CLAIMS.md](CLAIMS.md).

## The one-line summary

**There are two implementations of the control and only one of them is a
boundary.**

`harness/intent.py` is a client library. It runs in the caller's process and
binds only callers that choose to import it. Any writer that does not is
entirely unconstrained.

`harness/server_scripts/bill_intent.py` runs server-side. Paired with the role
that `harness/enforce.py` provisions, it is a real boundary: the constrained
identity cannot write a Sales Invoice by any route except the endpoint.
`reports/boundary.json` records 8 of 8 checks, including the control case
(`Administrator` posting `rate: 1.0` directly is allowed) and the boundary case
(the constrained identity gets 403).

That boundary covers **one intent** and binds **only identities somebody
deliberately constrained**. Everyone holding a normal ERPNext role, including
every human, writes directly exactly as before. Read the rest of this document
with that as the frame.

## What is being protected

Not the ledger. ERPNext defends the ledger perfectly well: it refuses
unbalanced vouchers (`general_ledger.py:473-503`), blocks postings to group,
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
| **Insider with API credentials** | Full API write credentials, knows the system, wants a specific record to read as legitimate. | Malicious | The hardest case, and largely out of scope. |
| **Compromised MCP server** | Whatever the ERP user it authenticates as can do. | Attacker's | In scope only for what the ERP role permits. |

**Explicitly out of scope**: an attacker with database access, an attacker with
`Administrator`, an attacker who can install Frappe apps or edit Server Scripts,
and anyone who can change `Selling Settings`. All four can defeat everything
here trivially. Note that the boundary itself is a Server Script, so anyone who
can edit Server Scripts can rewrite the control rather than bypass it.

## What the control stops

For a caller that goes through either implementation:

| Attempt | Result | Client engine | Enforced endpoint |
|---|---|---|---|
| Send `price_list_rate` in a line, any value | Refused before any write | `intent.py:179-187` | `bill_intent.py:19-22` |
| Send `discount_amount` or `discount_percentage` | Refused | same | same |
| Send `weight_per_unit` | Refused | same | same |
| Send `min_order_qty` on a Material Request | Refused (intent-level) | `catalog.yaml:151-154` | not covered |
| Send `rate` inside the line body | Refused. `rate` is overridable, but only as a declared override, never smuggled into the payload | `intent.py:179-187` | `bill_intent.py:23-25` |
| Declare a `rate` override with no reason, or whitespace | Refused | `intent.py:200-201` | `bill_intent.py:35-37` |
| Declare an override of a field that is neither overridable nor refused | Refused | `intent.py:195-199` | not covered: the endpoint only reads `rate` overrides and silently ignores others |
| Declare a `rate` override on a `return` | Refused. The intent-level refuse beats the default override | `catalog.yaml:238-244` | not covered |
| Order an item with no resolvable price | Refused. Neither will guess a rate | `intent.py:215-220` | `bill_intent.py:26-30` |
| Allocate a payment above an invoice's outstanding | Refused | `intent.py:111-115` | not covered |
| Supply `outstanding` on a payment allocation | Refused | `intent.py:106-109` | not covered |
| Allocate more in total than `paid_amount` | Refused | `intent.py:123-126` | not covered |
| Override `rate` with a reason | **Allowed.** Derived value computed first, both values plus the reason recorded | `intent.py:239-245` | `bill_intent.py:33-41`, `:64-74` |

The last row is the design. The control does not prevent off-list pricing, which
is normal commerce and runs at 6 to 9 percent of lines on real data (claim 5.3).
It prevents off-list pricing that does not say so.

Note the third column. The endpoint reimplements four of the twelve rules. It is
the only one that is enforced, and it is the smaller of the two.

## Bypasses

### Bypass 1: write directly to /api/resource

**Severity: total for unconstrained identities. Closed for constrained ones, on
one intent.**

For an identity with any normal ERPNext write role, `POST /api/resource/Sales Invoice`
with a hand-built body goes nowhere near either implementation.
`frappe/api/v1.py:45-48` constructs the document and inserts it. This is
confirmed as the control case in `reports/boundary.json`: `Administrator` posts
`rate: 1.0` and it is accepted.

Every one of these is an independently whitelisted write endpoint in stock
Frappe, and all of them are open to such an identity:

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

**Why the boundary closes all of them at once.** Frappe checks `create`
permission inside `Document.insert()` (`frappe/model/document.py:477`), and every
route in the table reaches `insert()` or `save()`. So denying `create` at the
role level denies the whole table, not one endpoint. `harness/enforce.py:21-56`
does exactly that: a role with read-only permission on 24 master doctypes and no
write permission on any transaction doctype. `harness/prove_boundary.py:38-46`
verifies the 403 with a plain `requests.Session`, so nothing about the check
depends on the client library behaving.

**What the boundary does not close.**

1. **It binds one intent.** `bill_intent` is the only Server Script installed
   (`enforce.py:67-76`). The constrained identity cannot write a Sales Invoice
   directly, and also cannot write a Sales Order, a Delivery Note, a Payment
   Entry, or anything else at all, because the role denies everything. That is
   safe but not useful: an agent that needs to do more than bill has no
   compliant route and will be given a broader role, at which point bypass 1
   reopens for whatever was granted. **The pressure to widen the role is the
   real risk here**, and it is a deployment pressure that no code in this repo
   resists.
2. **It binds only deliberately constrained identities** (claim 7.7). Every
   pre-existing role is untouched. This is correct behaviour and it must never
   be described as "the API is now closed."
3. **The endpoint is a privilege boundary.** It runs with
   `ignore_permissions=True` (`bill_intent.py:50`, `:74`). A caller who finds a
   way to make it construct a document it should not is escalating, not merely
   evading a convention. Nothing in this repo fuzzes it, reviews it as
   security-sensitive code, or checks that `args.get("customer")` and
   `args.get("company")` are ones this identity should be allowed to bill.
   **That last omission is a live hole**: the endpoint accepts any customer and
   any company the caller names.

**A request from the client engine cannot be told apart from a naive one.** This
is a separate point from the retracted detection claim (R5) and it holds. Even a
server-side observer cannot tell an `intent.py` write from a naive one.
`harness/intent.py:221` copies every non-null field returned by
`get_item_details` into the row it POSTs, including `price_list_rate`, so the
request body is a superset of what a naive caller sends. There is no marker and
no signature. Only the permission boundary distinguishes them, which is another
way of saying the boundary is the control and the library is not.

### Bypass 2: the reason is free text and nothing verifies it

**Severity: high. Structural, and probably not fully fixable.**

Both implementations require a non-empty reason (`intent.py:200-201`,
`bill_intent.py:35-37`) and nothing more. There is no check on the reason's
content, its author, or its truth.

The repo demonstrates the weakness itself. In the Phase 6 result, all 1,091
recorded overrides carry reasons written by a script:

```python
# harness/simulate.py:145-147
"reason": f"source invoice {r.Invoice} priced at {r.Price} vs list {lp}"
```

An agent asked to supply a reason will supply one. It will be fluent, plausible,
and generated by the same process that chose the price. What the control
produces is therefore not "a human decided this" but "a caller was forced to
state, at write time, that it was deviating and by how much, and that statement
is now frozen against the document."

What the weaker guarantee is still worth:

- It is a *contemporaneous* statement, not a reconstruction. It records the
  derived value as of the write, which cannot be recovered later.
- It is durable. The client engine writes `remarks`, which carries no
  `allow_on_submit` and is therefore immutable after submit
  (`frappe/model/base_document.py:1353-1361`). The endpoint writes a Comment
  (`bill_intent.py:72-74`), which is a separate document with its own trail.
- It names an actor. `bill_intent.py:69` records `frappe.session.user`, so the
  identity that made the call is on the record even though the reason's content
  is not verified.
- It is a hook for approval. A reason field that must be populated is where a
  human approval requirement can later be attached.

What would strengthen it, none of which is implemented: structured reason codes
rather than free text; a required approver identity distinct from the writing
user; a threshold above which the write is queued rather than executed.

**Do not claim the reason proves a human decided.** It does not, and the repo's
own numbers prove it does not.

### Bypass 3: fields outside the catalog

**Severity: medium. Open in both implementations.**

The catalog covers seven fields, all discovered through one derivation path
(`erpnext.stock.get_item_details.get_item_details`, `harness/census.py:70-74`).
The endpoint covers four of them. The following are not covered and a caller may
assert them freely:

- Header-level fields: `conversion_rate`, `plc_conversion_rate`,
  `posting_date`, `due_date`.
- The `taxes` child table in full. A caller supplying its own tax rows is not
  examined at all.
- Payment schedule rows.
- Stock valuation and `incoming_rate` on stock-moving documents.
- Every non-numeric derived field. `harness/census.py:58` returns `None` for
  anything that is not a number, so link fields (`income_account`,
  `expense_account`, `cost_center`, `item_tax_template`, `warehouse`) were never
  probed even though the client engine treats them as derived.

The last item is the most significant. `income_account` determines which GL
account revenue lands in. It is derived, it is not in `force_item_fields`, and by
claim 1.1 a caller-supplied value would therefore stick. That has not been
tested, and the census could not have tested it. A caller that picks its own
income account produces a balanced, valid, correctly-priced invoice that posts
revenue to the wrong account.

**A caller can also change the price list.** `selling_price_list` is a header
field, and both implementations take it from the caller
(`bill_intent.py:12` defaults it but accepts an override). Point it at a list
where the item is cheap and the derivation returns the cheap price with no
override at all, and the record reads as fully derived and fully compliant.
Nothing in the catalog constrains the choice of price list. **For an agent with
customer-scoped context this is the single largest gap, because it defeats the
control while satisfying it.**

### Bypass 4: derive-then-write is not atomic

**Severity: low in the endpoint, low in the library.**

Both read the derived value and then write in a separate step
(`intent.py:210-214` then `:256`; `bill_intent.py:26-27` then `:50`). Between
the two, the Item Price can change, and the recorded `derived` value would then
differ from what the server would derive at write time.

The window is much smaller in the endpoint, which runs both steps in one server
request. Exploiting it requires the ability to change Item Prices, and anyone
with that ability can simply use bypass 5 instead.

### Bypass 5: poison the derivation

**Severity: medium. Out of scope by design, worth stating.**

A caller with write access to Item Price, Pricing Rule, or
`Customer.default_price_list` does not need to override anything. Set the list
price to 1.00, write a fully derived invoice, set it back. The record shows
`rate == price_list_rate`, no override, no reason, and reads as a clean derived
document. The control's own logic certifies it.

The boundary partially addresses this: `harness/enforce.py:26-33` grants the
constrained role **read-only** access to `Item` and `Item Price`, and no access
at all to Pricing Rule. So a constrained agent cannot do this. Any identity
holding a normal role can.

A customer who assumes "no override recorded means it was derived from the
correct price" is assuming something the control does not deliver. The correct
statement is "derived from whatever the price list said at write time."

### Bypass 6: cancel and amend

**Severity: low. Open.**

`remarks` is immutable after submit and a Comment is a separate document, but a
submitted invoice can be cancelled and amended. The amended document is a new
document, and if the amendment is written directly (bypass 1) the reason does
not carry forward. Nothing in this repo examines the amendment path, and no
corpus or boundary check covers it.

### Bypass 7: rules that fire after the control has run

**Severity: medium. Detected but not prevented. This is the sharpest finding in
the repo.**

ERPNext applies Pricing Rules during `validate`, which runs after the endpoint
has set the rate and before the document is persisted
(`taxes_and_totals.py:170-221`). A rule can therefore silently replace a
caller's value between the intent being formed and the document being stored.

Observed: a caller's explicit reasoned override of 1.0 was replaced by 225.0, a
rule-derived 10% off the 250.00 list price. Corroborated independently in
`reports/census.json`, where Sales Invoice and Delivery Note probes supplying
`rate: 7.0` have a stored value of `225.0` alongside `discount_percentage: 10.0`
and `discount_amount: 25.0`.

This is an attack on the audit record rather than on the amount. The document is
priced by a configured rule, which is legitimate. What is not legitimate is an
audit record asserting that a caller chose 1.0 for a stated reason, attached to
a document that says 225.0. The record would be a true statement about an
intention and a false statement about a transaction.

**Mitigation, partial.** `harness/server_scripts/bill_intent.py:52-62` reads the
value back off the saved document and reports three values per line, `derived`,
`intended`, and `stored`, rather than one. When `stored` and `intended` disagree
it writes a `DRIFT` warning into the Comment (`:70-71`) and returns a
`drift_detected` count (`:76-77`).

**What remains open.**

- Drift is reported, not blocked. The document is already inserted when the
  discrepancy is found. Nothing rolls it back or refuses.
- Only `rate` is reconciled. If a rule changes `discount_amount`,
  `item_tax_template`, or `income_account`, nothing notices.
- The client engine (`harness/intent.py`) has no equivalent reconciliation. It
  records `supplied_value` from the request, not from the response, so every
  override it logs on a Pricing-Rule instance may be describing a value that was
  never stored.
- The same mechanism silently corrupted this project's own census (claim 2.16):
  six probes recorded as `protected` were overwritten by the rule, not defended
  by a derivation. Any measurement in this repo that infers a mechanism from
  "what I sent versus what was stored" is unsound on an instance carrying rules.

The general lesson, and it applies to any provenance system on any ERP:
**an audit record is a statement about a stored document, so it must be read
from the stored document.** Anything built from the writer's intent is a
statement about intentions, which is exactly the thing the record exists to stop
trusting.

### Bypass 8: fields the census marked accepted on weak evidence

**Severity: low. Correctness, not attack.**

28 of the 37 "silently accepted" census verdicts had a derived value of 0.0
(claim 2.6). For those, the census did not establish that a real derivation was
overwritten. The refuse rules for `weight_per_unit`, `discount_amount`, and
`discount_percentage` rest on that weaker evidence.

The risk this creates is false refusal, not false acceptance: either
implementation may refuse a field the server would have handled fine, breaking a
legitimate caller. That is the safe direction to be wrong in, but the catalog's
`evidence` strings overstate what the census showed and should be reworded.

Related: `intents/catalog.yaml:28` states `price_list_rate` was
"silently_accepted on 8/9 doctypes." The census records 9 of 9. The catalog's 8
is the more defensible figure but for a reason the catalog does not give: the
ninth (Material Request) derived 0.0, so nothing was overwritten there. One of
the two numbers is wrong as stated.

## What this control explicitly does not attempt

- **Authentication and authorisation.** Frappe's problem. If an agent has
  credentials it should not have, nothing here helps.
- **Preventing an unbalanced ledger.** Impossible to trigger
  (`general_ledger.py:473-503`) and therefore not a control anyone needs.
- **Detecting bad records after the fact.** Not because it is impossible, which
  was an earlier claim of this project and is retracted (claim R5), but because
  it answers a different question. A detector against a per-customer reference
  runs at 6 to 9 percent of all lines (claim 5.3) and tells you a price was
  unusual, not that nobody considered it.
- **Bounding how far off list a price may be.** That is
  `Item.max_discount` (`selling_controller.py:266-272`), and the interaction at
  claim 6.3 means it may not fire on a caller-supplied `rate`. Verify that
  against a live instance before telling a customer either way.
- **Anything about quantity, customer, dates, or which items appear.** Those are
  caller-asserted by definition and no derivation exists to compare against. The
  endpoint does not even check that the named customer belongs to this
  identity's scope.

## Residual risk, honestly

**With the client library alone**: agents that use it write self-describing
documents, agents that do not are unaffected. That closes the careless-agent
case for callers you control, which in practice is most of the exposure at most
companies, and closes nothing else.

**With the boundary as it stands**: a deliberately constrained identity can
write Sales Invoices only through a contract that enforces derive-or-refuse, and
cannot write anything else at all. That is a real control and it is the first
thing in this repo that survives an adversarial caller. Its limits are that it
covers one intent, binds only the identities you constrain, does not validate
which customer or company the caller may bill, reports drift rather than
preventing it, and leaves bypasses 2, 3, and 5 open.

**The honest sequencing**: close bypass 3 (`income_account`,
`selling_price_list`, the tax table) and add customer and company scoping to the
endpoint before describing the boundary as complete. Those two are cheap and
they are the difference between a demonstration and a control somebody can
deploy.
