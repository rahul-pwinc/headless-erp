# The override record

**Status:** implemented and verified against ERPNext v16.34.1 / Frappe v16.33.0 on
`http://localhost:8080`. Every output in this document was produced by
`harness/demo_audit.py` against that instance; nothing here is illustrative.
Document names (`ACC-SINV-…`, `IOV-…`) come from one particular run and differ
on each re-run — the values, deltas and behaviour do not.

**It is wired in.** Both write paths produce these records: the library path
(`harness/intent.py`) and the enforced server-side endpoint
(`harness/server_scripts/bill_intent.py`). They share one doctype, one hash
chain, and one canonical encoding. §8 covers the wiring; §10 covers what it
cost to make the second path work inside RestrictedPython.

---

## 1. What was wrong

Phase 4 got the *decision* right and the *record* wrong. `harness/intent.py`
refuses undeclared overrides, demands a reason, and derives the server's value
before applying the caller's. Then it stores the result like this:

```python
doc["remarks"] = "; ".join(
    f"override {o.fieldname}: {o.derived_value} -> {o.supplied_value} ({o.reason})"
    for o in res.overrides
)
```

A reviewer called this "not an audit trail design." That is correct, and it is
worth being precise about why, because each failure points at a requirement:

| Failure | Consequence |
|---|---|
| Free text in a single column | "Every override in Q3, by field" is a regex over every voucher in the period. There is no index, no type, no aggregate. |
| Schema is a semicolon | Two overrides on one document are one string. A reason containing `;` corrupts the parse. Nothing validates any of it. |
| Destroys the field it borrows | `remarks` is a real business field. The assignment is unconditional — whatever a user or another integration wrote there is gone. |
| Not tied to a row | `row_idx` exists on the `Override` dataclass and is thrown away at serialisation. On a 40-line invoice you cannot tell which line moved. |
| No actor | Nothing records who or what made the call. |
| No timestamp of its own | Only the document's `modified`, which every later edit overwrites. |
| Mutable whenever the document is | `remarks` is an ordinary field, so on any draft (`submit=False`) the "record" can be rewritten through the same API that wrote it. It is frozen on submit only as a side effect of the *document* being frozen, not by any property of the record. |
| Dies with the document | Delete the draft, lose the history. |
| **Written from intent, not from the document** | The string is built before `insert`, from what the intent layer decided. ERPNext then runs `validate` and can change it. The record survives as a confident, immutable account of a decision that never took effect. This is the worst of the eight, and §2.3 is about it. |

The replacement is `harness/audit.py`: a standalone, submittable, hash-chained
DocType, created through the REST API, one row per overridden field.

---

## 2. Three design choices, argued

### 2.1 Standalone DocType, not a child table on the voucher

A child table is the tempting answer — the record lives physically inside the
thing it describes, it is frozen when the parent is submitted, and it needs no
foreign key. All three of those turn out to be the problem.

**Child tables cannot answer the auditor's question.** Every child row is keyed
by `parent`/`parenttype`. "Show me every override in the period, by field" is
inherently cross-document; against child tables it is a scan of every voucher in
the period, and because the contract spans nine doctypes (Sales Invoice, Sales
Order, Delivery Note, Purchase Order, Purchase Receipt, Purchase Invoice,
Quotation, Material Request, Payment Entry — `intents/catalog.yaml`), it is nine
scans and a `UNION`. The question is cross-document, so the storage has to be.

**Child tables multiply the schema by nine.** One child table per parent doctype
means nine Customize Form changes, nine sets of columns to keep in step, and a
tenth problem the day a tenth doctype becomes overridable. The standalone
doctype is one table that already covers doctypes nobody has thought of yet:
`target_doctype` is data, not schema.

**Child tables cannot outlive their parent.** `frappe.delete_doc` cascades to
children. An override log whose rows vanish when someone deletes the cancelled
draft is not an audit trail; it is a convenience. The one case where you most
want the record — the document was made to go away — is exactly the case a child
table cannot serve.

The counter-argument for child tables is real and worth stating: they give
referential integrity for free and they are impossible to orphan. The standalone
doctype pays for its independence by having to verify its own target
(`record_override` refuses an unknown one) and by tolerating rows whose target
was later deleted. That trade is deliberate: an orphaned record that says "an
override happened here and the document is gone" is far more useful than no
record at all.

### 2.2 Submittable, because `read_only` is a lie

The whole premise of this repository is that ERPNext's `read_only` is a UI hint
the REST API does not enforce. So immutability cannot be built out of field
flags, and it cannot be built out of role permissions either — those are checked
above the model layer and Administrator bypasses them entirely.

`docstatus == 1` is different. It is checked in
`frappe/model/document.py::_validate_update_after_submit`, on every write path,
for every client, including `Administrator`. So the DocType is `is_submittable:
1`, `record_override` inserts and submits in the same call, and **no field is
marked `allow_on_submit`** — not even a status field. Verified live:

```
  edit the reason           -> refused: UpdateAfterSubmitError: Not allowed to change Reason after submission
  edit the stored value     -> refused: UpdateAfterSubmitError: Not allowed to change Stored (numeric) after submission
  edit the drift flag       -> refused: UpdateAfterSubmitError: Not allowed to change Drift after submission
  DELETE the record         -> refused [417]: Submitted Record cannot be deleted. You must Cancel it first.
```

The same refusals apply to a raw `PUT /api/resource/Intent Override
Log/<name>`; `frappe.client.save` and the REST verbs run through the same model
code.

One trap in writing that test: Frappe compares against the stored value, so
re-saving a field with the value it already holds is a no-op that succeeds. The
first version of the drift-flag check set `drift` to `"none"` on a record that
was already `"none"` and reported *"ACCEPTED — the record is NOT immutable"*.
The record was fine; the test was wrong. It now always writes a value that
differs from what is there.


A consequence worth naming: because nothing is `allow_on_submit`, the record
cannot carry a lifecycle *status*. "This override's document was later
cancelled" is not stored — it is resolved at query time (§5). That is the point.
The log holds only facts that were true when the override was made and that can
never become false.

### 2.3 Three values, because two is a lie

This is the correction that matters most, and it was learned by shipping the
wrong thing first.

The obvious schema has two value columns: what the server **derived**, and what
the caller **requested**. That pair is a complete account of what the intent
layer decided — and ERPNext is under no obligation to agree with it.

A `Pricing Rule` on this instance (`PRLE-0001`, 10% off `HL-WIDGET-001`) runs
during `validate`, which is *after* the intent layer has set the rate on the
document. It rewrote a line from 250 to 225, silently discarding an explicit,
reasoned override of 1.0. The record built from intent said:

```
derived 250.0  ->  charged 1.0    reason: goodwill credit, approved by finance
```

The document said `225.0`. Every field in that record was written in good
faith, and the record as a whole was false — and immutable, and hash-chained,
and signed with an actor and a timestamp. A confident lie is worse than no
record, because someone will rely on it.

So there are three value columns, not two, and the writer reads the persisted
document back after `insert` **and** `submit` to populate the third:

| Column | Source | What it tells you |
|---|---|---|
| `derived_value` | the price list, before the caller was consulted | the baseline |
| `requested_value` | the caller | what was asked for, and the only column the caller determines |
| `stored_value` | **the saved document, read back** | what is actually on the books |

Plus a verdict:

| Column | Meaning |
|---|---|
| `drift` | `none` \| `changed` \| `unverified` — indexed and filterable |
| `drift_delta` | `stored - requested`. Zero when the decision took effect. |
| `drift_note` | why, in words, for the auditor who is not going to diff two floats |
| `value_delta` | `stored - derived`. **What actually reached the books** — the number that ties to the ledger, which is not the same as what was requested. |

`drift` is three-valued on purpose. `none` and `changed` are both assessments;
`unverified` means the read-back could not locate the field and **no claim is
being made**. Collapsing that third state into `none` would turn an unknown
into a clean bill of health, which is the same class of error as the original
defect.

Verified live — the same scenario that produced the bug, now recorded correctly:

```
  f. explicit override of 1.0, with a 10% Pricing Rule live
      Sales Invoice ACC-SINV-2026-02962  docstatus=1 grand_total=900.0
      -> IOV-2026-09-00088  rate row 0
         derived 250.0 / requested 1.0 / STORED 225.0  eff.delta -25.0  drift=changed
```

Note `eff.delta -25.0`, not `-249.0`. The effective delta is measured against
what the document holds, so it agrees with the general ledger; the requested
value is recorded as a fact about the caller, not as a fact about the books.

The read-back happens once per document in `record_result` and is shared across
every override on it, so the three-value schema costs one extra `GET` per
document, not one per field.

**One consequence to know before writing your own queries.** Frappe stores a
`Float` with no value as `0.0`, not `NULL`. An `unverified` record therefore has
`stored_num = 0.0`, numerically indistinguishable from one whose stored value
really was zero. The text column `stored_value` stays `NULL` and `drift` says
`unverified`, so the truth is on the record — but any `SUM` over `stored_num` or
`value_delta` must be split by `drift` first. `overrides_by_field` and
`overrides_by_actor` both group by `drift` for exactly this reason.

---

## 2b. What can produce a record at all

The contract in `intents/catalog.yaml` sorts every field into three buckets, and
only one of them ever reaches this log.

| Bucket | Fields | Produces a record? |
|---|---|---|
| **refuse** | `price_list_rate`, `discount_amount`, `discount_percentage`, `weight_per_unit`, `conversion_rate`, `plc_conversion_rate`, `conversion_factor`, `income_account`, `expense_account` | **No.** The call is rejected before a document exists. |
| **override** | `rate` | **Yes** — with a reason, one record per row. |
| **derive** | everything else | No. Callers never send it. |

Nine refused, one overridable. That asymmetry is the design, not an accident of
implementation: a refused field produces no record because nothing happened —
there is no document, no ledger entry, and nothing for an auditor to assess. The
evidence of a refusal is the error the caller received, not a row in a log.

This matters when reading any total below. "Every override in the period" means
every `rate` override. It does not mean "every attempt to write something the
system should have derived", and a clean log is not evidence that nobody tried.

The list grew from four fields to nine while this log was being built, and the
addition that matters most is **`conversion_rate`** — the headline finding of
the project. Asserting `conversion_rate: 1.0` on a 1,000 USD invoice makes it
post 1,000 in company currency instead of 94,460, and the books balance
perfectly at the wrong number. It is refused rather than overridable on purpose:
a price is a commercial judgment a human can legitimately make, so it gets a
reason and a record; an exchange rate on a date is a published fact, so there is
no override case to record. Where there is no defensible reason, the right
answer is a refusal, not a better audit trail.

---

## 3. Schema

`Intent Override Log`, module `Custom`, `custom: 1`, `is_submittable: 1`,
`autoname: format:IOV-{YYYY}-{MM}-{#####}`, `track_changes: 1`,
`allow_rename: 0`, `allow_import: 0`.

`custom: 1` is what makes this creatable over REST on a running site: the
DocType lives in the database rather than in an app's filesystem, so it needs
neither developer mode nor a bench restart. `{#####}` goes through
`frappe.model.naming.getseries`, an atomic `UPDATE` on `tabSeries`, so names are
gap-free and collision-free under concurrency.

### Target

| Field | Type | Notes |
|---|---|---|
| `target_doctype` | Link → DocType | indexed, standard filter |
| `target_name` | **Data** | indexed. Deliberately *not* a Dynamic Link — see below |
| `row_idx` | Int | 0-based index into the item table; `-1` means the override was on the parent |
| `target_docstatus` | Int | the target's docstatus **at write time**, frozen |
| `target_posting_date` | Date | indexed. The accounting period key |
| `target_company` | Link → Company | indexed |

**Why `target_name` is `Data` and not a Dynamic Link.** The first draft used a
Dynamic Link, and it worked: ERPNext validated the reference on insert and
rejected `target_name='x'` with `LinkValidationError`. It also made the target
invoice permanently uncancellable:

```
LinkExistsError: Cannot delete or cancel because Sales Invoice
ACC-SINV-2026-02290 is linked with Intent Override Log IOV-2026-09-00007
```

`frappe/model/delete_doc.py::get_dynamic_linked_docs` raises whenever you cancel
a document that a **submitted** record points at — and every record here is
submitted by design. The framework offers exactly two exemptions: the
`ignore_links_on_delete` hook (delete only, and an app-level `hooks.py` entry),
and the target controller's own `ignore_linked_doctypes` tuple, which is how
`GL Entry` and `Stock Ledger Entry` get away with it and which cannot be
extended from outside without patching ERPNext.

So the choice was: framework-enforced referential integrity, or an ERP where
finance can cancel an invoice. An audit trail that blocks ordinary operations
gets removed within a week, and then there is no audit trail at all. The link
became `Data`, and `record_override` verifies the target itself — it fetches the
target document anyway, to snapshot the posting date off it rather than trusting
the caller's label.

### The override

| Field | Type | Notes |
|---|---|---|
| `fieldname` | Data | indexed, standard filter |
| `derived_value` | Small Text | canonical text of what the **server** computed |
| `requested_value` | Small Text | canonical text of what the **caller** asked for |
| `stored_value` | Small Text | canonical text of what the **saved document holds** |
| `is_numeric` | Check | set when derived and stored both parse as numbers |
| `derived_num`, `requested_num`, `stored_num` | Float(6) | numeric mirrors, so the values are summable |
| `value_delta` | Float(6) | `stored - derived`, **signed** — the effect on the books |
| `drift` | Select | `none` / `changed` / `unverified`, indexed, standard filter |
| `drift_delta` | Float(6) | `stored - requested` |
| `drift_note` | Small Text | prose explanation when drift is not `none` |

Two representations on purpose. The text columns are lossless for any field
type — an override of `item_tax_template` is a string. The numeric mirrors make
the money question a `SUM`. `value_delta` is signed because Phase 6 found that
21.3% of real lines in the UCI Online Retail II data price *above* list: an
unsigned "discount" column would misclassify one line in five. And it is
measured `stored - derived` rather than `requested - derived` so that it ties
to the general ledger rather than to the caller's intentions — see §2.3.

### Justification, actor, integrity

| Field | Type | Notes |
|---|---|---|
| `reason` | Small Text, required | rejected at the writer if blank or whitespace |
| `intent` | Data | the intent id from `intents/catalog.yaml` |
| `actor` | Data, required, indexed | caller identity as asserted, e.g. `agent:pricing-bot@1.4` |
| `actor_kind` | Select | Agent / Human / Unknown |
| `actor_user` | Link → User | the session ERPNext actually authenticated |
| `recorded_at` | Datetime, required | when the API call happened, distinct from `target_posting_date`. **Site-local**, not UTC — see below. |
| `seq` | Int, indexed | chain position |
| `prev_hash`, `record_hash` | Data(64) | SHA-256 chain; `record_hash` is unique |
| `schema_version` | Int | currently 3. Part of the hashed payload, and the encoder `verify_chain` dispatches on — see §6. |

`actor` and `actor_user` are both present because they answer different
questions. `actor` is what the intent layer says the caller is, and a caller can
lie about it. `actor_user` is the authenticated session, and it cannot. When
they disagree, that disagreement is itself the finding.

`recorded_at` and `target_posting_date` are both present for the same reason:
they diverge on every backdated document, and an auditor wants to be able to ask
both "what was booked into September" and "what was written on the 8th".

`recorded_at` is written in the **site's** timezone, which on this instance is
`Asia/Kolkata`. The first version used UTC, which was wrong twice over. Every
other timestamp in a Frappe database — `creation`, `modified`, a voucher's
posting time — is site-local, so a UTC `recorded_at` sat 5h30m from the
`creation` of the very row it was on, and anyone reconciling the two columns
would conclude the log was written before the document it describes. It also has
to agree with the enforced server-side path, which has only `frappe.utils.now()`
and is site-local by construction. Two write paths into one table cannot use two
clocks.

The `search_index: 1` flags become real indexes — confirmed on the live table:

```
PRIMARY(name)  actor  creation  fieldname  record_hash  seq
target_company  target_doctype  target_name  target_posting_date
```

### Permissions

| Role | read | create | write | submit | cancel | delete | amend |
|---|---|---|---|---|---|---|---|
| System Manager | ✓ | ✓ | ✓ | ✓ | — | — | — |
| Accounts Manager | ✓ | | | | | | |
| Accounts User | ✓ | | | | | | |
| Auditor | ✓ | | | | | | |

System Manager carries `write` only because Frappe refuses the DocType
otherwise — *"Cannot set Submit, Cancel, Amend without Write"*. That bit is not
what makes the record immutable and never was; `docstatus` is, and it is checked
below the permission layer. Once the record is submitted — milliseconds after
insert — the write bit buys its holder nothing.

---

## 4. The query API

```python
overrides_in_period(client, from_date, to_date, *, basis="posting",
                    fieldname=None, target_doctype=None, actor=None,
                    company=None, drift=None, resolve=True)
overrides_for_doc(client, target_doctype, target_name, *, follow_amendments=True)
overrides_with_drift(client, from_date=None, to_date=None, *,
                     include_unverified=True)
overrides_by_field(client, from_date=None, to_date=None, *, basis="posting")
overrides_by_actor(client, from_date=None, to_date=None, *, basis="posting")
overrides_by_drift(client, from_date=None, to_date=None, *, basis="posting")
resolve_targets(client, rows)
verify_chain(client)
```

`basis` selects which clock "the period" means: `"posting"` (default) filters on
`target_posting_date`, the period the voucher lands in; `"recorded"` filters on
`recorded_at`, when the call happened.

### The requirement, answered in one call

> *show me every override on any document in period X, by field, with reason and
> who made it*

```python
audit.overrides_in_period(client, "2026-09-01", "2026-09-30")
```

Real output:

```
  DATE       TARGET                       ROW  FIELD        DERIVED   REQUESTED    STORED  EFF.DELTA  DRIFT      STATE     ACTOR                     REASON
  ---------------------------------------------------------------------------------------------------------------------------------------------------------
  2026-09-08 SA ACC-SINV-2026-02957         0  rate          250.00      150.00    150.00    -100.00  none       submitted agent:pricing-bot@1.4     goodwill credit, approved by finance (ticket FIN-8812)
  2026-09-08 SA ACC-SINV-2026-02958         0  rate          250.00      337.50    337.50      87.50  none       submitted agent:pricing-bot@1.4     expedited freight priced into the line, per contract clause 7
  2026-09-08 SA ACC-SINV-2026-02959         1  rate          250.00      125.00    125.00    -125.00  none       submitted agent:pricing-bot@1.4     volume tier 3 applied manually; price list not yet updated
  2026-09-08 SA ACC-SINV-2026-02960         0  rate          250.00      225.00    225.00     -25.00  none       submitted human:priya@finance       matched competitor quote, verbal approval from CFO
  2026-09-08 SA ACC-SINV-2026-02961         0  rate          250.00       62.50     62.50    -187.50  none       cancelled agent:pricing-bot@1.4     keyed from the wrong contract; will be reissued
               ↳ target moved submitted -> cancelled since the override; amended by ACC-SINV-2026-02961-1
  2026-09-08 SA ACC-SINV-2026-02961-1       0  rate          250.00      187.50    187.50     -62.50  none       submitted agent:pricing-bot@1.4     reissue of the mis-keyed invoice, correct contract rate
  2026-09-08 SA ACC-SINV-2026-02962         0  rate          250.00        1.00    225.00     -25.00  changed    submitted agent:pricing-bot@1.4     goodwill credit, approved by finance
               !! the saved document holds 225.0 where 1.0 was requested (delta +224). Another ERPNext mechanism — a Pricing Rule, a tax or currency rule, or a server hook — overrode the recorded decision after the intent layer applied it.

  7 override(s) in the period.
```

Note row 3: `row_idx = 1` on a two-line invoice. The semicolon string could not
express that. Note the last row: requested 1.00, stored 225.00, flagged.

### Aggregate, server-side

```python
audit.overrides_by_field(client, "2026-09-01", "2026-09-30")
```

```
  FIELD     TARGET DOCTYPE  DRIFT         COUNT  NET EFF.DELTA       MIN       MAX   NET DRIFT
  --------------------------------------------------------------------------------------------
  rate      Sales Invoice   changed           1         -25.00    -25.00    -25.00      224.00
  rate      Sales Invoice   none              6        -412.50   -187.50     87.50        0.00

  by actor:
    agent:pricing-bot@1.4       Agent   changed    n=1    net_delta=-25.00
    agent:pricing-bot@1.4       Agent   none       n=5    net_delta=-387.50
    human:priya@finance         Human   none       n=1    net_delta=-25.00
```

Both aggregates group by `drift` — not for tidiness, but because of the
NULL-Float behaviour described in §2.3: an `unverified` record folded into the
same bucket contributes a silent zero and reads as "this override moved
nothing".

This is a `GROUP BY` executed in MariaDB, not a Python loop over fetched rows.
One implementation note: Frappe 16 rejects SQL functions written as strings in
`fields` — *"SQL functions are not allowed as strings in SELECT: count(name) as
n. Use dict syntax like {'COUNT': '*'} instead."* — so the aggregates go through
the dict form, and `_normalise_agg` maps the backend's column naming
(`count(name)`, `` count(`name`) ``, …) onto stable keys.

### The question the two-column schema could not be asked

> *show me every override that did not actually take effect*

```python
audit.overrides_with_drift(client, "2026-09-01", "2026-09-30")
```

```
  DATE       TARGET                       ROW  FIELD        DERIVED   REQUESTED    STORED  EFF.DELTA  DRIFT      STATE     ACTOR                     REASON
  ---------------------------------------------------------------------------------------------------------------------------------------------------------
  2026-09-08 SA ACC-SINV-2026-02962         0  rate          250.00        1.00    225.00     -25.00  changed    submitted agent:pricing-bot@1.4     goodwill credit, approved by finance
               !! the saved document holds 225.0 where 1.0 was requested (delta +224). Another ERPNext mechanism — a Pricing Rule, a tax or currency rule, or a server hook — overrode the recorded decision after the intent layer applied it.

  by drift state:
    changed       n=1    net_drift=224.00
    none          n=6    net_drift=0.00
```

`drift` is an indexed `Select`, so this is an index seek rather than a scan, and
it composes with every other filter — `overrides_in_period(..., drift="changed")`
narrows the period query the same way.

`include_unverified` defaults to `True`. A record where the field could not be
read back is not evidence of drift, but it is evidence that no assessment was
possible, and an auditor should see it rather than have it quietly counted as
clean.

---

## 5. The docstatus lifecycle

This is the question a child table answers badly and a free-text field does not
answer at all.

**Cancellation.** The override record is untouched. `target_docstatus` still
holds `1` — the state at the moment the override was made — and `resolve_targets`
looks up the live docstatus and annotates the row:

```
  2026-09-08 SA ACC-SINV-2026-02834  0  rate  250.00 -> 62.50  -187.50  cancelled  agent:pricing-bot@1.4  keyed from the wrong contract; will be reissued
               ↳ target moved submitted -> cancelled since the override; amended by ACC-SINV-2026-02834-1
```

An override on a document that was later cancelled is not noise to filter out.
Cancel-and-reissue-at-a-different-price is the pattern an auditor is looking
for, and it is visible only because the record outlived the document.

**Amendment.** ERPNext amends by cancelling `X` and creating `X-1` with
`amended_from = X`. The amended document goes through the intent layer like any
other, so its overrides get their own records. `overrides_for_doc` walks
`amended_from` in both directions and returns the whole family:

```python
audit.overrides_for_doc(client, "Sales Invoice", "ACC-SINV-2026-02834")
```

```
  2026-09-08 SA ACC-SINV-2026-02834    0  rate  250.00 ->  62.50  -187.50  cancelled  agent:pricing-bot@1.4  keyed from the wrong contract; will be reissued
               ↳ target moved submitted -> cancelled since the override; amended by ACC-SINV-2026-02834-1
  2026-09-08 SA ACC-SINV-2026-02834-1  0  rate  250.00 -> 187.50   -62.50  submitted  agent:pricing-bot@1.4  reissue of the mis-keyed invoice, correct contract rate
```

The price moved twice and both moves are on the record, with both reasons and
both actors. Ask for either name and you get the same lineage.

**Deletion of the target.** ERPNext will not delete a submitted document, but a
cancelled one can be deleted. If that happens, `resolve_targets` reports
`target_state: "deleted"` and the note *"target document no longer exists; the
override record survives it."* The record is not orphaned data to be cleaned
up — it is the only remaining evidence.

**Amendment of the log record itself.** Never. `amended_from` exists on the
DocType only because Frappe requires it on anything submittable; `amend` is `0`
for every role. A recorded override that was wrong is corrected by a new record,
not a revision.

---

## 6. Integrity

`verify_chain()` recomputes every `record_hash` from the record's own content
plus its predecessor's hash, and re-walks the chain. It reports `tampered`
(content no longer hashes to the stored value), `broken` (a `prev_hash` that
does not match its predecessor — what a deletion leaves behind), `forked` (two
records claiming the same predecessor), `unsubmitted` (a record left at
docstatus 0 by a crash between insert and submit), and `unverifiable_schema`.

That last one is separate from `tampered` on purpose. A record written by a
schema version this module no longer knows how to reproduce cannot be checked —
and "I cannot check this" is a different finding from "this was altered". Only
one of them is an accusation, and a tool whose entire value is that its
accusations are trustworthy must not make the cheap one.

### The canonical encoding, and why it changed

The hash is taken over a canonical serialisation of the record. Version 2 used
`json.dumps(sort_keys=True)`. Version 3 uses length prefixes:

```
iol|v3|<seq>|<prev_hash>|<field>=<len>:<value>|<field>=<len>:<value>|...
```

The reason is §10: there is a second implementation of this hash, inside a
Frappe Server Script, running under RestrictedPython with no `json` module and
no imports. Two implementations of a hash must agree byte for byte or the chain
reports tampering that never happened — a false accusation, which is the worst
possible failure for this component. `json.dumps` cannot be hand-rolled safely
in a sandbox: `ensure_ascii=True` escaping, key sorting, and separator handling
are three chances to disagree on some reason string containing a quote or an
accented character. Length prefixes need no escaping at all, and the field order
is a fixed list rather than a sort.

Both encoders are kept. `verify_chain` dispatches on each record's own stored
`schema_version`, so v2 records written before the change still verify instead
of being reported as tampered.

This was tested against the strongest attacker it is meant to catch: raw SQL
against MariaDB with the site's own credentials, underneath Frappe entirely.

```
  UPDATE `tabIntent Override Log` SET reason='...' WHERE name='IOV-2026-09-00031'
    the row now reads: 'routine, nothing to see here'
    verify_chain -> ok=False  tampered=['IOV-2026-09-00031']
      IOV-2026-09-00031  stored     36b36c476f90901186f6e6088ad78c4566d912eaca1efc34c33ecab2a9839c0a
                         recomputed a9c0d429ebc2417116b6ddb0487f7a2742ffb131aab801f119e62fb16b9d4b37

  DELETE FROM `tabIntent Override Log` WHERE name='IOV-2026-09-00029'
    verify_chain -> ok=False  records=5  broken=['IOV-2026-09-00030']
      IOV-2026-09-00030 (seq 5) expects prev db09687093943aab…, stores d289856d2759c7f0…
```

Reproduce with `python harness/demo_audit.py --tamper`.

---

## 7. What this design does **not** give you

This section is the important one. Everything above is a mechanism; none of it
is a guarantee of truth.

### 7.1 It does not prove the reason is truthful

`reason` is required, non-empty, and immutable once written. That is the entire
guarantee. "goodwill credit, approved by finance (ticket FIN-8812)" is a string
an agent typed. Nothing checks that FIN-8812 exists, that finance approved
anything, or that the words describe what happened. The log makes a *false*
justification durable, attributable and dated — which is genuinely more than
nothing, because it converts a silent price change into a specific claim someone
can be confronted with. It does not make it true.

Making a reason mean something requires a second party: a reason `Link`ed to an
approval document with its own workflow and its own approver, or a reason drawn
from a controlled list of codes with rules attached, or a threshold above which
the intent layer refuses to proceed without a countersignature. Those are policy
mechanisms. This is the substrate they would sit on.

### 7.2 It binds only the identities you deliberately constrain

This section was written before `harness/enforce.py` existed, when the honest
answer was "nothing stops a bypass." Half of that has since been fixed, and the
other half has not. Both halves matter.

**What now holds.** `harness/enforce.py` provisions a real server-side boundary:
a role (`Agent Writer`) with read-only permission on 24 master doctypes and
**no write permission on any transaction doctype**, a user bound to it, and a
Frappe Server Script API endpoint (`bill_intent`) that runs with elevated
permission and enforces derive-or-refuse server-side. For that identity, the
intent layer is not advice — it is the only door. Verified by
`harness/prove_boundary.py`, 8/8, re-run for this document:

```
1. direct write, unconstrained identity
   Administrator POST /api/resource -> ALLOWED ACC-SINV-2026-02954 at rate 1.0

2. direct write, constrained identity
   agent POST /api/resource -> 403 BLOCKED, boundary holds

3. constrained identity through the enforced endpoint
   ok   clean call derives the list price
   ok   caller supplies rate                 refused: ... Declare it as an override with a reason.
   ok   caller supplies price_list_rate      refused: ... derived, and read-only in the Desk UI
   ok   override without a reason            refused: override of rate on line 0 requires a reason
   ok   override with a reason               ACC-SINV-2026-02956 list=250.0 requested=1.0 stored=1.0
   ok   item with no resolvable price        refused: ... Refusing to guess a rate.
```

This is a genuine control, and it is stronger than the permlevel approach
sketched below because the refusal is *loud*: the caller gets an error naming
the field and the rule, not a `200` and a quietly different document. And since
§10, that path writes full `Intent Override Log` records rather than a Comment —
so for a constrained identity the log is not merely a byproduct of a client
library it could have declined to use.

**What does not hold.** The boundary binds one identity. It is a property of
the `Agent Writer` role, not of the Sales Invoice doctype. Anyone holding a
normal ERPNext role — `Accounts User`, `Sales User`, `System Manager`, any
existing integration's API key, every human at the Desk — still writes directly
to `/api/resource/Sales Invoice` with whatever `rate` they like, and no record
is written, because the thing that writes records is the layer they skipped.

So the honest scope of any claim this log makes is: **complete for constrained
identities, blind for everyone else.** "Every override in the period" means
"every override by an identity you deliberately fenced." On a real ERP with
forty users and six integrations, that is a minority of the write traffic until
someone does the work of constraining each one — and that work is organisational,
not technical.

Closing the rest needs the boundary to attach to the *field* rather than to the
role. Frappe has that too: **field permission levels**. A `DocField` with
`permlevel > 0` is writable only by a role holding `write` at that permlevel.
Enforcement is in `frappe/model/document.py::validate_higher_perm_levels` →
`frappe/model/base_document.py::reset_values_if_no_permlevel_access`, called
from `Document.insert` (`document.py:483`) and `Document.save`
(`document.py:592`) — every write path, including the REST API. For a new
document, offending fields are reset to their default.

The deployment would be a `Property Setter` on `Sales Invoice Item.rate` (and
the equivalent child field on the other eight doctypes) setting `permlevel = 1`,
with `write` at permlevel 1 granted only to the identity the intent endpoint
runs as. Then *every* caller that sets `rate` directly gets the list price
instead, regardless of what role they hold.

**Verified on this instance** (`python harness/demo_audit.py --boundary`), on a
scratch DocType with a permlevel-1 field:

```
  restricted caller POSTs guarded=999 -> stored 0.0     <- the value never landed
  Administrator POSTs guarded=999     -> stored 999.0   <- permlevel does not apply to Administrator
```

Two caveats the proof itself surfaces:

- **The write is silently discarded, not refused.** The caller gets a `200` and
  a document that quietly disagrees with what it sent. That is a worse failure
  mode than an error, and it is the same class of silence this whole repository
  is about — which is why `enforce.py`'s endpoint-shaped boundary is the better
  primary control and permlevel is the backstop behind it. Making permlevel loud
  needs a `validate` hook that rejects `rate != price_list_rate` without a
  matching log record.
- **Administrator bypasses permlevel entirely** (`document.py:1026`:
  `if frappe.session.user == "Administrator": return`). Every script in this
  repository, including the demo, authenticates as Administrator, so the
  boundary is invisible to them. A real deployment gives every agent its own
  non-Administrator service account; that is a prerequisite, not a detail.

The permlevel proof used a scratch DocType rather than `Sales Invoice
Item.rate` because a permlevel `Property Setter` on Sales Invoice is global,
persistent metadata and other processes were driving this instance at the time.
The mechanism is identical; only the target differs.

### 7.3 It does not make the record undestroyable

Role permissions say `cancel: 0` and `delete: 0` for every role, and `docstatus`
refuses edits and deletes through every API path. Administrator is not subject
to role permissions, and can cancel a record and then delete it. Anyone with
database credentials can `DELETE` the row outright — demonstrated in §6.

The hash chain makes that *evident*, not *impossible*. A point edit or a
deletion breaks the chain and `verify_chain` names the record. But the hash is
computed by the same module that writes the row, so an attacker who can reach
the database can also run this code and rebuild the tail. What the chain
genuinely defends against is the realistic threat — one embarrassing row edited
in place — not a determined operator with root.

Real tamper-proofing requires the chain head to be published somewhere ERPNext
cannot write: a periodic append of `verify_chain()["head"]` to external
append-only storage, a signature from a key the ERP does not hold, or shipping
records to a separate system on write. All of those are outside ERPNext, which
is the honest conclusion: an ERP cannot certify its own logs.

### 7.4 Other limits worth naming

- **The chain forks under concurrent writers.** `record_override` reads the
  chain head, then writes. Two writers racing can produce two records claiming
  the same predecessor. `verify_chain` reports this as `forked` rather than
  silently accepting it, but nothing prevents it. Serialising the write (a
  dedicated queue, or an atomic sequence allocation like the one `tabSeries`
  already provides for names) would fix it; single-writer deployments do not hit
  it.
- **Insert and submit are two HTTP calls.** A crash between them leaves a draft.
  Drafts are still returned by every query — none of them filters on
  `docstatus` — and `verify_chain` lists them as `unsubmitted`, so the failure is
  loud rather than lost. A `frappe.client.insert_many`-style atomic
  insert-and-submit would remove the window.
- **`derived_value` is what the derivation returned, not necessarily what
  ERPNext would have stored.** `intent.py` already compensates for one known
  case — `get_item_details` returns `rate: 0` when no rate was supplied, so
  recording 0 as the baseline would tell an auditor the derived price was zero.
  Other fields may have similar quirks that have not been characterised.
- **It records overrides, not every deviation.** A caller that supplies a
  *refused* field is rejected outright (no record needed — nothing happened),
  and a caller that supplies a value identical to the derived one is recorded
  with a delta of zero. But a document changed *after* submission through
  `allow_on_submit` fields is not an override in this sense and is not captured
  here; Frappe's `Version` doctype covers that, separately.
- **`actor` is self-asserted.** It is whatever string the caller passed to
  `record_result`. `actor_user` is the authenticated session and cannot be
  forged, so the pair is checkable — but only if someone checks it.
- **The two write paths share one chain and read the head independently.** A
  library write and an endpoint write landing in the same instant can both take
  the same predecessor. `verify_chain` reports that as `forked`; nothing
  prevents it. It was a theoretical concern with one writer and is a real one
  with two.
- **The enforced path cannot withdraw a document whose record failed.** A
  `frappe.throw` would roll back the audit records written earlier in the same
  request, so the endpoint reports `audit_error` and leaves the invoice. §10
  covers what closing that would take.
- **`drift` detects that the decision was overridden, not by what.** The note
  names the usual suspects (a Pricing Rule, a tax or currency rule, a server
  hook) because the read-back cannot tell them apart: it sees the before and the
  after, not the mechanism in between. Identifying the culprit means going to
  the document's own `pricing_rules` table or Frappe's `Version` history.
- **A drift-free record is not proof the field was never touched.** It means
  the value that ended up stored equals the value requested. A rule that
  computed its way back to the same number, or two mechanisms that cancelled,
  read as `none`. The claim `drift: none` supports is "the books agree with the
  recorded decision," not "nothing else ran."
- **Numeric drift uses a half-cent tolerance.** ERPNext rounds to the
  document's currency precision, so a 0.004 difference is rounding, not an
  override being overridden. A real override smaller than half a cent would be
  recorded as `none`.
- **The read-back is one `GET` after submit, not a subscription.** If something
  changes the document *after* `record_result` returns — an `allow_on_submit`
  edit, a repost, a background job — the record still describes the state at
  read time, and says nothing about the change. `recorded_at` is what bounds
  the claim.

---

## 8. How it is wired in

Not a proposal. Both paths write these records today.

### 8.1 The library path — `harness/intent.py`

The `remarks` hack is gone. In its place:

```python
saved = self.client.insert(doc)
if submit and spec["maps_to"].get("submittable"):
    saved = self.client.submit(saved)
res.name, res.docstatus = saved.get("name"), saved.get("docstatus")

# After the document is saved and submitted, never before: the record is
# built by reading the persisted document back, and a read taken before
# submit would miss whatever the submit path changed.
self._record_overrides(res)
self._cite_record(res)
```

Four things worth naming:

**`remarks` is left alone.** Not appended to, not templated into — untouched.
It is a business field that belongs to whoever wrote it, and the previous
version overwrote it unconditionally. The Desk breadcrumb goes in a `Comment`,
which is the field Frappe provides for exactly this and which adds rather than
replaces:

```
Override recorded in Intent Override Log: IOV-2026-09-00097
```

**`IntentResult` carries the record ids.** `res.override_records` is a list of
`Intent Override Log` names, and each `Override` gains `.record`, `.drift` and
`.stored_value` after the write — so a caller can cite the record without
re-querying, and can see that what it asked for is not what the document holds.
`Override.supplied_value` survives as a property alias for `.requested_value`,
so existing callers (`prove_intent.py`, `simulate.py`) keep working.

**The actor is the calling program, not an invented identity.** With nothing
passed to `IntentEngine(actor=...)`, the default is `harness:<script name>` —
`harness:prove_intent.py`, `harness:run_corpus.py`. The audit record separates
`actor` (asserted, forgeable) from `actor_user` (authenticated, not), and
inventing a service name for `actor` would put a fiction in the forgeable
column. Naming the script is the true statement available.

**An audit failure rolls the document back.** This is the one real judgement
call in the wiring, so the argument is worth stating.

The tempting alternative is to log a warning and return the invoice — the books
balance either way and the caller got what it asked for. But the entire premise
of this layer is that an off-list price is *allowed because it is recorded*. An
override that is not recorded is the exact defect Phase 2 documented, reached by
a different route: a document priced away from the list with nothing, anywhere,
saying who decided that or why. Returning it successfully would mean the harness
produces the artefact it exists to prevent.

So on an audit failure the document is cancelled (or deleted, if it never left
draft) and the caller gets an `AuditWriteFailed`. A cancelled Sales Invoice
reverses its own GL entries, so the books are left where they started. Verified
by injecting an audit outage:

```
AuditWriteFailed raised:
    Sales Invoice ACC-SINV-2026-03039 has 1 unrecorded override(s) and was rolled back.
      audit write failed: RuntimeError: simulated audit outage
      compensation: ACC-SINV-2026-03039 cancelled (docstatus 2); its GL entries are
                    reversed and the books are where they started
   verified live: ACC-SINV-2026-03039 docstatus=2 (2 = cancelled)
   live GL entries remaining: 0
   override records for it   : 0
```

The compensation can itself fail — the network is down, the document is already
linked. When it does, the exception says `COMPENSATION FAILED` and names the
document, because that is a state that needs a person rather than a retry.

The breadcrumb `Comment` is deliberately **not** treated this way: if it fails
it is swallowed, because the authoritative record already exists and is
immutable. Losing a convenience pointer is not worth cancelling a correct
invoice over. The difference between the two calls is whether the information
is stored anywhere else.

### 8.2 The enforced path — `harness/server_scripts/bill_intent.py`

Same doctype, same chain, same `insert`-then-`submit` immutability, written from
inside the Server Script sandbox. It was a Comment before; it is a real record
now. See §10 for what that took.

```
   ok   override with a reason
        ACC-SINV-2026-03086 list=250.0 requested=1.0 stored=1.0  audit=IOV-2026-09-00104
```

Neither path trusts its caller for `stored_value` or `drift`. Both derive them
by reading the persisted document — the library path with one `GET` after
submit, the endpoint by inspecting `doc.items[i].rate` after `doc.insert()`. A
writer that took `stored` from its caller would reintroduce the exact defect
§2.3 describes.

## 9. Reproducing

```bash
# the suites — these now write override records as a side effect of running
python harness/prove_intent.py                      # the contract,        10/10
python harness/run_corpus.py                        # the use-case corpus, 39/39
python harness/prove_boundary.py                    # the enforced path,    8/8

# the audit trail itself
python harness/demo_audit.py                        # write, drift, query, immutability, chain
python harness/demo_audit.py --reset                # drop everything first, clean run
python harness/demo_audit.py --tamper               # + raw-SQL tamper detection
python harness/demo_audit.py --boundary             # + the permlevel boundary proof
```

The drift section (§2) enables Pricing Rule `PRLE-0001` for exactly one
document and disables it again in a `finally`, even if the call raises. The rule
is global metadata on a shared site — while it is on, every Sales Invoice anyone
creates is repriced — so it is never left enabled.

### What the suites leave behind

The three suites are not audit demos — they test the contract. The records below
are a *byproduct* of running them, which is the point: the log fills up because
the real code path writes to it, not because a demo script was pointed at it.
Log emptied first, then the three suites run in order, then queried:

```
  DATE       TARGET                       ROW  FIELD        DERIVED   REQUESTED    STORED  EFF.DELTA  DRIFT      STATE     ACTOR                     REASON
  ---------------------------------------------------------------------------------------------------------------------------------------------------------
  2026-09-08 SA ACC-SINV-2026-03312         0  rate          250.00        1.00      1.00    -249.00  none       submitted harness:prove_intent.py   goodwill credit, approved by finance
  2026-09-08 SA ACC-SINV-2026-03326         0  rate          250.00        1.00      1.00    -249.00  none       submitted harness:run_corpus.py     goodwill credit approved by finance
  2026-09-08 SA ACC-SINV-2026-03327         0  rate          250.00      200.00    200.00     -50.00  none       submitted harness:run_corpus.py     volume deal
  2026-09-08 SA ACC-SINV-2026-03336         0  rate          250.00        1.00      1.00    -249.00  none       draft     endpoint:bill_intent      goodwill, approved by finance

  by actor:
    endpoint:bill_intent      Agent   none      n=1   net_delta=-249.00
    harness:prove_intent.py   Agent   none      n=1   net_delta=-249.00
    harness:run_corpus.py     Agent   none      n=2   net_delta=-299.00

  by field:
    rate    Sales Invoice   none      n=4   net_eff_delta=-797.00

  verify_chain: {'records': 4, 'tampered': 0, 'broken': 0, 'forked': 0,
                 'unsubmitted': 0, 'unverifiable_schema': 0, 'ok': True}
```

Three actors, two write paths, one chain. `harness:prove_intent.py` and
`harness:run_corpus.py` came through the library path; `endpoint:bill_intent`
came from inside RestrictedPython. Note that the endpoint's row is `draft` and
the library rows are `submitted` — the asymmetry described at the end of §10.

Four records for 68 checks across three suites (10 + 50 + 8) is the right order of
magnitude, and worth reading correctly: only `rate` can produce a record (§2b),
and most scenarios test refusals, which produce none because nothing happened.

`--reset` cancels and deletes every log record and drops the DocType. It is a
development affordance, and it is also §7.3 made concrete: an Administrator can
destroy this trail with four API calls, and nothing inside ERPNext can stop
them.

Full row-level output is written to `reports/audit_demo.json`.

---

## 10. The enforced path inside RestrictedPython

The brief expected this half to fail, and to end with a documented Comment
fallback and a note that fixing it needs a real Frappe app. It did not fail. The
enforced endpoint writes the same records as the library path. What follows is
what was measured, because the difference between "impossible" and "possible"
here was entirely a matter of probing the sandbox instead of assuming.

### What the sandbox actually allows

Probed by installing a temporary Server Script that tried each capability in a
`try/except` and returned the results, rather than reasoning about
RestrictedPython's documentation:

| | |
|---|---|
| `frappe.session.user` | works |
| `frappe.utils.now()` | works — site-local, which is why the library path was changed to match |
| **`frappe.utils.sha256_hash(str)`** | **works** — `hashlib.sha256(s.encode()).hexdigest()`, identical to the library path |
| `frappe.get_all(..., order_by=..., limit_page_length=...)` | works — so the chain head is readable |
| `frappe.db.get_value(...)` | works |
| `frappe.get_doc({...}).insert(ignore_permissions=True)` | works |
| `.submit()` on that document | works — so records are immutable, not just present |
| `import hashlib` | **fails**: `__import__ not found` |
| `frappe.parse_json` | **fails**: `module has no attribute` |
| `frappe.as_json` | **fails**: `module has no attribute` |
| `frappe.generate_hash` | **fails**: `module has no attribute` |

`frappe.utils.sha256_hash` is the one that decides it. Without a hash function
the endpoint could write rows but not join the chain, and an unchained record in
a chained log is worse than no record — it looks verified and is not.

### What it cost

One design change, described in §6: the canonical encoding moved from
`json.dumps` to length prefixes so that two implementations — one in ordinary
Python, one in a sandbox with no `json` — can produce identical bytes without
either of them hand-rolling JSON escaping.

Three smaller things the sandbox forced:

- **`int(doc.docstatus)`.** `docstatus` is an `IntEnum` in Frappe 16, and
  `str()` of an enum is not `str()` of an int. Left alone it would have hashed a
  different string on the two sides and reported permanent, phantom tampering.
- **`str(doc.posting_date)`.** A `datetime.date` on the server side, a string
  over REST. They stringify the same, but only if you say so.
- **A hand-written absolute value.** `abs()` is fine, but the half-cent drift
  tolerance is written out as a comparison so the arithmetic is visibly the same
  on both sides.

### Proof that the two implementations agree

The test is not that the endpoint wrote a row. It is that the library-side
verifier, running the library-side encoder, reproduces the hash the sandbox
computed:

```
record              : IOV-2026-09-00103   (written inside RestrictedPython)
stored hash         : 9c57335f1c342099bf554a8945a516aa6240cd627282f6afa4b87f45aaee7975
audit.py recomputes : 9c57335f1c342099bf554a8945a516aa6240cd627282f6afa4b87f45aaee7975
MATCH               : True
docstatus           : 1  (submitted, immutable)
```

and that `verify_chain` is clean across a chain interleaving records from both
writers:

```
verify_chain across BOTH write paths:
  {'records': 17, 'tampered': 0, 'broken': 0, 'forked': 0,
   'unsubmitted': 0, 'unverifiable_schema': 0, 'ok': True}
```

Drift detection works there too. With Pricing Rule `PRLE-0001` enabled, through
the constrained agent identity and the enforced endpoint:

```
  invoice ACC-SINV-2026-03109  derived=250.0 requested=1.0 STORED=225.0
  drift=changed  drift_detected=1  audit=['IOV-2026-09-00107']  audit_error=None
```

### Where the enforced path is still weaker

Not in the record — those are identical. In the failure handling.

The library path cancels the document when the audit write fails (§8.1). The
endpoint **cannot**, and this is a property of Frappe's request handling rather
than an oversight: a `frappe.throw` rolls back the entire request transaction,
including any override records already written earlier in the same loop. So on
an audit failure the endpoint returns the invoice with `audit_error` populated
and the failing line carrying its own `audit_error`. Loud, not silent — but the
document survives, where the library path would have withdrawn it.

Closing that gap needs the document insert and the audit write in one
transaction with a savepoint the script controls, which is exactly the kind of
thing a Server Script is not allowed to do. That is the remaining argument for
packaging this as a real Frappe app: not the record format, which works, but
transactional control over the two writes.

A second, smaller asymmetry: the endpoint writes records against a **draft**
invoice (`doc.insert()`, no submit), so `target_docstatus` is 0 and queries
report `target_state: draft`. The library path records against submitted
documents. Both are accurate about what they saw; they are just not the same
moment in the lifecycle.
