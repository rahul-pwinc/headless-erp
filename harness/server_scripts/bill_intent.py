
# Enforced `bill` intent.
#
# Design note learned the hard way: the audit record is written from the SAVED
# document, never from what this endpoint intended to write. ERPNext applies
# Pricing Rules during validate, AFTER the rate is set, so a caller's declared
# override can be silently replaced by a rule. An audit record built from
# intent would then assert a decision that never took effect.
args = frappe.form_dict
items = args.get("items") or []
overrides = args.get("overrides") or []
price_list = args.get("selling_price_list") or "Standard Selling"
REFUSED_LINE = ["price_list_rate", "discount_amount", "discount_percentage",
                "weight_per_unit", "conversion_factor", "income_account", "expense_account"]
# Header-level refusals. conversion_rate is the highest-impact field in the
# contract: assert 1.0 on a USD invoice and it posts at 1/94th of its value,
# balanced. An exchange rate is a published fact, so there is no override case.
REFUSED_HEADER = ["conversion_rate", "plc_conversion_rate"]

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

# Reconcile intent against what was actually persisted.
lines = []
drift = []
for i in intents:
    stored = doc.items[i["row"]].rate
    rec = {"row": i["row"], "item": i["item"], "derived": i["derived"],
           "intended": i["intended"], "stored": stored, "reason": i["reason"]}
    if stored != i["intended"]:
        rec["drift"] = "another rule changed this after the intent was applied"
        drift.append(rec)
    lines.append(rec)

for rc in lines:
    if rc["reason"] or rc.get("drift"):
        note = ("OVERRIDE rate row " + str(rc["row"]) + " | list " + str(rc["derived"])
                + " | requested " + str(rc["intended"]) + " | STORED " + str(rc["stored"])
                + " | reason: " + (rc["reason"] or "(none)")
                + " | actor: " + frappe.session.user)
        if rc.get("drift"):
            note = note + " | WARNING: " + rc["drift"]
        frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                        "reference_doctype": "Sales Invoice", "reference_name": doc.name,
                        "content": note}).insert(ignore_permissions=True)

frappe.response["message"] = {"name": doc.name, "grand_total": doc.grand_total,
                              "lines": lines, "drift_detected": len(drift)}
