# The override record

**Status:** implemented and verified against ERPNext v16.34.1 / Frappe v16.33.0 on
`http://localhost:8080`. Every output in this document was produced by
`harness/demo_audit.py` against that instance; nothing here is illustrative.
Document names (`ACC-SINV-…`, `IOV-…`) come from one particular run and differ
on each re-run — the values, deltas and behaviour do not.

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
the period, and because the overridable set spans nine doctypes (Sales Invoice,
Sales Order, Delivery Note, Purchase Order, Purchase Receipt, Purchase Invoice,
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
  edit the supplied value   -> refused: UpdateAfterSubmitError: Not allowed to change Supplied Value after submission
  DELETE the record         -> refused [417]: Submitted Record cannot be deleted. You must Cancel it first.
```

The same three refusals apply to a raw `PUT /api/resource/Intent Override
Log/<name>`; `frappe.client.save` and the REST verbs run through the same model
code.

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
| `recorded_at` | Datetime, required | when the API call happened, distinct from `target_posting_date` |
| `seq` | Int, indexed | chain position |
| `prev_hash`, `record_hash` | Data(64) | SHA-256 chain; `record_hash` is unique |
| `schema_version` | Int | part of the hashed payload, so the hash format can evolve |

`actor` and `actor_user` are both present because they answer different
questions. `actor` is what the intent layer says the caller is, and a caller can
lie about it. `actor_user` is the authenticated session, and it cannot. When
they disagree, that disagreement is itself the finding.

`recorded_at` and `target_posting_date` are both present for the same reason:
they diverge on every backdated document, and an auditor wants to be able to ask
both "what was booked into September" and "what was written on the 8th".

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
records claiming the same predecessor), and `unsubmitted` (a record left at
docstatus 0 by a crash between insert and submit).

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

### 7.2 It does not stop a caller bypassing the intent layer

This is the largest gap and it should be stated plainly: **`harness/intent.py`
is a client-side convention.** Any caller with the same credentials can skip it
entirely and `POST /api/resource/Sales Invoice` with `rate` set to whatever it
likes. ERPNext accepts that — Phase 2's census is a catalogue of exactly which
fields it accepts silently — and no record is written, because the thing that
writes records is the layer that was skipped. The audit trail is complete with
respect to overrides that went through the intent layer, and blind to everything
else. Any claim it makes about "every override in the period" carries that
asterisk.

Closing it needs a *server-side* boundary. Frappe has one, and it is the right
tool: **field permission levels**.

A `DocField` with `permlevel > 0` is writable only by a role holding `write` at
that permlevel. Enforcement is in
`frappe/model/document.py::validate_higher_perm_levels` →
`frappe/model/base_document.py::reset_values_if_no_permlevel_access`, called
from `Document.insert` (`document.py:483`) and `Document.save`
(`document.py:592`) — i.e. on every write path, including the REST API. For a
new document, offending fields are reset to their default; for an existing one,
to their stored value.

Applied here, the deployment would be:

1. `Property Setter` on `Sales Invoice Item.rate` (and the equivalent child
   field on each of the other eight doctypes) setting `permlevel = 1`.
2. A role — say `Pricing Override Approver` — granted `write` at permlevel 1.
3. The agent's service account gets a role with permlevel-0 access only.
4. The intent layer runs under an identity that *does* hold permlevel 1, so a
   declared, reasoned override still works.

Then an agent that skips the intent layer and POSTs `rate` directly does not get
a mispriced invoice; it gets the list price, and the only path to an off-list
rate is the path that writes a record.

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
  is about. Making it loud needs a `validate` hook — a Frappe Server Script on
  each doctype — that rejects `rate != price_list_rate` unless a matching log
  record exists for the document being saved.
- **Administrator bypasses permlevel entirely** (`document.py:1026`:
  `if frappe.session.user == "Administrator": return`). Every script in this
  repository, including the demo, authenticates as Administrator, so the
  boundary would be invisible to them. A real deployment gives every agent its
  own non-Administrator service account; that is a prerequisite, not a detail.

The boundary was proved on a scratch DocType rather than on `Sales Invoice
Item.rate` because a permlevel `Property Setter` on Sales Invoice is global,
persistent metadata, and other processes were driving this instance at the time.
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

---

## 8. Integrating with the intent engine

`harness/intent.py` is untouched by this work (another process owns it). The
integration is one line, at the point where it currently builds the `remarks`
string — after `insert`/`submit` returns, so the target document exists:

```python
import audit
...
saved = self.client.insert(doc)
if submit and spec["maps_to"].get("submittable"):
    saved = self.client.submit(saved)
res.name = saved.get("name")
...
audit.record_result(self.client, res, actor=caller_identity)
```

and the deletion of:

```python
if res.overrides:
    doc["remarks"] = "; ".join(...)   # remove: destroys a real field, records nothing usable
```

`record_result` is duck-typed on `.doctype`, `.name`, `.intent` and `.overrides`
(each with `.fieldname`, `.derived_value`, `.supplied_value`, `.reason`,
`.row_idx`) — the existing `IntentResult` and `Override` dataclasses already
satisfy it, so no import from `intent.py` is needed and no cycle is created.
`actor` is the one new thing the engine has to supply: the identity of whoever
called it, which today it never learns.

---

## 9. Reproducing

```bash
python harness/demo_audit.py                        # write, query, immutability, chain
python harness/demo_audit.py --reset                # drop everything first, clean run
python harness/demo_audit.py --tamper               # + raw-SQL tamper detection
python harness/demo_audit.py --boundary             # + the permlevel boundary proof
```

`--reset` cancels and deletes every log record and drops the DocType. It is a
development affordance, and it is also §7.3 made concrete: an Administrator can
destroy this trail with four API calls, and nothing inside ERPNext can stop
them.

Full row-level output is written to `reports/audit_demo.json`.
