# headless-erp

**A write-time control for ERP write APIs.** It records, at the moment of the
write, whether each value came from the server's own derivation or from the
caller's assertion, and forces every assertion to carry a reason. It is not a
scanner and not an anomaly detector: it never looks at a finished document and
guesses whether the number was right.

Built and verified against ERPNext v16.34.1 / Frappe v16.33.0, because it is the
most complete open-source ERP whose source you can read.

---

> **All source citations refer to erpnext 16.34.1 and frappe 16.33.0**, the
> versions running in `docker/pwd.yml`. Line numbers move between releases, and
> twice during this work a number was read out of the develop branch by mistake.
> `make citations` re-checks every load-bearing citation against the running
> container and fails if one has drifted.

## The finding

An ERP write mixes two kinds of value. Some the server can compute from its own
state: the price list rate for this item, the income account for this item
group, the exchange rate for this currency on this date. Some only the caller
knows: the customer, the quantity, a negotiated price.

ERPNext takes both in the same JSON body, stores the result, and keeps no record
of which was which.

### Start with the exchange rate

An exchange rate is the clearest case, because nobody negotiates one.

```
Currency Exchange USD to INR, resolved by the server:  94.46
Invoice: 4 x $250 = $1,000
```

| | `conversion_rate` | `grand_total` | `base_grand_total` | GL balanced |
|---|---|---|---|---|
| caller omits the field | **94.46** (derived) | 1,000.00 USD | **94,460.00 INR** | yes |
| caller sends `1.0` | **1.00** (kept) | 1,000.00 USD | **1,000.00 INR** | yes |

`ACC-SINV-2026-03035` and `ACC-SINV-2026-03036` on the live instance. A $1,000
invoice booked at ₹1,000 instead of ₹94,460. Debits equal credits in both. The
trial balance nets to zero either way.

The mechanism is three lines. `set_price_list_currency` fills the rate only when
the caller left it empty:

```python
# accounts_controller.py:1061 (v16.34.1) / :719 (v17.0.0-dev)
elif not self.conversion_rate:
    self.conversion_rate = get_exchange_rate(...)
```

and the only validation is that it is non-zero: `validate_conversion_rate` at
`accounts_controller.py:3261` on v16.34.1, moved to
`accounts/services/taxes.py:230-241` on v17.0.0-dev, called from
`taxes_and_totals.py:152-166` in both. It throws when the rate is falsy and
never compares it against the derived rate.

Citations here follow the split described in
[docs/CLAIMS.md](docs/CLAIMS.md): the live results are v16.34.1, the readable
`vendor/` tree is v17.0.0-dev, and where a v16 line number does not resolve in
that tree the v17 location is given too.

Independently reproduced twice: `reports/derivation_map.md` finding 1
(`ACC-SINV-2026-02343`, `SAL-ORD-2026-00049/50`) and the run above.

Two caveats on the number itself, because the rate is not a constant.
`erpnext.setup.utils.get_exchange_rate:97-105` looks for a `Currency Exchange`
record and, finding none, falls through to an external FX feed
(`:117-144`, cached six hours). On this instance the `Currency Exchange` table
is empty, so 94.46 came from that feed and will differ tomorrow. The committed
`reports/derivation_map.md` recorded 87.0 against a `Currency Exchange` record
that existed at the time. **The 94x multiple is real; the specific multiplier
floats.**

### The same shape, eight times

From `reports/derivation_map.md`, which surveys the server-side derivation
surface beyond the item-detail path. Every row marked live carries a document
name in that file.

| # | Field | What the server knows | What the caller can do | Evidence |
|---|---|---|---|---|
| 1 | `conversion_rate`, `plc_conversion_rate` | Currency Exchange, or an external feed | Assert `1.0`. 94x error in company currency. | live |
| 2 | `taxes` rows / `taxes_and_charges` | the named Tax Template | Name an 18% template, send zero rows, get an invoice with no tax. | live |
| 3 | `discount_amount` via Pricing Rule | the rule, applied to the real list price | Forge `price_list_rate: 10000` and the same 10% rule computes `discount_amount 1000`, `rate 9000`, still citing `PRLE-0001`. | live |
| 4 | `commission_rate` | Sales Partner / Sales Person master | 75 of 367 `fetch_from` fields carry `fetch_if_empty`, so the caller wins. Master 2% became 75%, `incentives 750` instead of 20. | live |
| 5 | Item Price itself | nothing yet | With `auto_insert_price_list_rate_if_missing = 1`, invoicing an unpriced item at 7777 **creates** the Item Price at 7777. The next honest caller derives 7777. | live |
| 6 | `income_account`, `expense_account` | Item Default to Group to Brand to Company | Server derives `Sales - HTC`; caller's `Interest Income - HTC` survives. | live |
| 7 | `conversion_factor` | UOM Conversion Detail | Master says `Box = 10`, caller sends 7, `stock_qty` becomes 28 instead of 40. **This moves stock, not just money.** | live |
| 8 | `weight_per_unit` | Item master | In `force_item_fields` and still the caller's number, because the "derivation" reads the caller's own value back out (`get_item_details.py:620`). | live |

Finding 5 is the one worth reading twice. It is not a wrong value on one
document. It writes the wrong value into the master data, where every later
caller derives it honestly and correctly.

The honest scale, from the same document: roughly **15 to 18** real derivation
decisions are caller-overridable, against about **292 `fetch_from` fields that
are genuinely enforced** server-side. ERPNext is not undefended. The defended
surface is `fetch_from`; the undefended surface is everything it derives in
Python.

---


**The census count is history-dependent, and that is not a defect in the census.**
A fresh instance reports 196 probes and 6 accepted fields. An instance that has
already recorded purchases reports 203 and 7, the extra being
`last_purchase_rate`, which `get_item_details` only returns once an item has a
purchase history to read. `make all` runs the census before the corpus creates
any purchase documents, so a clone reproduces the smaller number. Both are in
`reports/clean/census.json` lineage; the six-field set is the stable claim.

## Why this class of error survives every check you run

**The ledger balances.** Always, by construction.
`erpnext/accounts/general_ledger.py:473-503` calls
`raise_debit_credit_not_equal_error` (`:536`) before a voucher posts. An
unbalanced document cannot exist, so a balanced one tells you nothing. After
2,876 submitted invoices on this instance, including every wrong document above,
`reports/trial_balance.json` records 8,346 GL entries and 717,378.18 on both
sides. Difference: 0.00. That artifact exists to demonstrate that the check is
uninformative, not to pass it.

**ERPNext's own tests pass.** 4,153 of them
(`grep -rn "def test_" --include="*.py" erpnext | wc -l` in the v16.34.1 tree).
Every document above is structurally valid. There is nothing for them to catch.

**A post-hoc detector is feasible on price, and imprecise.** How often real
commerce prices off reference depends entirely on what you call the reference,
over the same 1,033,527 lines of UCI Online Retail II:

| reference for "list price" | off reference | why it is wrong on its own |
|---|---|---|
| per-SKU median, pooled across customers | 31.4% | Bimodal seller (wholesale and retail). A two-price SKU shows half its lines off by construction. |
| per-customer modal price for that SKU | 3.4% | 42.9% of lines are the only time that customer bought that SKU, so they are trivially at reference. |
| per-customer modal, pairs bought 2+ times | **6.0%** | the honest cut |
| per-customer modal, pairs bought 6+ times | **8.9%** | where a usual price genuinely exists |

The first two are in `reports/dataset_analysis.json`; the last two are
reproducible from `data/sales_clean.csv`. **The defensible figure is 6 to 9
percent, not a third.**

At 6 to 9 percent a price detector works. It is just expensive and imprecise: on
a million line items a year that is 60,000 to 90,000 lines in a review queue,
almost all of them legitimate, permanently, and precision does not improve with
volume because the deviation is real business behaviour rather than noise.

| approach | false positives | question it answers |
|---|---|---|
| post-hoc detection | 6 to 9 percent of all lines | is this price unusual? |
| write-time capture | none | did anybody decide this? |

A price can be unusual and correct. A price can be ordinary and unconsidered.
Only the second question distinguishes them.

**`conversion_rate` does not share that weakness.** An exchange rate is a
published fact, not a commercial judgment. There is no legitimate population of
invoices booked at 1.0 when the rate is 94.46, so detection there is clean, and
the error is 94x rather than a discount. If you take one thing from this repo
and build nothing else, reconcile `conversion_rate` against
`get_exchange_rate` for every foreign-currency document you have ever posted.

---

## What is not true

Every claim this project has made and withdrawn, in one place, so a reader does
not have to find them scattered through the history. The full register with
evidence and strength ratings is in [docs/CLAIMS.md](docs/CLAIMS.md).

**"The caller receives no indication that a Price List entry existed."**
False, retracted. The write response returns the saved document, containing
`price_list_rate: 250.0` and `discount_amount: 249.0`. It is visible in this
repo's own `reports/latest.json` and was there the whole time the claim stood. A
caller that reads its own response can see exactly what it overrode. Any
argument built on caller blindness is void.

**"The API accepts documents the UI cannot produce."**
Retracted as stated. It rested on `price_list_rate` being `read_only` on 5 of 9
child doctypes, which it is. But `Selling Settings.editable_price_list_rate`
exists precisely to make that field editable in the Desk UI
(`public/js/utils/sales_common.js:326-339`), and it is a checkbox. The claim
holds only under default settings and is one administrator action from being
false.

**"Post-hoc detection of this class of error is impossible."**
Retracted. It was built on the 31.4% figure, which is definition-dependent.
Against a per-customer reference the rate is 6 to 9 percent, and at that rate a
detector is feasible. "Expensive and imprecise" is not "impossible" and the two
should never have been conflated. The surviving argument is the different-questions
one above, and it is an argument, not a measurement: **no detector was ever
built, run, or scored in this repo**, so no precision figure here is real.

**"31.4% of real commerce prices off list."**
Retracted as a headline. It is a real computation over real data and it is
partly an artifact of using a pooled median as the reference for a seller with
two price points. The defensible range is 6 to 9 percent.

**"144 of 196 census probes were protected, so ERPNext has real defences."**
Retracted as a framing, and the numbers have since moved to 156 of 203. Most
"protected" fields are totals the server recomputes anyway. Worse, on an
instance carrying Pricing Rules a `protected` verdict does not mean the
derivation defended itself: six probes are recorded protected with stored values
of 225.0, 25.0 and 10.0 against a supplied 7.0, which is a 10% rule firing, not
a defence.

**"The trial balance demonstration is a finding."**
Retracted. Balance is enforced. Presenting it as a result implies the opposite
outcome was possible. It is usable only in the negative direction.

### Two things that are ordinary, not discoveries

**A caller-supplied `rate` winning over the price list is documented, intended
ERPNext behaviour.** It is the mechanism by which a manually entered discount
survives a re-save. It is not a bug and no fix is being requested.

**"The API does not enforce the UI's `read_only`" is a generic Frappe
property**, true of every read-only field on every doctype. Field-level
`read_only` appears nowhere in the server-side save or validate path. It is a
rendering hint. Naming it as an ERPNext-specific finding would be wrong.

What is worth naming is the consequence of those two ordinary facts together: a
class of field where the server holds a correct value, the caller may overwrite
it, and the resulting record is indistinguishable from a deliberate human
decision.

---

## What this does about it

A three-way contract, declared in `intents/catalog.yaml` across 11 business
intents, enforced by `harness/intent.py` and by a server-side endpoint.

- **derive** the caller does not send the field. The server is asked first and
  what it returns is written.
- **override** the caller may send a different value, but only as a declared
  override carrying a reason, and only after the derived value has been computed
  and recorded next to it.
- **refuse** the caller may not send the field at all.

### The nine refused fields

Each names the evidence that put it there.

| field | why |
|---|---|
| `price_list_rate` | the reference price. Forging it fabricates the baseline a discount is measured against. |
| `discount_amount` | computed from list and rate; accepting it lets the two disagree |
| `discount_percentage` | same |
| `weight_per_unit` | item master property, not a transaction value |
| `conversion_rate` | derivation map finding 1 |
| `plc_conversion_rate` | same mechanism, price-list side |
| `conversion_factor` | derivation map finding 7. Moves stock quantity. |
| `income_account` | derivation map finding 6 |
| `expense_account` | derivation map finding 6 |

`rate` is the one field in the override bucket, because a business genuinely
does sell off list and a human genuinely may decide to. `source` additionally
refuses `min_order_qty`; `return` refuses `rate` outright, because a return
reverses an agreed transaction and may not reprice history.

### The boundary is what makes this a control

The contract as a client library is advice. A caller can ignore it and POST to
`/api/resource` directly. `harness/enforce.py` closes that, and the proof is a
pair of results, not one:

```
Administrator      POST /api/resource/Sales Invoice  rate: 1.0   ->  ALLOWED
constrained agent  POST /api/resource/Sales Invoice  rate: 1.0   ->  403
constrained agent  POST /api/method/bill_intent      rate: 1.0   ->  refused, declare it as an override
constrained agent  POST /api/method/bill_intent      + a reason  ->  written, recorded
```

The first line is the control case. Without it the 403 would prove only that
something was broken. Frappe checks `create` permission inside
`Document.insert()` (`frappe/model/document.py:477`), and every REST and
`frappe.client` write route reaches `insert()` or `save()`, so denying `create`
at the role level closes all nine of them at once. The only way in for that
identity is `harness/server_scripts/bill_intent.py`, which enforces
derive-or-refuse server-side.

**An audit record is read from the saved document, never from the caller's
intent.** ERPNext applies Pricing Rules during `validate`, after the endpoint
has set the rate. A rule was observed replacing a caller's explicit reasoned
override of 1.0 with 225.0. A record built from intent would have asserted a
decision that never took effect. The endpoint therefore reads the value back off
the persisted document and reports `derived`, `requested` and `stored`
separately, flagging `DRIFT` when the last two disagree
(`bill_intent.py:52-74`).

---

## Current numbers

| what | result | artifact |
|---|---|---|
| contract cases | **10 / 10** | `reports/clean/intent_proof.json` |
| accounting corpus | **49/52** across 9 categories | `reports/clean/corpus.json` |
| enforcement boundary | **11/11** | `reports/clean/boundary.json` |
| derivation census | 203 probes, 150 protected, 43 accepted, 10 rejected; 7 fields across 9 doctypes | `reports/clean/census.json` |
| differential | 0 gaps when `rate` is omitted, 3 when asserted (1,000.00 vs 4.00) | `reports/latest.json` |
| replay at scale | 1,000 real invoices written twice, 4,380 lines, 1,091 overrides recorded, 0 invariant failures | `reports/simulation.json` |
| trial balance | guaranteed balanced by construction, therefore uninformative | `reports/trial_balance.json` |

Everything in `reports/clean/` comes from one run against a site created for the
purpose, not from the long-lived instance this was developed on. That
distinction matters: an earlier version of these numbers was measured on an
instance four agents had been writing to, and the census moved when it was
re-run clean.

**The two corpus failures are deliberate.** `srv-01` and `srv-02` use a raw path
that bypasses the contract on purpose, to document what ERPNext does natively: a
caller-supplied `conversion_factor` putting 49 in the stock ledger instead of
70, and a naive write creating a master `Item Price` from an invented rate.
Neither is reachable through the contract. They stay failing because they are
findings, not defects to tune away.

Read the census figures with the caveat above: the 203 is a small field set
multiplied across doctypes, most "protected" verdicts are arithmetic the server
recomputes anyway, and the numbers moved between runs because of a Pricing Rule
rather than because of anything in ERPNext.

Read the replay as a round-trip test. `harness/simulate.py` computes which lines
are off list, passes exactly those as overrides, and then reports that many
overrides were recorded. The 1,091 = 1,091 equality is a tautology. It shows the
override path preserves information at volume. It shows nothing about detection,
and all 1,091 reasons were written by a format string.

---

## Run it

```bash
make setup      # venv + pinned requirements
make up         # ERPNext v16.34.1 stack, ~5 min on first run
make all        # data, differential, census, contract, corpus, boundary, replay
```

Individual targets: `make diff census contract corpus boundary simulate`.
Defaults to `http://localhost:8080`, `Administrator` / `admin`.
`make boundary` needs `server_script_enabled` in the container's site config;
[docs/REPRODUCE.md](docs/REPRODUCE.md) has the exact command and the measured
runtime for every step.

---

## Documentation

| document | what it is for |
|---|---|
| [docs/CLAIMS.md](docs/CLAIMS.md) | every claim, its evidence, its verification method, and a strength rating from A to D. Inference is labelled inference. Read this first if you are hostile. |
| [docs/POSITIONING.md](docs/POSITIONING.md) | what this is, who buys it, the honest competitive picture including the ERPNext controls that already exist, and when you do not need this |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | eight bypasses, written adversarially against our own control |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | one vendor, one derivation path in the census, replay-not-browser, no stock coverage, single currency, unverifiable reasons |
| [docs/AUDIT_TRAIL.md](docs/AUDIT_TRAIL.md) | the override record: structure, hash chain, and what it does and does not prove |
| [docs/REPRODUCE.md](docs/REPRODUCE.md) | fresh clone to every number, with runtimes and the gaps that remain |
| `reports/derivation_map.md` | the full server-side derivation survey the findings table above summarises |

---

## Layout

```
intents/catalog.yaml           11 intents, the derive/override/refuse contract, 27 invariants
corpus/scenarios.yaml          52 scenarios asserting accounting principles, not ERPNext behaviour
harness/intent.py              the client-side intent engine
harness/enforce.py             provisions the role, the user and the endpoint
harness/server_scripts/        bill_intent.py, the server-side enforced contract
harness/prove_boundary.py      proves the boundary, including the control case
harness/census.py              derivation census probe
harness/oracle.py              ctx reconstruction, both paths, the diff
harness/audit.py               the override record
harness/simulate.py            replays UCI Online Retail II through both paths
docker/pwd.yml                 ERPNext v16.34.1 stack
```

## Known gaps

Stated here rather than left for a reader to find. Detail in
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).

- The boundary covers **one intent** (`bill`) and binds **only identities you
  deliberately constrain**. Anyone holding a normal ERPNext role still writes
  directly.
- The endpoint accepts whatever `customer` and `company` the caller names and
  runs with `ignore_permissions=True`. No scope validation yet.
- Drift is reported, not prevented, and only for `rate`.
- The UI comparison column is a faithful replay of what the browser asks the
  server, not an observed browser session. No browser is driven anywhere here.
- `fulfil` and `receive` declare stock invariants that have never executed,
  because every fixture item is `is_stock_item: 0`.
- One company, one currency for everything except the exchange-rate finding, no
  tax templates applied, one seller's dataset from 2009 to 2011.
- `Item.max_discount` is read from source as not firing on a caller-supplied
  `rate` (`taxes_and_totals.py:221` zeroes `discount_percentage` before
  `selling_controller.py:266-272` reads it). **Not reproduced live.** It is the
  most consequential untested reading here.

## License

MIT for this harness. ERPNext is GPLv3 and Frappe Framework is MIT; neither is
redistributed here (`vendor/` is gitignored).
