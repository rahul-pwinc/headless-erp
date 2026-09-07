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

- **derive**: the caller does not send the field. The engine asks the server for
  the value first (`harness/intent.py:210-214`) and writes what it gets back.
- **override**: the caller may send a different value, but only as a declared
  override carrying a reason string, and only after the derived value has been
  computed and recorded alongside it (`harness/intent.py:190-202`, `:242-245`).
- **refuse**: the caller may not send the field at all
  (`harness/intent.py:179-187`).

The assignment of each field to one of the three buckets is stated in
`intents/catalog.yaml` and is derived from an empirical census of what the
server actually accepts (`reports/census.json`) crossed with whether the Desk UI
lets a human type into the field.

That is the whole product. It is roughly 320 lines of engine plus a declarative
catalog. The value is not in the code, it is in the claim that the catalog is
correct, which is why [CLAIMS.md](CLAIMS.md) exists.

## What this is not

**It is not an anomaly detector, a scanner, or an audit tool that finds bad
rows.** That approach cannot work, and the strongest evidence in this repo is
the evidence against it.

`reports/dataset_analysis.json` analyses 1,033,527 real line items from a UK
wholesaler (UCI Online Retail II). Taking the per-SKU median price as the list
price, 31.4% of lines do not transact at list, and those lines carry 50.8% of
gross revenue. 88.4% of SKUs sold at more than one price.

That number is partly an artefact of the method, and the artefact matters more
than the headline. A median is a statistic, not a price list. A wholesaler with
a trade price and a retail price for the same SKU will show close to half its
lines away from the median by construction, and ERPNext models exactly that case
properly through `Customer.default_price_list`,
`Customer Group.default_price_list`, and Pricing Rule scoped by
`customer_group`. So the 31.4% is best read not as "a third of this company's
sales were off-list" but as "a third of its lines fall outside any single
reference price, and a naive reconstruction of the reference price cannot tell
you which of those were legitimate."

Both readings support the same conclusion, and it is the conclusion this product
is built on: **you cannot separate a legitimate off-list price from an agent
that never looked up the price, by looking at the finished record.** There is no
threshold, no distribution, and no model that does it, because the two
populations are the same shape. The information that separates them (did anyone
make a decision here) is destroyed at write time and never recorded.

If the information is destroyed at write time, then write time is the only place
to capture it. Everything downstream is reconstruction.

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
human decision. The census found six such fields on nine doctypes
(`reports/census.json`), from one derivation path.

## Competitive picture

### ERPNext itself

This is the most important competitor and it is usually skipped. ERPNext already
ships server-side price controls, and an honest pitch names them first.

| Control | Where | Default | What it catches | What it misses |
|---|---|---|---|---|
| `Item.max_discount` | `selling_controller.py:266-272` | unset per item | `discount_percentage` above a per-item ceiling | Reads `d.discount_percentage`, which `taxes_and_totals.py:221` sets to `0` when the caller supplied `rate` below `price_list_rate`. A caller that sends `rate` instead of `discount_percentage` is not bounded by it. |
| `Selling Settings.maintain_same_sales_rate` | `transaction_base.py:145-186`, gated at `sales_invoice.py:828-836` | `0` (off) | A downstream rate that differs from the upstream Sales Order or Delivery Note rate, hard stop unless the user holds `role_to_override_stop_action` | Only fires when the item row links to a prior document. The first document in a chain is unconstrained. |
| `Selling Settings.validate_selling_price` | `selling_controller.py:287-332` | `0` (off) | A net rate below last purchase rate or valuation | Compares against cost, not against the price list. A price above cost but far below list passes. |
| Server Script, doctype event | `frappe/model/document.py:1705`, `server_script_utils.py` | none installed | Anything you write, server-side, on the API path as well as the UI path | You have to write it, per field, per doctype, and keep it correct as the schema moves. |
| Role permissions | `frappe/model/document.py:730` | full access for System Manager | Denies `create`/`write` on a doctype entirely | All or nothing at the doctype level. It cannot express "may write this document but not assert this field." |

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

They are subject to the detection argument above. An SoD platform looking at
finished Sales Invoices sees `rate=1, price_list_rate=250` and can flag it as a
large discount, which is a rule you can already write in SQL. It cannot tell you
whether a decision occurred. On a customer where 31.4% of lines are legitimately
off any single reference price, that rule is mostly false positives.

This product is upstream of them and complementary: it produces the field that
would make their analytics work.

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

## Why this has to be at write time, restated

The three-way split (derive / override / refuse) is only implementable while the
server's own derivation is still reachable. Once the document is saved, the
derived value is gone: the price list may have changed, the pricing rules may
have changed, the customer's default price list may have changed. Recomputing
the "should have been" value months later gives you a different number than the
one that was available at write time, and comparing against it produces
findings that are wrong in both directions.

This is not a marketing distinction. It is the reason the `override` bucket
records `derived_value` and `supplied_value` as a pair
(`harness/intent.py:33-39`, `:242-245`) instead of just flagging the row.

## When you do NOT need this

Say no to yourself before a customer says it. All of the following are good
reasons not to buy or build this:

- **Only humans write.** If every transaction originates in the Desk UI, the
  derivation runs in the browser before save and the human sees the derived
  value change. The gap this closes does not exist for you. Revisit when you add
  an integration.
- **Agents only read.** A read-only MCP server cannot produce this failure.
- **You already run the ERPNext controls and they cover your fields.** If
  `maintain_same_sales_rate` is `Stop`, `Item.max_discount` is set on the SKUs
  you care about, and your agents always work from a Sales Order rather than
  creating invoices from scratch, you have most of the value already, for free,
  server-side. Turning those on is cheaper than adopting anything here and you
  should do it first.
- **Nothing in your ERP is derived.** Some deployments price everything from an
  external system and treat ERPNext as a ledger of record. If the price list is
  empty, there is no derivation to protect and every value is legitimately
  caller-asserted.
- **You cannot deploy server-side code.** On a hosted ERP whose API you cannot
  extend, this control is advisory only. A client-side library binds the callers
  that choose to use it and nothing else. See
  [THREAT_MODEL.md](THREAT_MODEL.md), bypass 1. If you cannot install a Frappe
  app or a Server Script, do not buy this expecting enforcement.
- **You will not staff the catalog.** The refuse/override/derive assignment is a
  standing claim about a moving codebase. `reports/census.json` is a snapshot
  taken against ERPNext v16.34.1 on 2026-09-07. If nobody owns re-running it,
  the catalog decays into a config file nobody trusts.
- **A single ERPNext-native Server Script covers your one field.** If the whole
  requirement is "never let anyone book a Sales Invoice line more than 20% below
  list without a note," that is twenty lines of Server Script on the
  `Before Validate` event, it runs server-side on every write path, and it needs
  no new dependency. Reach for the full contract when you have many fields, many
  doctypes, or a requirement to keep the derived value alongside the override.

## What a buyer is actually buying

Not the enforcement code. Three things:

1. **The catalog**, and a commitment to keep it accurate against ERPNext
   releases. `intents/catalog.yaml` is the asset. The engine that reads it is
   replaceable.
2. **The evidence discipline.** [CLAIMS.md](CLAIMS.md) is the deliverable that a
   finance or audit reader can take to their own risk committee. Every entry in
   the catalog names the census probe that put it there.
3. **The permission boundary**, once the engine runs server-side. Until then,
   what is on offer is a well-argued convention, and [THREAT_MODEL.md](THREAT_MODEL.md)
   says so in the first section.
