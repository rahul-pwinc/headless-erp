# Enforced `bill` intent.
#
# Two design notes, both learned the hard way.
#
# 1. The audit record is written from the SAVED document, never from what this
#    endpoint intended to write. ERPNext applies Pricing Rules during validate,
#    AFTER the rate is set, so a caller's declared override can be silently
#    replaced by a rule. An audit record built from intent would then assert a
#    decision that never took effect.
#
# 2. This runs under RestrictedPython. No imports, no json, no
#    frappe.parse_json, no frappe.as_json, no frappe.generate_hash. What IS
#    available, verified by probing this sandbox rather than by assuming:
#        frappe.session.user          frappe.utils.now()
#        frappe.utils.sha256_hash()   frappe.db.get_value()
#        frappe.get_all(..., order_by=..., limit_page_length=...)
#        frappe.get_doc({...}).insert(ignore_permissions=True)  and  .submit()
#    That is enough to write real, submitted, hash-chained Intent Override Log
#    records — the same doctype and the same chain the library path writes to,
#    not a Comment approximating one.
#
# The hash below must agree byte for byte with harness/audit.py::_blob_v3.
# That is why the canonical encoding is length-prefixed rather than JSON: a
# sandbox with no json module cannot reproduce json.dumps' ensure_ascii
# escaping, and a hash that disagrees would report tampering that did not
# happen. Length prefixes need no escaping at all.

args = frappe.form_dict

# ---------------------------------------------------------------------------
# Identity scope. The elevation below (ignore_permissions) is deliberate: the
# constrained role has no write permission on Sales Invoice, so an endpoint that
# runs elevated is the mechanism, not a shortcut. But elevation without a scope
# means this identity could bill on behalf of any company on the site. The
# allowed company is written in at provisioning time by harness/enforce.py and
# anything else is refused before a document is built.
ALLOWED_COMPANY = "__ALLOWED_COMPANY__"

if ALLOWED_COMPANY and args.get("company") != ALLOWED_COMPANY:
    frappe.throw("this identity may only bill for " + ALLOWED_COMPANY
                 + ", not " + str(args.get("company")))

items = args.get("items") or []
overrides = args.get("overrides") or []
price_list = args.get("selling_price_list") or "Standard Selling"
REFUSED_LINE = ["price_list_rate", "discount_amount", "discount_percentage",
                "weight_per_unit", "conversion_factor", "income_account", "expense_account"]
# Header-level refusals. conversion_rate is the highest-impact field in the
# contract: assert 1.0 on a USD invoice and it posts at 1/94th of its value,
# balanced. An exchange rate is a published fact, so there is no override case.
REFUSED_HEADER = ["conversion_rate", "plc_conversion_rate"]

# Must match harness/audit.py::HASHED_FIELDS exactly, in this order.
HASHED_FIELDS = ["target_doctype", "target_name", "target_docstatus",
                 "target_posting_date", "target_company", "row_idx", "fieldname",
                 "derived_value", "requested_value", "stored_value", "drift",
                 "reason", "intent", "actor", "actor_user", "actor_kind",
                 "recorded_at"]
SCHEMA_VERSION = 3
GENESIS = "0000000000000000000000000000000000000000000000000000000000000000"
AUDIT_DOCTYPE = "Intent Override Log"

for f in REFUSED_HEADER:
    if args.get(f) is not None:
        frappe.throw("may not supply " + f
                     + ": the exchange rate on a date is a published fact, not a "
                     + "commercial decision. Omit it and the server will derive it.")

rows = []
intents = []
idx = 0
for it in items:
    for f in REFUSED_LINE:
        if f in it:
            frappe.throw("line " + str(idx) + " may not supply " + f
                         + ": it is derived, and read-only in the Desk UI")
    if "rate" in it:
        frappe.throw("line " + str(idx)
                     + " may not supply rate directly. Declare it as an override with a reason.")
    lp = frappe.db.get_value("Item Price",
        {"item_code": it.get("item_code"), "price_list": price_list}, "price_list_rate")
    if not lp:
        frappe.throw("no price for " + str(it.get("item_code")) + " in " + price_list
                     + ". Refusing to guess a rate.")
    charged = lp
    reason = ""
    for o in overrides:
        if o.get("row") == idx and o.get("field") == "rate":
            reason = (o.get("reason") or "").strip()
            if not reason:
                frappe.throw("override of rate on line " + str(idx) + " requires a reason")
            charged = o.get("value")
    rows.append({"item_code": it.get("item_code"), "qty": it.get("qty"), "rate": charged})
    intents.append({"row": idx, "item": it.get("item_code"), "derived": lp,
                    "intended": charged, "reason": reason})
    idx = idx + 1

doc = frappe.get_doc({
    "doctype": "Sales Invoice", "customer": args.get("customer"),
    "company": args.get("company"), "selling_price_list": price_list,
    "posting_date": args.get("posting_date"), "due_date": args.get("due_date"),
    "items": rows,
})
doc.insert(ignore_permissions=True)

# Submit. Leaving the document at docstatus 0 was a real hole: the control
# checked drift once at insert and then walked away, leaving a draft window in
# which anyone with a normal role could edit the rate and submit, while the
# audit record still described the draft. Submitting here closes that window
# for documents this endpoint creates.
#
# It also matters that validate runs AGAIN on submit, so a Pricing Rule can fire
# a second time and move a rate after the insert-time reconciliation. That is
# why the reconciliation below reads doc.items AFTER the submit, not before.
# insert() takes ignore_permissions as a parameter; submit() does not. It reads
# the flag off the document. Calling doc.submit() without setting it leaves the
# document at docstatus 0 and does NOT raise, so the endpoint reported success
# while every invoice it wrote stayed a draft. Silent no-op, exactly the failure
# mode this project is about.
doc.flags.ignore_permissions = True
doc.submit()
doc.reload()

# ---------------------------------------------------------------------------
# Reconcile intent against what was actually persisted, post-submit.
# ---------------------------------------------------------------------------
lines = []
drift_rows = []
for i in intents:
    stored = doc.items[i["row"]].rate
    rec = {"row": i["row"], "item": i["item"], "derived": i["derived"],
           "intended": i["intended"], "stored": stored, "reason": i["reason"]}
    # Half-cent tolerance, matching audit.py::_assess_drift: ERPNext rounds to
    # the document's currency precision, and 0.004 is rounding, not an override
    # being overridden.
    gap = stored - i["intended"]
    if gap < 0:
        gap = 0 - gap
    if gap >= 0.005:
        rec["drift"] = "changed"
        rec["drift_delta"] = stored - i["intended"]
        rec["drift_note"] = ("the saved document holds " + str(stored) + " where "
                             + str(i["intended"]) + " was requested. Another ERPNext "
                             + "mechanism overrode the recorded decision after the "
                             + "intent layer applied it.")
        drift_rows.append(rec)
    else:
        rec["drift"] = "none"
        rec["drift_delta"] = 0.0
        rec["drift_note"] = None
    lines.append(rec)


def canon(v):
    # Mirrors harness/audit.py::_canon for the value types this path produces.
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def blob_v3(payload, prev_hash, seq):
    parts = ["iol", "v3", str(seq), prev_hash]
    for k in HASHED_FIELDS:
        val = canon(payload.get(k))
        parts.append(k + "=" + str(len(val)) + ":" + val)
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Write one Intent Override Log record per override. Same doctype, same chain,
# same immutability (insert then submit) as the library path.
# ---------------------------------------------------------------------------
written = []
audit_error = None
for rc in lines:
    if not rc["reason"] and rc["drift"] == "none":
        continue
    try:
        head = frappe.get_all(AUDIT_DOCTYPE, fields=["seq", "record_hash"],
                              order_by="seq desc", limit_page_length=1)
        if head:
            seq = (head[0].get("seq") or 0) + 1
            prev = head[0].get("record_hash") or GENESIS
        else:
            seq = 1
            prev = GENESIS

        payload = {
            "doctype": AUDIT_DOCTYPE,
            "target_doctype": "Sales Invoice",
            "target_name": doc.name,
            # int() on purpose: docstatus is an IntEnum in Frappe 16 and str()
            # of an enum is not str() of an int, which would break the hash.
            "target_docstatus": int(doc.docstatus),
            "target_posting_date": str(doc.posting_date),
            "target_company": doc.company,
            "row_idx": rc["row"],
            "fieldname": "rate",
            "derived_value": canon(rc["derived"]),
            "requested_value": canon(rc["intended"]),
            "stored_value": canon(rc["stored"]),
            "is_numeric": 1,
            "derived_num": rc["derived"],
            "requested_num": rc["intended"],
            "stored_num": rc["stored"],
            "value_delta": rc["stored"] - rc["derived"],
            "drift": rc["drift"],
            "drift_delta": rc["drift_delta"],
            "drift_note": rc["drift_note"],
            "reason": rc["reason"] or "(no reason: recorded because the stored value drifted)",
            "intent": "bill",
            "actor": "endpoint:bill_intent",
            "actor_kind": "Agent",
            "actor_user": frappe.session.user,
            "recorded_at": frappe.utils.now(),
            "schema_version": SCHEMA_VERSION,
            "seq": seq,
            "prev_hash": prev,
        }
        payload["record_hash"] = frappe.utils.sha256_hash(blob_v3(payload, prev, seq))

        log = frappe.get_doc(payload)
        log.insert(ignore_permissions=True)
        log.submit()
        rc["audit_record"] = log.name
        written.append(log.name)
    except Exception as e:
        # Recorded and returned, never swallowed. The library path cancels the
        # document when the audit write fails; this path cannot, because the
        # throw would roll back the whole request transaction including the
        # audit records already written in this loop — so the caller is told,
        # loudly, in the response.
        audit_error = str(e)[:400]
        rc["audit_error"] = audit_error

# Breadcrumb on the invoice, so a human opening it in the Desk can see that an
# override record exists. Separate try: the authoritative record is already
# written and immutable, and losing a convenience pointer must not be reported
# as an audit failure.
if written:
    try:
        frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                        "reference_doctype": "Sales Invoice", "reference_name": doc.name,
                        "content": "Override recorded in Intent Override Log: "
                                   + ", ".join(written)}).insert(ignore_permissions=True)
    except Exception:
        pass

frappe.response["message"] = {"name": doc.name, "grand_total": doc.grand_total,
                              "lines": lines, "drift_detected": len(drift_rows),
                              "audit_records": written, "audit_error": audit_error}
