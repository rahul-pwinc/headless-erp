"""Phase 7 proof — the override record, end to end against a live instance.

Creates the DocType if absent, drives real overrides through the Phase 4 intent
engine onto real submitted Sales Invoices, records them, then runs the auditor's
queries and prints what comes back. Nothing here is stubbed; every number below
is read out of ERPNext.

    python harness/demo_audit.py [--url http://localhost:8080]

The scenarios are chosen to exercise the awkward cases, not the happy one:
  1  a discount        (rate below list)
  2  a premium         (rate above list — 21.3% of real lines, per Phase 6)
  3  two rows, one doc (proves row_idx is not decoration)
  4  a second actor    (proves "who" is queryable, not just recorded)
  5  a cancelled doc   (proves the record outlives the voucher)
  6  an amendment      (proves the lineage survives cancel-and-reissue)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit                                      # noqa: E402
from client import FrappeClient, FrappeError      # noqa: E402
from fixtures import ensure_all                   # noqa: E402
from intent import IntentEngine                   # noqa: E402

TODAY, LATER = "2026-09-08", "2026-10-08"
BOT = "agent:pricing-bot@1.4"
DESK = "human:priya@finance"
RULE = "=" * 100


def head(n: str) -> None:
    print(f"\n{RULE}\n{n}\n{RULE}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--user", default=os.environ.get("ERPNEXT_USER", "Administrator"))
    ap.add_argument("--password", default=os.environ.get("ERPNEXT_PASSWORD", "admin"))
    ap.add_argument("--out", default="reports/audit_demo.json")
    ap.add_argument("--reset", action="store_true",
                    help="drop the log DocType and every record in it, then rebuild. "
                         "Development affordance only — and a live demonstration of the "
                         "limit documented in docs/AUDIT_TRAIL.md: an Administrator can "
                         "destroy this trail, and nothing inside ERPNext can stop them.")
    args = ap.parse_args()

    c = FrappeClient(args.url, args.user, args.password)
    print(f"connected to {args.url}")

    if args.reset:
        _reset(c)

    head("0.  SCHEMA — create the DocType through the REST API, idempotently")
    r1 = audit.ensure_doctype(c)
    print(f"  first call : created={r1['created']} existing={r1['existing']}")
    r2 = audit.ensure_doctype(c)
    print(f"  second call: created={r2['created']} existing={r2['existing']}  <- idempotent")

    fx = ensure_all(c)
    eng = IntentEngine(c)
    lp = fx["list_price"]
    hdr = {"customer": fx["customer"], "company": fx["company"], "currency": "INR",
           "conversion_rate": 1, "selling_price_list": fx["price_list"],
           "price_list_currency": "INR", "plc_conversion_rate": 1,
           "posting_date": TODAY, "due_date": LATER, "update_stock": 0}
    item = fx["item_code"]
    print(f"\n  fixtures: item {item} lists at {lp} on {fx['price_list']}")

    head("1.  WRITE — six real overrides on real submitted documents")
    written = []

    def run(label, lines, overrides, actor, kind="Agent"):
        # The instance is shared with other processes during development, and
        # ERPNext allocates invoice names from a single `tabSeries` row, so an
        # insert can lose a deadlock. Same retry the audit writer uses.
        res = audit.retry_transient(
            lambda: eng.execute("bill", hdr, lines, overrides=overrides))
        recs = audit.record_result(c, res, actor=actor, actor_kind=kind)
        print(f"  {label}")
        print(f"      {res.doctype} {res.name}  docstatus={res.docstatus} "
              f"grand_total={res.grand_total}  invariant_failures={len(res.invariant_failures)}")
        for rec in recs:
            print(f"      -> {rec['name']}  {rec['fieldname']} row {rec['row_idx']}  "
                  f"{rec['derived_num']} -> {rec['supplied_num']}  "
                  f"delta {rec['value_delta']}  docstatus={rec['docstatus']}")
        written.append((res, recs))
        return res

    run("a. discount: sold below list",
        [{"item_code": item, "qty": 4}],
        [{"field": "rate", "value": round(lp * 0.6, 2),
          "reason": "goodwill credit, approved by finance (ticket FIN-8812)"}], BOT)

    run("b. premium: sold above list",
        [{"item_code": item, "qty": 2}],
        [{"field": "rate", "value": round(lp * 1.35, 2),
          "reason": "expedited freight priced into the line, per contract clause 7"}], BOT)

    run("c. two rows on one document, only row 1 overridden",
        [{"item_code": item, "qty": 1}, {"item_code": item, "qty": 3}],
        [{"field": "rate", "value": round(lp * 0.5, 2), "row": 1,
          "reason": "volume tier 3 applied manually; price list not yet updated"}], BOT)

    run("d. a different actor, a human at the Desk",
        [{"item_code": item, "qty": 5}],
        [{"field": "rate", "value": round(lp * 0.9, 2),
          "reason": "matched competitor quote, verbal approval from CFO"}], DESK, "Human")

    to_cancel = run("e. an override on a document about to be cancelled",
                    [{"item_code": item, "qty": 6}],
                    [{"field": "rate", "value": round(lp * 0.25, 2),
                      "reason": "keyed from the wrong contract; will be reissued"}], BOT)

    head("2.  LIFECYCLE — cancel that document, then amend it")
    c.call("frappe.client.cancel", doctype="Sales Invoice", name=to_cancel.name)
    print(f"  cancelled {to_cancel.name}")
    print(f"  the override record for it still exists: "
          f"{len(audit.overrides_for_doc(c, 'Sales Invoice', to_cancel.name, follow_amendments=False))} row(s)")

    amended = _amend(c, to_cancel.name, eng, hdr, item, lp)
    if amended:
        print(f"  amended into {amended.name} (amended_from={to_cancel.name})")
        recs = audit.record_result(c, amended, actor=BOT)
        for rec in recs:
            print(f"      -> {rec['name']}  {rec['fieldname']}  "
                  f"{rec['derived_num']} -> {rec['supplied_num']}")
        written.append((amended, recs))

    # ----------------------------------------------------------------- queries
    head("3.  QUERY — 'every override on any document in period X, "
         "by field, with reason and who made it'")
    print(f"  audit.overrides_in_period(client, '2026-09-01', '2026-09-30')\n")
    period = audit.overrides_in_period(c, "2026-09-01", "2026-09-30")
    print(audit.format_rows(period))
    print(f"\n  {len(period)} override(s) in the period.")

    head("4.  QUERY — narrowed: one field, one actor")
    print("  audit.overrides_in_period(..., fieldname='rate', actor='agent:pricing-bot@1.4')\n")
    print(audit.format_rows(
        audit.overrides_in_period(c, "2026-09-01", "2026-09-30",
                                  fieldname="rate", actor=BOT)))

    head("5.  QUERY — aggregate by field (server-side GROUP BY)")
    print("  audit.overrides_by_field(client, '2026-09-01', '2026-09-30')\n")
    by_field = audit.overrides_by_field(c, "2026-09-01", "2026-09-30")
    print(f"  {'FIELD':<16}{'TARGET DOCTYPE':<20}{'COUNT':>7}{'NET DELTA':>14}"
          f"{'MIN':>12}{'MAX':>12}")
    print("  " + "-" * 79)
    for row in by_field:
        print(f"  {row.get('fieldname', ''):<16}{row.get('target_doctype', ''):<20}"
              f"{row.get('n', 0):>7}{float(row.get('net_delta') or 0):>14,.2f}"
              f"{float(row.get('min_delta') or 0):>12,.2f}"
              f"{float(row.get('max_delta') or 0):>12,.2f}")
    print("\n  by actor:")
    for row in audit.overrides_by_actor(c, "2026-09-01", "2026-09-30"):
        print(f"    {row.get('actor', ''):<28}{row.get('actor_kind', ''):<8}"
              f"n={row.get('n', 0):<4} net_delta={float(row.get('net_delta') or 0):,.2f}")

    head("6.  QUERY — one document's history, following the amendment lineage")
    fam = audit.overrides_for_doc(c, "Sales Invoice", to_cancel.name)
    print(f"  audit.overrides_for_doc(client, 'Sales Invoice', '{to_cancel.name}')\n")
    print(audit.format_rows(fam))

    head("7.  IMMUTABILITY — try to edit, then delete, a submitted record")
    if period:
        victim = c.get_doc(audit.DOCTYPE, period[0]["name"])
        for what, fn in (
            ("edit the reason", lambda: _edit(c, victim, "reason", "actually it was fine")),
            ("edit the supplied value", lambda: _edit(c, victim, "supplied_num", 0.01)),
            ("DELETE the record", lambda: _delete(c, victim["name"])),
        ):
            print(f"  {what:<30} -> {fn()}")

    head("8.  INTEGRITY — recompute the hash chain")
    v = audit.verify_chain(c)
    print(f"  records={v['records']}  ok={v['ok']}")
    print(f"  tampered={len(v['tampered'])} broken={len(v['broken'])} "
          f"forked={len(v['forked'])} unsubmitted={len(v['unsubmitted'])}")
    print(f"  head: seq={v['head']['seq']} hash={v['head']['hash']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"period": period, "by_field": by_field, "chain": v}, fh,
                  indent=2, default=str)
    print(f"\n  full rows written to {args.out}")
    return 0


def _reset(c):
    """Cancel, delete, drop. Only Administrator can do this, and that is the
    point: role permissions say cancel=0 and delete=0 for everyone, but
    Administrator is not subject to role permissions."""
    n = 0
    for row in (c.call("frappe.client.get_list", doctype=audit.DOCTYPE,
                       fields=["name", "docstatus"], limit_page_length=0) or []):
        if row["docstatus"] == 1:
            c.call("frappe.client.cancel", doctype=audit.DOCTYPE, name=row["name"])
        c.session.delete(f"{c.base_url}/api/resource/{audit.DOCTYPE}/{row['name']}")
        n += 1
    c.session.delete(f"{c.base_url}/api/resource/DocType/{audit.DOCTYPE}")
    print(f"  --reset: destroyed {n} record(s) and the DocType itself")


def _amend(c, name, eng, hdr, item, lp):
    """ERPNext amendment: copy the cancelled doc, set amended_from, re-submit.
    Done through the intent engine so the new document's override is recorded
    the same way any other is."""
    try:
        res = eng.execute("bill", {**hdr, "amended_from": name},
                          [{"item_code": item, "qty": 6}],
                          overrides=[{"field": "rate", "value": round(lp * 0.75, 2),
                                      "reason": "reissue of the mis-keyed invoice, correct contract rate"}])
        return res
    except (FrappeError, Exception) as e:  # noqa: BLE001 - reported, not swallowed
        print(f"  amendment skipped: {str(e).splitlines()[0][:160]}")
        return None


def _edit(c, doc, field, value):
    d = dict(doc); d[field] = value
    try:
        c.call("frappe.client.save", doc=json.dumps(d))
        return "!!! ACCEPTED — the record is NOT immutable"
    except FrappeError as e:
        return f"refused: {_exc(e)}"


def _delete(c, name):
    r = c.session.delete(f"{c.base_url}/api/resource/{audit.DOCTYPE}/{name}")
    if r.status_code < 300:
        return "!!! DELETED — the record is NOT durable"
    try:
        return f"refused [{r.status_code}]: {r.json().get('exception', '')[:150]}"
    except Exception:  # noqa: BLE001
        return f"refused [{r.status_code}]"


def _exc(e):
    for part in str(e).split("\\n"):
        if "Error:" in part:
            return part.strip()[:150]
    return str(e).splitlines()[1][:150] if len(str(e).splitlines()) > 1 else str(e)[:150]


if __name__ == "__main__":
    raise SystemExit(main())
