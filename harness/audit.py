"""Phase 7 — the override record.

Phase 4 got the *decision* right: a caller may set `rate` off-list, but only
with a reason, and only against a value the server derived first. It got the
*record* wrong. It wrote:

    doc["remarks"] = "; ".join(f"override {o.fieldname}: {a} -> {b} ({why})")

That is a sentence, not a record. It cannot be filtered, it cannot be summed,
it silently destroys whatever `remarks` already held, its schema is a comma,
and the only way to audit a quarter is to regex several thousand free-text
fields. This module replaces it with a real one.

Design in one line: a standalone, submittable, hash-chained DocType created
through the REST API, one row per overridden field, linked to the voucher by a
validated Dynamic Link.

Why standalone rather than a child table on the voucher
-------------------------------------------------------
A child table looks tempting — it is physically attached to the thing it
describes, and it dies with it. All three of those are the problem:

1.  Child rows of a submitted parent are frozen with the parent, but they are
    also *invisible across parents*. "Every override in Q3, by field" becomes
    a scan of every voucher in the period, per doctype, because child tables
    are keyed by parent. The question an auditor actually asks is cross-
    document; the storage has to be cross-document too.
2.  The overridable set spans nine doctypes (Sales Invoice, Sales Order,
    Delivery Note, Purchase Order, ...). A child table means nine schema
    changes, nine Customize Form entries, nine migrations — and nine separate
    tables to UNION at query time.
3.  A child table cannot outlive its parent. `frappe.delete_doc` cascades to
    children. An override log whose row disappears when someone deletes the
    cancelled draft is not an audit trail.

A standalone doctype gets all three for free: one table, one query, and rows
that survive the target document.

Why submittable
---------------
Immutability has to be enforced by the server, not by a `read_only` flag —
this whole repository exists because ERPNext's `read_only` is a UI hint the
REST API does not enforce. `docstatus == 1` is different: it is checked in
`frappe/model/document.py::_validate_update_after_submit`, on every write
path, for every client. Verified on this instance (see docs/AUDIT_TRAIL.md):
a submitted log record refuses `frappe.client.save`, refuses a raw
`PUT /api/resource/...`, and refuses `DELETE` until it is cancelled first.

So `record_override` inserts and submits in one breath. There is no window in
which the row is editable and no field marked `allow_on_submit`. Nothing about
a recorded override is ever revised — not even its status. Lifecycle
(cancelled target, amended target) is resolved at *query* time against the
live document, so the log itself holds only facts that were true when the
override was made and can never become false.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import FrappeClient, FrappeError  # noqa: E402

DOCTYPE = "Intent Override Log"
GENESIS = "0" * 64

# Fields that go into the record hash, in this order. Anything not listed here
# is either derived from these (value_delta) or is chain plumbing (seq,
# prev_hash, record_hash). Changing this list changes every future hash, so it
# is versioned: SCHEMA_VERSION is part of the hashed payload.
SCHEMA_VERSION = 2
HASHED_FIELDS = [
    "target_doctype",
    "target_name",
    "target_docstatus",
    "target_posting_date",
    "target_company",
    "row_idx",
    "fieldname",
    "derived_value",
    "requested_value",
    "stored_value",
    "drift",
    "reason",
    "intent",
    "actor",
    "actor_user",
    "actor_kind",
    "recorded_at",
]

# Fields earlier versions of this module defined and no longer does.
# ensure_doctype removes them, so a site that ran v1 converges on v2 instead of
# carrying dead columns that queries would silently read as NULL.
OBSOLETE_FIELDS = ["supplied_value", "supplied_num"]


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

def _field_spec() -> list[dict]:
    """The DocType's fields. Kept as data so ensure_doctype can diff against a
    live instance and report drift instead of silently disagreeing with it."""
    return [
        {"fieldname": "sec_target", "fieldtype": "Section Break", "label": "Target"},
        {"fieldname": "target_doctype", "fieldtype": "Link", "options": "DocType",
         "label": "Target DocType", "reqd": 1, "search_index": 1, "in_standard_filter": 1,
         "in_list_view": 1},
        # Data, NOT Dynamic Link — and this was measured, not assumed.
        #
        # A Dynamic Link is the obvious choice: it gets referential integrity
        # for free (the first draft of this module used one, and ERPNext duly
        # rejected target_name='x' with LinkValidationError). It also makes the
        # target document uncancellable. frappe/model/delete_doc.py
        # ::get_dynamic_linked_docs raises LinkExistsError when cancelling any
        # document that a *submitted* record points at — and every record here
        # is submitted, by design. Observed live:
        #
        #     LinkExistsError: Cannot delete or cancel because Sales Invoice
        #     ACC-SINV-2026-02290 is linked with Intent Override Log
        #     IOV-2026-09-00007
        #
        # The only exemptions the framework offers are the `ignore_links_on_delete`
        # hook (Delete only, and an app-level hooks.py entry) and the target
        # controller's own `ignore_linked_doctypes` tuple — which is how GL Entry
        # and Stock Ledger Entry get away with it, and which cannot be extended
        # from outside without patching ERPNext.
        #
        # So: an audit trail that stops finance from cancelling an invoice will
        # be deleted by finance within a week, and then there is no audit trail
        # at all. Give up the framework-enforced link, verify the target in the
        # writer instead (record_override refuses an unknown target), and let
        # the voucher live its normal life.
        {"fieldname": "target_name", "fieldtype": "Data", "label": "Target Name",
         "reqd": 1, "search_index": 1, "in_list_view": 1},
        {"fieldname": "row_idx", "fieldtype": "Int", "label": "Row Index",
         "description": "0-based index into the target's item table. -1 means the override was on the parent."},
        {"fieldname": "cb_target", "fieldtype": "Column Break"},
        # Frozen at write time. The *live* docstatus is resolved at query time
        # by resolve_targets(); storing a mutable status on an immutable record
        # would be a lie waiting to happen.
        {"fieldname": "target_docstatus", "fieldtype": "Int", "label": "Target Docstatus At Write"},
        # The accounting period key. "Overrides in period X" means the period
        # the voucher posts to, not the wall-clock minute the API was called —
        # those differ every time someone backdates.
        {"fieldname": "target_posting_date", "fieldtype": "Date", "label": "Target Posting Date",
         "search_index": 1, "in_standard_filter": 1, "in_list_view": 1},
        {"fieldname": "target_company", "fieldtype": "Link", "options": "Company",
         "label": "Company", "search_index": 1, "in_standard_filter": 1},

        # THREE values, never two. This is the correction that matters most in
        # the whole schema, and it was learned by being wrong:
        #
        #   derived    what the server computed from the price list
        #   requested  what the caller asked for, with a reason
        #   stored     what the saved document actually contains
        #
        # A two-column record (derived, requested) is an account of what the
        # intent layer *decided*, and ERPNext is under no obligation to agree
        # with it. A Pricing Rule runs during validate — after the intent layer
        # has set the rate — and rewrote a line from 250 to 225, silently
        # replacing an explicit, reasoned override of 1.0. The record built
        # from intent said "250 -> 1.0, reason: goodwill". The document said
        # 225. The audit trail was asserting a decision that never took effect.
        #
        # So `stored` is read back off the persisted document (see
        # _stored_value), and the record is written from the document, not from
        # the caller's intention. Anything else is a record of a wish.
        {"fieldname": "sec_override", "fieldtype": "Section Break", "label": "Override"},
        {"fieldname": "fieldname", "fieldtype": "Data", "label": "Fieldname", "reqd": 1,
         "search_index": 1, "in_standard_filter": 1, "in_list_view": 1},
        {"fieldname": "derived_value", "fieldtype": "Small Text", "label": "Derived Value",
         "description": "What the server computed before the caller was consulted."},
        {"fieldname": "requested_value", "fieldtype": "Small Text", "label": "Requested Value",
         "description": "What the caller asked for instead."},
        {"fieldname": "stored_value", "fieldtype": "Small Text", "label": "Stored Value",
         "description": "What the saved document actually holds, read back after "
                        "insert and submit. This is the only one of the three that "
                        "describes reality."},
        {"fieldname": "cb_override", "fieldtype": "Column Break"},
        {"fieldname": "is_numeric", "fieldtype": "Check", "label": "Is Numeric"},
        {"fieldname": "derived_num", "fieldtype": "Float", "label": "Derived (numeric)", "precision": "6"},
        {"fieldname": "requested_num", "fieldtype": "Float", "label": "Requested (numeric)", "precision": "6"},
        {"fieldname": "stored_num", "fieldtype": "Float", "label": "Stored (numeric)", "precision": "6"},
        # stored - derived. The delta that actually reached the books, which is
        # the one an auditor is adding up. Signed on purpose: Phase 6 found
        # 21.3% of real lines price ABOVE list, so an unsigned "discount"
        # column would misclassify one line in five.
        {"fieldname": "value_delta", "fieldtype": "Float", "label": "Effective Delta",
         "precision": "6", "in_list_view": 1,
         "description": "stored - derived: what the override actually did to the books."},

        {"fieldname": "sec_drift", "fieldtype": "Section Break", "label": "Drift"},
        # Three-valued on purpose. "none" and "changed" are both assessments;
        # "unverified" means the read-back could not find the field and no
        # claim is being made. Collapsing the third state into "none" would
        # turn an unknown into a clean bill of health.
        {"fieldname": "drift", "fieldtype": "Select", "label": "Drift",
         "options": "none\nchanged\nunverified", "default": "unverified",
         "search_index": 1, "in_standard_filter": 1, "in_list_view": 1,
         "description": "changed = the saved document does not hold what the caller "
                        "requested, so some other ERPNext mechanism overrode the "
                        "recorded decision."},
        {"fieldname": "cb_drift", "fieldtype": "Column Break"},
        # stored - requested. Zero when the decision took effect.
        {"fieldname": "drift_delta", "fieldtype": "Float", "label": "Drift Delta",
         "precision": "6",
         "description": "stored - requested: how far the document diverged from the "
                        "recorded decision."},
        {"fieldname": "drift_note", "fieldtype": "Small Text", "label": "Drift Note"},

        {"fieldname": "sec_why", "fieldtype": "Section Break", "label": "Justification"},
        {"fieldname": "reason", "fieldtype": "Small Text", "label": "Reason", "reqd": 1},
        {"fieldname": "intent", "fieldtype": "Data", "label": "Intent",
         "description": "The intent id from intents/catalog.yaml that produced this document."},

        {"fieldname": "sec_actor", "fieldtype": "Section Break", "label": "Actor"},
        {"fieldname": "actor", "fieldtype": "Data", "label": "Actor", "reqd": 1,
         "search_index": 1, "in_standard_filter": 1,
         "description": "Caller identity as asserted by the intent layer, e.g. 'agent:pricing-bot@1.4'."},
        {"fieldname": "actor_kind", "fieldtype": "Select", "label": "Actor Kind",
         "options": "Agent\nHuman\nUnknown", "default": "Unknown"},
        {"fieldname": "cb_actor", "fieldtype": "Column Break"},
        # The session ERPNext actually authenticated. Unlike `actor`, the
        # caller cannot choose this one.
        {"fieldname": "actor_user", "fieldtype": "Link", "options": "User", "label": "Session User"},
        {"fieldname": "recorded_at", "fieldtype": "Datetime", "label": "Recorded At", "reqd": 1},

        {"fieldname": "sec_chain", "fieldtype": "Section Break", "label": "Integrity",
         "collapsible": 1},
        {"fieldname": "seq", "fieldtype": "Int", "label": "Sequence", "search_index": 1},
        {"fieldname": "schema_version", "fieldtype": "Int", "label": "Schema Version"},
        {"fieldname": "cb_chain", "fieldtype": "Column Break"},
        {"fieldname": "prev_hash", "fieldtype": "Data", "label": "Previous Hash", "length": 64},
        {"fieldname": "record_hash", "fieldtype": "Data", "label": "Record Hash", "length": 64,
         "unique": 1},

        # Required by the framework for any submittable doctype. Deliberately
        # never used: an override record is never amended. If a recorded
        # override was wrong, the correction is a new record, not a revision.
        {"fieldname": "amended_from", "fieldtype": "Link", "options": DOCTYPE,
         "label": "Amended From", "read_only": 1, "no_copy": 1, "print_hide": 1},
    ]


def _permission_spec() -> list[dict]:
    """Read wide, write narrow, revise never.

    One writer role, three reader roles. `cancel`, `delete` and `amend` are 0
    everywhere — no ordinary operator, and no agent running under an ordinary
    role, has a path to remove or revise a record.

    System Manager carries `write: 1` because Frappe refuses the doctype
    otherwise: "Cannot set Submit, Cancel, Amend without Write"
    (frappe/core/doctype/docperm — submit implies write at the role level).
    That permission is not what makes the record immutable and never was.
    `docstatus == 1` is, and docstatus is checked below the permission layer,
    so the write bit buys the holder exactly nothing once the record is
    submitted — which is a few milliseconds after it is inserted.
    """
    return [
        {"role": "System Manager", "read": 1, "create": 1, "submit": 1,
         "write": 1, "cancel": 0, "delete": 0, "amend": 0, "report": 1, "export": 1},
        {"role": "Accounts Manager", "read": 1, "report": 1, "export": 1},
        {"role": "Accounts User", "read": 1, "report": 1, "export": 1},
        {"role": "Auditor", "read": 1, "report": 1, "export": 1},
    ]


def _doctype_spec() -> dict:
    return {
        "doctype": "DocType",
        "name": DOCTYPE,
        "module": "Custom",
        # custom=1 is what makes this creatable through the REST API on a
        # production site: it lives in the database, not in an app's
        # filesystem, so it needs neither developer mode nor a bench restart.
        "custom": 1,
        "is_submittable": 1,
        # {#####} goes through frappe.model.naming.getseries, which is an
        # atomic UPDATE on `tabSeries`. The *name* is therefore gap-free and
        # collision-free even under concurrency; see verify_chain() for what is
        # and is not guaranteed about the hash chain under the same conditions.
        "autoname": "format:IOV-{YYYY}-{MM}-{#####}",
        "track_changes": 1,
        "allow_import": 0,
        "allow_rename": 0,
        "sort_field": "creation",
        "sort_order": "DESC",
        "search_fields": "target_doctype,target_name,fieldname,actor",
        "description": "Immutable record of an override: what the server derived, what the caller requested, and what the document actually stored.",
        "fields": _field_spec(),
        "permissions": _permission_spec(),
    }


# Fieldtype changes that are safe to apply in place: all of these are backed
# by the same varchar/text column, so switching between them rewrites metadata
# and leaves the stored values untouched. Anything outside this set is a
# migration and is reported, never performed.
_SAFE_RETYPE = {"Data", "Link", "Dynamic Link", "Small Text", "Select"}


def ensure_doctype(client: FrappeClient, verbose: bool = True, repair: bool = True) -> dict:
    """Create the DocType if it is absent. Idempotent.

    If it already exists, this does not silently trust it: the live field set
    is diffed against _field_spec(). Missing fields are added. A field whose
    type has drifted is corrected only when the change is column-compatible
    (see _SAFE_RETYPE) — otherwise it is reported and left alone, because
    changing a column type under existing rows is a migration, not a fixup.
    """
    report = {"created": False, "added_fields": [], "removed_fields": [],
              "retyped": [], "type_drift": [], "existing": True}
    if not client.exists("DocType", DOCTYPE):
        client.insert(_doctype_spec())
        report["created"] = True
        report["existing"] = False
        if verbose:
            print(f"  created DocType {DOCTYPE!r} (custom=1, submittable)")
        return report

    live = client.get_doc("DocType", DOCTYPE)
    live_fields = {f["fieldname"]: f for f in live.get("fields", [])}
    want = _field_spec()
    missing = [f for f in want if f["fieldname"] not in live_fields]
    dirty = False

    # Drop columns this module used to define. Only names in OBSOLETE_FIELDS are
    # touched — never an unrecognised field, which might belong to someone else.
    stale = [f for f in OBSOLETE_FIELDS if f in live_fields]
    if stale:
        live["fields"] = [f for f in live["fields"] if f["fieldname"] not in stale]
        report["removed_fields"] = stale
        dirty = True

    for f in want:
        got = live_fields.get(f["fieldname"])
        if not got or got.get("fieldtype") == f["fieldtype"]:
            continue
        drift = {"fieldname": f["fieldname"], "live": got.get("fieldtype"),
                 "expected": f["fieldtype"]}
        if repair and got.get("fieldtype") in _SAFE_RETYPE and f["fieldtype"] in _SAFE_RETYPE:
            got["fieldtype"] = f["fieldtype"]
            got["options"] = f.get("options")
            report["retyped"].append(drift)
            dirty = True
        else:
            report["type_drift"].append(drift)

    if missing:
        live["fields"] = live.get("fields", []) + missing
        report["added_fields"] = [f["fieldname"] for f in missing]
        dirty = True

    if dirty:
        retry_transient(lambda: client.call("frappe.client.save", doc=json.dumps(live)))
        if verbose:
            if report["added_fields"]:
                print(f"  {DOCTYPE!r} existed; added {report['added_fields']}")
            if report["removed_fields"]:
                print(f"  {DOCTYPE!r}: dropped obsolete {report['removed_fields']}")
            for d in report["retyped"]:
                print(f"  {DOCTYPE!r}: retyped {d['fieldname']} "
                      f"{d['live']} -> {d['expected']}")
    elif verbose:
        print(f"  DocType {DOCTYPE!r} present and matches spec")
    if report["type_drift"] and verbose:
        print(f"  WARNING type drift, not auto-corrected: {report['type_drift']}")
    return report


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _canon(v: Any) -> str:
    """One canonical text form per value, so the same override always hashes
    the same way. json.dumps with sort_keys handles dicts/lists; floats keep
    repr so 1.0 and 1 stay distinguishable."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _as_num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _record_hash(payload: dict, prev_hash: str, seq: int) -> str:
    body = {k: _canon(payload.get(k)) for k in HASHED_FIELDS}
    body["_schema"] = SCHEMA_VERSION
    body["_seq"] = seq
    body["_prev"] = prev_hash
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _chain_head(client: FrappeClient) -> tuple[int, str]:
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE,
        fields=["seq", "record_hash"], order_by="seq desc", limit_page_length=1) or []
    if not rows:
        return 0, GENESIS
    return int(rows[0]["seq"] or 0), rows[0]["record_hash"] or GENESIS


class UnknownTarget(ValueError):
    """The document an override claims to be about does not exist."""


def read_target(client: FrappeClient, doctype: str, name: str) -> dict:
    """Fetch the *persisted* document. Every record is written from this.

    Called after insert and after submit, so what comes back is the document
    as ERPNext finally saved it — with Pricing Rules, tax templates, currency
    rounding and every other validate-time mechanism already applied. The
    caller's intention is not consulted for anything except `requested_value`.
    """
    try:
        return client.get_doc(doctype, name)
    except FrappeError as e:
        raise UnknownTarget(f"{doctype} {name!r} does not exist: {str(e)[:120]}") from e


def _target_facts(doc: dict) -> dict:
    """Period-defining facts, taken off the saved document rather than the
    caller's payload, so a caller cannot mislabel which period it lands in."""
    return {
        "target_docstatus": doc.get("docstatus"),
        "target_posting_date": doc.get("posting_date") or doc.get("transaction_date")
                               or (doc.get("creation") or "")[:10] or None,
        "target_company": doc.get("company"),
    }


# Sentinel distinguishing "the saved document holds None here" from "the field
# could not be located in the saved document at all". Conflating them would let
# an unreadable field masquerade as a clean, drift-free override.
UNREADABLE = object()


def _stored_value(doc: dict, fieldname: str, row_idx: int) -> Any:
    """Read one field back out of the saved document.

    row_idx < 0 means the override was on the parent. Otherwise the field lives
    in a child table, and which table that is depends on the doctype — `items`
    on the sales/purchase documents, `references` on Payment Entry, and so on.
    Rather than hard-coding a map that goes stale, find the table by looking for
    the one whose rows actually carry the field.
    """
    if row_idx is None or row_idx < 0:
        return doc.get(fieldname, UNREADABLE)

    candidates = [
        k for k, v in doc.items()
        if isinstance(v, list) and v and isinstance(v[0], dict) and fieldname in v[0]
    ]
    if not candidates:
        return UNREADABLE
    # `items` wins a tie: on documents that have several tables carrying `rate`,
    # it is the line table the intent layer indexes into.
    table = "items" if "items" in candidates else candidates[0]
    rows = doc[table]
    if row_idx >= len(rows):
        return UNREADABLE
    return rows[row_idx].get(fieldname, UNREADABLE)


def _assess_drift(requested: Any, stored: Any, rnum: float | None,
                  snum: float | None) -> tuple[str, float | None, str | None]:
    """Compare what was asked for against what was saved.

    Returns (drift, drift_delta, drift_note). Numeric comparison uses a
    half-cent tolerance because ERPNext rounds to the document's currency
    precision and a 0.004 difference is rounding, not an override being
    overridden.
    """
    if stored is UNREADABLE:
        return ("unverified", None,
                "the field could not be located in the saved document; no claim is "
                "made about whether the requested value took effect")
    if rnum is not None and snum is not None:
        delta = snum - rnum
        if abs(delta) < 0.005:
            return "none", 0.0, None
        return ("changed", delta,
                f"the saved document holds {snum} where {rnum} was requested "
                f"(delta {delta:+.6g}). Another ERPNext mechanism — a Pricing Rule, "
                f"a tax or currency rule, or a server hook — overrode the recorded "
                f"decision after the intent layer applied it.")
    if _canon(requested) == _canon(stored):
        return "none", None, None
    return ("changed", None,
            f"the saved document holds {_canon(stored)!r} where "
            f"{_canon(requested)!r} was requested. Another ERPNext mechanism "
            f"overrode the recorded decision after the intent layer applied it.")


def record_override(
    client: FrappeClient,
    *,
    target_doctype: str,
    target_name: str,
    fieldname: str,
    derived_value: Any,
    requested_value: Any,
    reason: str,
    actor: str,
    row_idx: int = 0,
    intent: str | None = None,
    actor_kind: str = "Agent",
    saved_doc: dict | None = None,
) -> dict:
    """Write one immutable override record. Returns the saved, submitted doc.

    The record is built from the PERSISTED target document, never from what the
    caller intended. `saved_doc` is the target as ERPNext stored it; if it is
    not supplied it is fetched here. `stored_value` and every drift assessment
    come from that document, and `requested_value` is the only field the caller
    gets to determine.

    Insert-then-submit is two HTTP calls, which means a crash between them can
    leave a draft. A draft record is *visible* to every query below (they do
    not filter on docstatus) and is flagged by verify_chain as unsubmitted, so
    the failure mode is a loud one, not a lost one.
    """
    if not (reason or "").strip():
        raise ValueError("an override record requires a reason")
    if not (actor or "").strip():
        raise ValueError("an override record requires an actor")

    doc = saved_doc if saved_doc is not None else read_target(
        client, target_doctype, target_name)
    facts = _target_facts(doc)

    stored_value = _stored_value(doc, fieldname, row_idx)
    dnum = _as_num(derived_value)
    rnum = _as_num(requested_value)
    snum = None if stored_value is UNREADABLE else _as_num(stored_value)
    drift, drift_delta, drift_note = _assess_drift(
        requested_value, stored_value, rnum, snum)

    payload = {
        "doctype": DOCTYPE,
        "target_doctype": target_doctype,
        "target_name": target_name,
        "row_idx": row_idx,
        "fieldname": fieldname,
        "derived_value": _canon(derived_value),
        "requested_value": _canon(requested_value),
        "stored_value": None if stored_value is UNREADABLE else _canon(stored_value),
        "is_numeric": 1 if (dnum is not None and snum is not None) else 0,
        "derived_num": dnum,
        "requested_num": rnum,
        "stored_num": snum,
        # stored - derived: what actually reached the books, not what was asked
        # for. When a Pricing Rule overrides the override, this is the rule's
        # effect, which is the number that ties to the ledger.
        "value_delta": (snum - dnum) if (dnum is not None and snum is not None) else None,
        "drift": drift,
        "drift_delta": drift_delta,
        "drift_note": drift_note,
        "reason": reason.strip(),
        "intent": intent,
        "actor": actor,
        "actor_kind": actor_kind,
        "actor_user": _session_user(client),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
        "schema_version": SCHEMA_VERSION,
        **{k: v for k, v in facts.items() if v is not None},
    }

    seq, prev = _chain_head(client)
    payload["seq"] = seq + 1
    payload["prev_hash"] = prev
    payload["record_hash"] = _record_hash(payload, prev, seq + 1)

    saved = retry_transient(lambda: client.insert(payload))
    return retry_transient(lambda: client.submit(saved))


def retry_transient(fn, attempts: int = 5, base_delay: float = 0.15):
    """Retry a write through a transient MariaDB contention error.

    Frappe allocates document names from a single `tabSeries` row per series,
    so two writers inserting at the same instant collide:

        QueryDeadlockError (1020, "Record has changed since last read in
        table 'tabSeries'; try restarting transaction")

    Observed live while a sibling process was driving the same instance. This
    is retry-safe: the transaction rolled back, so nothing partial survived,
    and the retry takes a fresh series number. It is deliberately narrow —
    only contention errors are retried, never a validation failure.
    """
    transient = ("QueryDeadlockError", "1020", "Deadlock found",
                 "Lock wait timeout", "QueryTimeoutError")
    for i in range(attempts):
        try:
            return fn()
        except FrappeError as e:
            if i == attempts - 1 or not any(t in str(e) for t in transient):
                raise
            time.sleep(base_delay * (2 ** i))


_USER_CACHE: dict[int, str] = {}


def _session_user(client: FrappeClient) -> str | None:
    key = id(client)
    if key not in _USER_CACHE:
        try:
            _USER_CACHE[key] = client.call("frappe.auth.get_logged_user")
        except FrappeError:
            _USER_CACHE[key] = None
    return _USER_CACHE[key]


def record_result(client: FrappeClient, result: Any, actor: str,
                  actor_kind: str = "Agent") -> list[dict]:
    """Adopt an IntentResult wholesale.

    This is the entire integration surface for harness/intent.py. Where it
    currently builds a semicolon string into `doc["remarks"]`, it would instead
    call this after the insert/submit returns:

        audit.record_result(self.client, res, actor=caller_identity)

    **Call it after submit, not after insert.** The target document is read
    back once here and every record in the batch is built from that read. If it
    happens before submit, the read misses anything the submit path changes and
    every record in the batch is a claim about a document state that no longer
    exists.

    Deliberately duck-typed on `.doctype`, `.name`, `.intent`, `.overrides`, so
    it needs no import from intent.py and creates no cycle. `result.overrides`
    may carry either `.requested_value` or the older `.supplied_value` — the
    intent engine's `Override` dataclass currently uses the latter.
    """
    if not getattr(result, "overrides", None) or not getattr(result, "name", None):
        return []
    doc = read_target(client, result.doctype, result.name)
    out = []
    for o in result.overrides:
        requested = getattr(o, "requested_value", None)
        if requested is None:
            requested = getattr(o, "supplied_value", None)
        out.append(record_override(
            client,
            target_doctype=result.doctype,
            target_name=result.name,
            fieldname=o.fieldname,
            derived_value=o.derived_value,
            requested_value=requested,
            reason=o.reason,
            row_idx=getattr(o, "row_idx", 0),
            intent=getattr(result, "intent", None),
            actor=actor,
            actor_kind=actor_kind,
            saved_doc=doc,
        ))
    return out


# --------------------------------------------------------------------------
# querying — the point of the whole exercise
# --------------------------------------------------------------------------

LIST_FIELDS = [
    "name", "seq", "target_doctype", "target_name", "row_idx", "target_docstatus",
    "target_posting_date", "target_company", "fieldname",
    "derived_value", "requested_value", "stored_value",
    "derived_num", "requested_num", "stored_num", "value_delta", "is_numeric",
    "drift", "drift_delta", "drift_note",
    "reason", "intent", "actor", "actor_kind", "actor_user", "recorded_at",
    "record_hash", "prev_hash", "docstatus",
]

_BASIS = {"posting": "target_posting_date", "recorded": "recorded_at"}


def overrides_in_period(
    client: FrappeClient,
    from_date: str,
    to_date: str,
    *,
    basis: str = "posting",
    fieldname: str | None = None,
    target_doctype: str | None = None,
    actor: str | None = None,
    company: str | None = None,
    drift: str | list[str] | None = None,
    resolve: bool = True,
) -> list[dict]:
    """"Show me every override on any document in period X, by field, with
    reason and who made it." — one call, one round trip.

    `basis` picks which clock the period means. "posting" is the accounting
    period the voucher lands in (the default, and what an auditor means).
    "recorded" is when the API call happened; the two diverge on every
    backdated document, which is exactly when you want to be able to ask both.
    """
    if basis not in _BASIS:
        raise ValueError(f"basis must be one of {sorted(_BASIS)}")
    col = _BASIS[basis]
    hi = f"{to_date} 23:59:59" if basis == "recorded" and len(to_date) == 10 else to_date
    filters: dict[str, Any] = {col: ["between", [from_date, hi]]}
    if fieldname:
        filters["fieldname"] = fieldname
    if target_doctype:
        filters["target_doctype"] = target_doctype
    if actor:
        filters["actor"] = actor
    if company:
        filters["target_company"] = company
    if drift:
        filters["drift"] = ["in", [drift] if isinstance(drift, str) else list(drift)]
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE, filters=filters,
        fields=LIST_FIELDS, order_by="target_posting_date asc, seq asc",
        limit_page_length=0) or []
    return resolve_targets(client, rows) if resolve else rows


def overrides_for_doc(client: FrappeClient, target_doctype: str, target_name: str,
                      *, follow_amendments: bool = True, resolve: bool = True) -> list[dict]:
    """Every override on one voucher.

    With follow_amendments, this walks the ERPNext amendment lineage
    (`amended_from`) in both directions and returns the overrides for the whole
    family. Amending is the standard way to "fix" a submitted document, and an
    override that was on SINV-0007 before it was cancelled and re-issued as
    SINV-0007-1 is very much part of SINV-0007-1's history.
    """
    names = [target_name]
    if follow_amendments:
        names = _amendment_family(client, target_doctype, target_name)
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE,
        filters={"target_doctype": target_doctype, "target_name": ["in", names]},
        fields=LIST_FIELDS, order_by="seq asc", limit_page_length=0) or []
    return resolve_targets(client, rows) if resolve else rows


def _amendment_family(client: FrappeClient, doctype: str, name: str) -> list[str]:
    seen, frontier = {name}, [name]
    while frontier:
        cur = frontier.pop()
        try:
            doc = client.call("frappe.client.get_value", doctype=doctype,
                              filters={"name": cur}, fieldname="amended_from") or {}
        except FrappeError:
            doc = {}
        parent = doc.get("amended_from")
        if parent and parent not in seen:
            seen.add(parent); frontier.append(parent)
        kids = client.call("frappe.client.get_list", doctype=doctype,
                           filters={"amended_from": cur}, fields=["name"],
                           limit_page_length=0) or []
        for k in kids:
            if k["name"] not in seen:
                seen.add(k["name"]); frontier.append(k["name"])
    return sorted(seen)


def overrides_with_drift(client: FrappeClient, from_date: str | None = None,
                        to_date: str | None = None, *, basis: str = "posting",
                        include_unverified: bool = True,
                        resolve: bool = True) -> list[dict]:
    """"Show me every override that did not actually take effect."

    An override whose `drift` is "changed" was recorded, reasoned, attributed —
    and then overwritten by something else before the document was saved. The
    books do not reflect the decision the log describes. That is a distinct and
    more alarming class of finding than an override that simply happened, and
    it is why `drift` is an indexed, filterable column rather than a note.

    `include_unverified` also returns records where the field could not be read
    back at all. Those are not evidence of drift; they are evidence that no
    assessment was possible, and an auditor should see them rather than have
    them quietly counted as clean.
    """
    wanted = ["changed"] + (["unverified"] if include_unverified else [])
    filters: dict[str, Any] = {"drift": ["in", wanted]}
    if from_date and to_date:
        filters[_BASIS[basis]] = ["between", [from_date, to_date]]
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE, filters=filters,
        fields=LIST_FIELDS, order_by="target_posting_date asc, seq asc",
        limit_page_length=0) or []
    return resolve_targets(client, rows) if resolve else rows


def overrides_by_drift(client: FrappeClient, from_date: str | None = None,
                       to_date: str | None = None, *, basis: str = "posting") -> list[dict]:
    """Aggregate: how many overrides in the period actually took effect."""
    filters: dict[str, Any] = {}
    if from_date and to_date:
        filters[_BASIS[basis]] = ["between", [from_date, to_date]]
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE, filters=filters,
        fields=["drift", {"COUNT": "name"}, {"SUM": "drift_delta"}],
        group_by="drift", order_by="drift asc", limit_page_length=0) or []
    return [_normalise_agg(r) for r in rows]


def overrides_by_field(client: FrappeClient, from_date: str | None = None,
                       to_date: str | None = None, *, basis: str = "posting") -> list[dict]:
    """Aggregate: count, net delta and distinct-document count per field.

    Done server-side with a GROUP BY. Frappe 16 rejects SQL functions written
    as strings in `fields` (ValidationError: "SQL functions are not allowed as
    strings in SELECT"), so the aggregate goes through the dict form.
    """
    filters: dict[str, Any] = {}
    if from_date and to_date:
        filters[_BASIS[basis]] = ["between", [from_date, to_date]]
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE, filters=filters,
        fields=["fieldname", "target_doctype", "drift", {"COUNT": "name"},
                {"SUM": "value_delta"}, {"MIN": "value_delta"}, {"MAX": "value_delta"},
                {"SUM": "drift_delta"}],
        group_by="fieldname, target_doctype, drift", order_by="fieldname asc",
        limit_page_length=0) or []
    return [_normalise_agg(r) for r in rows]


def overrides_by_actor(client: FrappeClient, from_date: str | None = None,
                       to_date: str | None = None, *, basis: str = "posting") -> list[dict]:
    filters: dict[str, Any] = {}
    if from_date and to_date:
        filters[_BASIS[basis]] = ["between", [from_date, to_date]]
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE, filters=filters,
        fields=["actor", "actor_kind", {"COUNT": "name"}, {"SUM": "value_delta"}],
        group_by="actor, actor_kind", order_by="actor asc", limit_page_length=0) or []
    return [_normalise_agg(r) for r in rows]


_AGG_ALIASES = {
    "count(`name`)": "n", "count(name)": "n", "count_name": "n",
    "sum(`value_delta`)": "net_delta", "sum(value_delta)": "net_delta",
    "min(`value_delta`)": "min_delta", "min(value_delta)": "min_delta",
    "max(`value_delta`)": "max_delta", "max(value_delta)": "max_delta",
    "sum(`drift_delta`)": "net_drift", "sum(drift_delta)": "net_drift",
}


def _normalise_agg(row: dict) -> dict:
    """Frappe names aggregate columns after the SQL expression, which differs
    between versions and quoting styles. Normalise to stable keys so callers
    are not coupled to the backend's spelling."""
    out = {}
    for k, v in row.items():
        out[_AGG_ALIASES.get(k.lower().replace(" ", ""), k)] = v
    return out


# --------------------------------------------------------------------------
# lifecycle resolution — the docstatus question
# --------------------------------------------------------------------------

_STATE = {0: "draft", 1: "submitted", 2: "cancelled"}


def resolve_targets(client: FrappeClient, rows: list[dict]) -> list[dict]:
    """Annotate each override row with the *current* state of its target.

    This is where the cancellation/amendment question is answered. The log
    stores docstatus-at-write and never changes it; the live state is looked up
    now. Each row gains:

        target_state       draft | submitted | cancelled | deleted
        target_amended_by  the successor document, if the target was amended
        lifecycle_note     set when the two disagree

    An override on a document that was later cancelled is not noise to be
    filtered out — cancel-and-reissue at a different price is precisely the
    pattern an auditor is looking for, and it is only visible because the
    record outlived the document.
    """
    if not rows:
        return rows
    by_dt: dict[str, set[str]] = {}
    for r in rows:
        by_dt.setdefault(r["target_doctype"], set()).add(r["target_name"])

    live: dict[tuple[str, str], int] = {}
    amended: dict[tuple[str, str], str] = {}
    for dt, names in by_dt.items():
        got = client.call("frappe.client.get_list", doctype=dt,
                          filters={"name": ["in", sorted(names)]},
                          fields=["name", "docstatus"], limit_page_length=0) or []
        for g in got:
            live[(dt, g["name"])] = g["docstatus"]
        try:
            kids = client.call("frappe.client.get_list", doctype=dt,
                               filters={"amended_from": ["in", sorted(names)]},
                               fields=["name", "amended_from"], limit_page_length=0) or []
        except FrappeError:
            kids = []
        for k in kids:
            amended[(dt, k["amended_from"])] = k["name"]

    for r in rows:
        key = (r["target_doctype"], r["target_name"])
        cur = live.get(key)
        r["target_live_docstatus"] = cur
        r["target_state"] = "deleted" if cur is None else _STATE.get(cur, str(cur))
        r["target_amended_by"] = amended.get(key)
        was = r.get("target_docstatus")
        note = None
        if cur is None:
            note = "target document no longer exists; the override record survives it"
        elif was is not None and cur != was:
            note = f"target moved {_STATE.get(was, was)} -> {_STATE.get(cur, cur)} since the override"
        if r.get("target_amended_by"):
            note = ((note + "; ") if note else "") + f"amended by {r['target_amended_by']}"
        r["lifecycle_note"] = note
    return rows


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------

def verify_chain(client: FrappeClient) -> dict:
    """Recompute every record hash and re-walk the chain.

    Detects, in order of how much they should worry you:
      tampered   a record whose content no longer hashes to its stored hash
      broken     a record whose prev_hash is not its predecessor's hash
                 (which is what a deleted record leaves behind)
      forked     two records claiming the same predecessor — see the caveat
      unsubmitted a record left at docstatus 0 by a crash between insert
                 and submit

    Honest limit: the hash is computed by the same process that writes the row,
    so this proves nothing against an adversary who can run this module's code
    against the database directly — they can rewrite the tail and rehash it.
    What it does prove is that no *point* edit survived, which covers the
    realistic threat: a SQL UPDATE on one embarrassing row.
    """
    rows = client.call(
        "frappe.client.get_list", doctype=DOCTYPE,
        fields=LIST_FIELDS + ["schema_version"], order_by="seq asc",
        limit_page_length=0) or []
    out = {"records": len(rows), "tampered": [], "broken": [], "forked": [],
           "unsubmitted": [], "ok": True, "head": None}
    claimed: dict[str, str] = {}
    prev_hash, prev_seq = GENESIS, 0
    for r in rows:
        if r.get("docstatus") != 1:
            out["unsubmitted"].append(r["name"])
        want = _record_hash(r, r.get("prev_hash") or GENESIS, int(r.get("seq") or 0))
        if want != (r.get("record_hash") or ""):
            out["tampered"].append({"name": r["name"], "stored": r.get("record_hash"),
                                    "recomputed": want})
        if (r.get("prev_hash") or GENESIS) != prev_hash:
            out["broken"].append({"name": r["name"], "seq": r.get("seq"),
                                  "expected_prev": prev_hash,
                                  "stored_prev": r.get("prev_hash")})
        p = r.get("prev_hash") or GENESIS
        if p in claimed:
            out["forked"].append({"name": r["name"], "also_claimed_by": claimed[p]})
        claimed[p] = r["name"]
        prev_hash, prev_seq = r.get("record_hash") or "", int(r.get("seq") or 0)
    out["head"] = {"seq": prev_seq, "hash": prev_hash} if rows else None
    out["ok"] = not (out["tampered"] or out["broken"] or out["forked"] or out["unsubmitted"])
    return out


# --------------------------------------------------------------------------

def format_rows(rows: Iterable[dict]) -> str:
    """Fixed-width rendering for the demo and for CLI use."""
    rows = list(rows)
    if not rows:
        return "  (none)"
    hdr = f"  {'DATE':<11}{'TARGET':<28}{'ROW':>4}  {'FIELD':<10}{'DERIVED':>10}{'->':^4}{'SUPPLIED':>10}{'DELTA':>10}  {'STATE':<10}{'ACTOR':<26}REASON"
    lines = [hdr, "  " + "-" * (len(hdr) - 2)]
    for r in rows:
        tgt = f"{r['target_doctype'][:2].upper()} {r['target_name']}"
        lines.append(
            f"  {str(r.get('target_posting_date') or '')[:10]:<11}"
            f"{tgt[:27]:<28}{r.get('row_idx', 0):>4}  {r['fieldname'][:9]:<10}"
            f"{_fmt(r.get('derived_num'), r.get('derived_value')):>10}{'->':^4}"
            f"{_fmt(r.get('supplied_num'), r.get('supplied_value')):>10}"
            f"{_fmt(r.get('value_delta'), ''):>10}  "
            f"{(r.get('target_state') or '?'):<10}{r['actor'][:25]:<26}{r['reason']}")
        if r.get("lifecycle_note"):
            lines.append(f"  {'':<11}  ↳ {r['lifecycle_note']}")
    return "\n".join(lines)


def _fmt(num: Any, fallback: Any) -> str:
    if num is None:
        return str(fallback or "")[:10]
    return f"{float(num):,.2f}"
