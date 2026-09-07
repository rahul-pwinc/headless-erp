# Positioning

Companion documents: [CLAIMS.md](CLAIMS.md) for the evidence behind every number
used here, [THREAT_MODEL.md](THREAT_MODEL.md) for what the control does not stop,
[LIMITATIONS.md](LIMITATIONS.md) for what has not been tested.

## What this is

A write-time control for ERP write APIs.

Every write to an ERP transaction document mixes two kinds of value: values the
server can compute from its own state (the price list rate for this item on this
price list, the income account for this item group, the UOM conversion factor),
and values that can only come from the caller (the customer, the quantity, a
negotiated price). ERPNext accepts both in the same JSON body and stores the
result without distinguishing them.

This project makes the distinction explicit and enforces it at the moment of
writing:

- **derive**: the caller does not send the field. The server is asked for the
  value first and what it returns is written.
- **override**: the caller may send a different value, but only as a declared
  override carrying a reason, and only after the derived value has been computed
  and recorded alongside it.
- **refuse**: the caller may not send the field at all.

There are two implementations of that contract in the repo, and the difference
between them is the difference between advice and a control:

| | `harness/intent.py` | `harness/server_scripts/bill_intent.py` |
|---|---|---|
| Runs | in the caller's process | server-side, as a Frappe Server Script |
| Binds | callers that choose to import it | the identity, whatever it calls |
| Bypassed by | `POST /api/resource` | nothing, for a constrained role |
| Coverage | all 11 intents | `bill` only |

`harness/enforce.py` provisions the boundary that makes the second one real: a
role with no write permission on any transaction doctype, read-only access to
the master data an agent legitimately needs, and the `bill_intent` endpoint as
the only way in. `reports/boundary.json` records 8 of 8 checks passing,
including the two that matter: `Administrator` posting `rate: 1.0` straight to
`/api/resource/Sales Invoice` is allowed, and the constrained identity doing the
same gets 403.

The assignment of each field to one of the three buckets is stated in
`intents/catalog.yaml` and is derived from an empirical census of what the
server actually accepts (`reports/census.json`) crossed with whether the Desk UI
lets a human type into the field.

The value is not in the code, which is small. It is in the claim that the
catalog is correct, which is why [CLAIMS.md](CLAIMS.md) exists.

## What this is not

**It is not an anomaly detector or a scanner.** Not because detection is
impossible, which was an earlier claim of this project and is retracted, but
because detection answers a different question than the one the buyer has.

### How often does real commerce price off reference

`reports/dataset_analysis.json` and `data/sales_clean.csv` cover 1,033,527 real
line items from a UK wholesaler (UCI Online Retail II). The off-reference rate
depends entirely on what you call the reference price:

| reference for "list price" | off-reference | what makes the number wrong |
|---|---|---|
| per-SKU median, pooled across all customers | 31.4% | This seller is bimodal (wholesale and retail). A single median puts roughly half the lines of a two-price SKU off reference by construction. |
| per-customer modal price for that SKU | 3.4% | 42.9% of lines are the only time that customer bought that SKU, so they are trivially at reference. |
| per-customer modal, pairs bought 2+ times | 6.0% | The honest cut. |
| per-customer modal, pairs bought 6+ times | 8.9% | The subset where a usual price genuinely exists. |

The first two are in `reports/dataset_analysis.json`. The last two are
reproducible from `data/sales_clean.csv` and were reproduced independently for
this document (claim 5.2).

**The defensible figure is 6 to 9 percent, not a third.** A single global price
list overstates it. ERPNext models per-customer pricing properly through
`Customer.default_price_list`, `Customer Group.default_price_list`, and Pricing
Rule scoped by `customer_group`, and against a maintained baseline the real
deviation rate is single digits.

### What that means for detection

At 6 to 9 percent a detector is feasible. It is expensive and imprecise. On a
million line items a year, flagging every deviation from an established
customer-specific price puts 60,000 to 90,000 lines into a review queue, almost
all of them legitimate, permanently. Precision does not improve with volume,
because the deviation is real business behaviour rather than noise that averages
out.

The comparison that matters is not feasible versus infeasible. It is that the
two approaches answer different questions:

| approach | false positives | question it answers |
|---|---|---|
| post-hoc detection | 6 to 9 percent of all lines | is this price unusual? |
| write-time capture | none | did anybody decide this? |

A price can be unusual and correct. A price can be ordinary and unconsidered.
Only the second question is the one an auditor asks, and only the second
distinguishes the two cases. Deviation is a proxy. A recorded reason, written at
the moment of the write, is the fact.

That is the entire positioning, and it is narrower than the argument this
project started with.

## Who this is for

The buyer is a team that has put, or is about to put, a non-human writer in
front of an ERP write API, in a company where someone will eventually be asked
to justify a transaction.

Concretely, all three of these need to be true:

1. **Machine writers.** An MCP server, an LLM agent, an integration, or a bulk
   importer holds write credentials to Sales Invoice, Purchase Invoice, Sales
   Order, or Purchase Order. The population of writers is growing and their
   context is not auditable after the fact.
2. **Fields the server can derive.** Pricing, accounts, tax templates, UOM
   conversions. Where nothing is derivable there is nothing to protect.
3. **An accountability requirement.** Someone (finance, external audit, a
   customer dispute, a revenue recognition review) will later ask why a line was
   priced the way it was, and "the API accepted it" is not an answer.

The purchase trigger is usually specific: an agent wrote a wrong price, or
finance blocked an agent rollout because nobody could answer "how would we know."

## The problem, stated narrowly

Six months after the fact, a Sales Invoice line reading
`price_list_rate=250, rate=1, discount_amount=249` is indistinguishable from an
authorised 99.6% discount. That exact document exists in this repo
(`reports/latest.json`, `ACC-SINV-2026-00012`). Nothing in the document, the GL
entries, or any log separates "a salesperson approved this" from "an agent never
looked up the price."

A detector would flag that line, correctly, along with 6 to 9 percent of every
other line in the company. It would not tell you which of them anybody decided.

Two things that are **not** the problem, stated plainly so a reader does not have
to work out what has been oversold:

- **A caller-supplied rate winning over the price list is documented, intended
  ERPNext behaviour**, not a bug and not a discovery. It is how manual discounts
  survive a re-save.
- **"The API does not enforce the UI's `read_only`" is a general property of
  Frappe**, true of every read-only field on every doctype. Field-level
  `read_only` never appears in the server-side save or validate path
  (verified: `vendor/frappe/frappe/model/`). It is a rendering hint. Naming it as
  an ERPNext-specific finding would be wrong.

What is worth naming is the consequence of those two ordinary facts together: a
class of field where the server holds a correct value, the caller may overwrite
it, and the resulting record is downstream-indistinguishable from a deliberate
human decision. The census found seven such fields on nine doctypes
(`reports/census.json`), from one derivation path.

## Competitive picture

### ERPNext itself

This is the most important competitor and it is usually skipped. ERPNext already
ships server-side price controls, and an honest pitch names them first.

| Control | Where | Default | What it catches | What it misses |
|---|---|---|---|---|
| `Item.max_discount` | `selling_controller.py:266-272` | unset per item | `discount_percentage` above a per-item ceiling | Reads `d.discount_percentage`, which `taxes_and_totals.py:221` sets to `0` when the caller supplied `rate` below `price_list_rate`. A caller that sends `rate` instead of `discount_percentage` is not bounded by it. Source-read, not reproduced. |
| `Selling Settings.maintain_same_sales_rate` | `transaction_base.py:145-186`, gated at `sales_invoice.py:828-836` | `0` (off) | A downstream rate that differs from the upstream Sales Order or Delivery Note rate, hard stop unless the user holds `role_to_override_stop_action` | Only fires when the item row links to a prior document. The first document in a chain is unconstrained. |
| `Selling Settings.validate_selling_price` | `selling_controller.py:287-332` | `0` (off) | A net rate below last purchase rate or valuation | Compares against cost, not against the price list. A price above cost but far below list passes. |
| Pricing Rule | `taxes_and_totals.py:170-221` | none defined | Applies a configured discount or margin during validate, overwriting whatever the caller sent | It is a pricing mechanism, not a control. It silently replaces caller values including deliberate overrides, which is its own problem (see below). |
| Server Script, doctype event | `frappe/model/document.py:1705` | none installed | Anything you write, server-side, on the API path as well as the UI path | You have to write it, per field, per doctype, and keep it correct as the schema moves. This is also the mechanism this project's own boundary uses. |
| Role permissions | `frappe/model/document.py:730` | full access for System Manager | Denies `create`/`write` on a doctype entirely | All or nothing at the doctype level. It cannot express "may write this document but not assert this field," which is why the boundary pairs it with an endpoint. |

Read that table as the case against building this rather than for it, then note
what survives. `maintain_same_sales_rate` is the closest existing control and it
is genuinely good: turn it on and set the action to `Stop`, and the sales cycle
becomes rate-consistent server-side. It still does not tell you whether the rate
on the *first* document was derived or asserted, and it records no reason.

The honest summary: ERPNext gives you ceilings and consistency checks. It does
not give you provenance. Nothing in ERPNext records, per field, whether a value
came from the system or from the caller.

### Generic API gateways and schema validators

Kong, Apigee, an OpenAPI validator, a JSON Schema layer in front of the REST
API. These enforce shape, authentication, and rate. They have no model of which
fields the ERP could have derived, so the only rule they can express is "reject
requests containing `price_list_rate`," which is a blunt version of the `refuse`
bucket and cannot express `override` at all, because `override` requires
computing the derived value first in order to record the delta.

### GRC and SoD platforms (SafePaaS, Pathlock, Fastpath, SAP GRC)

These work on roles and on post-hoc analytics: who can do what, which role
combinations violate segregation of duties, which transactions match a risk
pattern. They are strong at the access question and at the "this user should not
be able to both create a vendor and pay it" question.

On the pricing question they are the detector in the table above. They will
surface 6 to 9 percent of lines as unusual and cannot rank within that set,
because the deviation is real business behaviour. This product is upstream of
them and complementary: it produces the field that turns "unusual" into
"unexplained," which is a set small enough to review.

### Agent sandboxes and tool-permission layers

These constrain what an agent's process can reach: which tools, which hosts,
which credentials, what it may spend. They operate on the tool call, not on the
semantics of the document. An agent that is fully authorised to call
`create_sales_invoice` and does so with a stale price is a correct tool call
producing a wrong record. A sandbox is the right control for a different risk.

### Observability and audit logging (Frappe Version, doc events, SIEM)

Frappe already versions documents and ERPNext already has an audit trail. These
record what the value became. They cannot record what the value would have been,
because nobody computed it. That is the specific gap this product fills, and it
is why the control has to be in the write path rather than beside it.

## Why this has to be at write time

The three-way split is only implementable while the server's own derivation is
still reachable. Once the document is saved, the derived value is gone: the
price list may have changed, the pricing rules may have changed, the customer's
default price list may have changed. Recomputing the "should have been" value
months later gives a different number than the one that was available at write
time, and comparing against it produces findings that are wrong in both
directions.

This is why the `override` bucket records `derived_value` and `supplied_value`
as a pair rather than just flagging the row.

## Write the audit record from the saved document

A finding from building the enforced endpoint, and the sharpest engineering
lesson in the repo.

The endpoint sets a line's rate, then calls `doc.insert()`. ERPNext applies
Pricing Rules during `validate`, which runs **after** the rate is set. A rule
configured on the item can therefore rewrite the caller's value between the
intent being formed and the document being persisted, with no error and no
signal. In the observed case a caller's explicit reasoned override of 1.0 was
replaced by 225.0, a rule-derived 10% off the 250.00 list price.

An audit record built from what the endpoint *intended* would have asserted a
decision that never took effect. It would have said "the caller chose 1.0, and
here is why," about a document that says 225.0.

`harness/server_scripts/bill_intent.py:52-74` therefore reads
`doc.items[i].rate` back off the saved document and reports three values per
line rather than one: `derived` (the list price), `requested` (what the caller
asked for), and `stored` (what is actually persisted). When `stored` and
`requested` disagree it emits a `DRIFT` warning into the record and counts it in
`drift_detected`.

The general rule, which applies to any provenance system on any ERP: **an audit
record is a statement about a stored document, so it must be read from the
stored document.** Anything else is a statement about the writer's intentions,
which is exactly the thing the audit record exists to stop trusting.

## When you do NOT need this

Say no to yourself before a customer says it. All of the following are good
reasons not to buy or build this:

- **Only humans write.** If every transaction originates in the Desk UI, the
  derivation runs in the browser before save and the human sees the derived
  value change. Revisit when you add an integration.
- **Agents only read.** A read-only MCP server cannot produce this failure.
- **You already run the ERPNext controls and they cover your fields.** If
  `maintain_same_sales_rate` is `Stop`, `Item.max_discount` is set on the SKUs
  you care about, and your agents always work from a Sales Order rather than
  creating invoices from scratch, you have most of the value already, for free,
  server-side. Turning those on is cheaper than adopting anything here and you
  should do it first.
- **A review queue at 6 to 9 percent is affordable for you.** If you write
  thousands of lines a year rather than millions, a detector plus a human
  reviewer is a smaller and more boring solution, and boring is worth a lot. The
  argument for write-time capture is strongest where the queue would be
  unmanageable or where the reviewer cannot reconstruct the decision anyway.
- **Nothing in your ERP is derived.** If the price list is empty because
  everything is priced by an external system, there is no derivation to protect
  and every value is legitimately caller-asserted.
- **You cannot deploy server-side code.** The boundary requires a Frappe Server
  Script and a custom role. Without both, what remains is the client library,
  which binds only the callers that choose to use it. On a hosted ERP whose API
  you cannot extend, do not buy this expecting enforcement.
- **You will not staff the catalog.** The refuse/override/derive assignment is a
  standing claim about a moving codebase, snapshotted against ERPNext v16.34.1.
  If nobody owns re-running the census, the catalog decays into a config file
  nobody trusts. The census has already moved once between runs
  (claim 2.1).
- **A single Server Script covers your one field.** If the whole requirement is
  "never let anyone book a Sales Invoice line more than 20% below list without a
  note," that is a short Server Script on `Before Validate`, it runs server-side
  on every write path, and it needs no new dependency. Reach for the full
  contract when you have many fields, many doctypes, or a requirement to keep
  the derived value alongside the override.

## What a buyer is actually buying

Not the enforcement code. Three things:

1. **The catalog**, and a commitment to keep it accurate against ERPNext
   releases. `intents/catalog.yaml` is the asset. The engine that reads it is
   replaceable.
2. **The evidence discipline.** [CLAIMS.md](CLAIMS.md) is the deliverable a
   finance or audit reader can take to their own risk committee. Every entry in
   the catalog names the census probe that put it there, and every claim carries
   its own strength rating including the weak ones.
3. **The boundary**, which as of `reports/boundary.json` exists and holds for
   the `bill` intent against a deliberately constrained identity. Its limits are
   real and are stated in [THREAT_MODEL.md](THREAT_MODEL.md): it binds only
   identities somebody chose to constrain, and it covers one intent so far.
