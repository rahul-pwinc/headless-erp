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
import re
import subprocess
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
    ap.add_argument("--boundary", action="store_true",
                    help="prove the permission boundary that would stop a caller "
                         "bypassing the intent layer: creates a scratch DocType with a "
                         "permlevel-1 field, a restricted user, and shows the field being "
                         "reset. Cleans up after itself.")
    ap.add_argument("--tamper", action="store_true",
                    help="after the queries, edit and delete a record with raw SQL "
                         "straight against MariaDB, bypassing Frappe entirely, and show "
                         "verify_chain() catching both. Needs the docker compose stack.")
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
                  f"derived {rec['derived_num']} / requested {rec['requested_num']} "
                  f"/ STORED {rec['stored_num']}  eff.delta {rec['value_delta']}  "
                  f"drift={rec['drift']}")
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

    # ---------------------------------------------------------------- drift
    head("2.  DRIFT — a Pricing Rule overrides the override, and the record says so")
    print("  ERPNext applies Pricing Rules during validate, AFTER the intent layer has")
    print("  set the rate. A record built from what the caller intended would assert a")
    print("  decision that never took effect. This one is built from the saved document.\n")
    drift_res = _with_pricing_rule(c, lambda: run(
        "f. explicit override of 1.0, with a 10% Pricing Rule live",
        [{"item_code": item, "qty": 4}],
        [{"field": "rate", "value": 1.0,
          "reason": "goodwill credit, approved by finance"}], BOT))
    if drift_res:
        stored = c.get_doc("Sales Invoice", drift_res.name)["items"][0]["rate"]
        print(f"\n  the document actually stored rate={stored}, not the requested 1.0.")
        print(f"  the intent engine still believes it wrote "
              f"{drift_res.overrides[0].supplied_value} — that is exactly the lie the")
        print("  three-value schema exists to catch.")

    head("3.  LIFECYCLE — cancel that document, then amend it")
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
                  f"derived {rec['derived_num']} / requested {rec['requested_num']} "
                  f"/ STORED {rec['stored_num']}  drift={rec['drift']}")

    # ----------------------------------------------------------------- queries
    head("4.  QUERY — 'every override on any document in period X, "
         "by field, with reason and who made it'")
    print(f"  audit.overrides_in_period(client, '2026-09-01', '2026-09-30')\n")
    period = audit.overrides_in_period(c, "2026-09-01", "2026-09-30")
    print(audit.format_rows(period))
    print(f"\n  {len(period)} override(s) in the period.")

    head("5.  QUERY — narrowed: one field, one actor")
    print("  audit.overrides_in_period(..., fieldname='rate', actor='agent:pricing-bot@1.4')\n")
    print(audit.format_rows(
        audit.overrides_in_period(c, "2026-09-01", "2026-09-30",
                                  fieldname="rate", actor=BOT)))

    head("6.  QUERY — aggregate by field (server-side GROUP BY)")
    print("  audit.overrides_by_field(client, '2026-09-01', '2026-09-30')\n")
    by_field = audit.overrides_by_field(c, "2026-09-01", "2026-09-30")
    print(f"  {'FIELD':<10}{'TARGET DOCTYPE':<16}{'DRIFT':<12}{'COUNT':>7}"
          f"{'NET EFF.DELTA':>15}{'MIN':>10}{'MAX':>10}{'NET DRIFT':>12}")
    print("  " + "-" * 92)
    for row in by_field:
        print(f"  {row.get('fieldname', ''):<10}{row.get('target_doctype', ''):<16}"
              f"{row.get('drift', ''):<12}{row.get('n', 0):>7}"
              f"{float(row.get('net_delta') or 0):>15,.2f}"
              f"{float(row.get('min_delta') or 0):>10,.2f}"
              f"{float(row.get('max_delta') or 0):>10,.2f}"
              f"{float(row.get('net_drift') or 0):>12,.2f}")
    print("\n  by actor:")
    for row in audit.overrides_by_actor(c, "2026-09-01", "2026-09-30"):
        print(f"    {row.get('actor', ''):<28}{row.get('actor_kind', ''):<8}"
              f"{row.get('drift', ''):<11}n={row.get('n', 0):<4} "
              f"net_delta={float(row.get('net_delta') or 0):,.2f}")

    head("7.  QUERY — 'show me every override that did not actually take effect'")
    print("  audit.overrides_with_drift(client, '2026-09-01', '2026-09-30')\n")
    drifted = audit.overrides_with_drift(c, "2026-09-01", "2026-09-30")
    print(audit.format_rows(drifted))
    print("\n  by drift state:")
    for row in audit.overrides_by_drift(c, "2026-09-01", "2026-09-30"):
        print(f"    {row.get('drift', ''):<14}n={row.get('n', 0):<4} "
              f"net_drift={float(row.get('net_drift') or 0):,.2f}")

    head("8.  QUERY — one document's history, following the amendment lineage")
    fam = audit.overrides_for_doc(c, "Sales Invoice", to_cancel.name)
    print(f"  audit.overrides_for_doc(client, 'Sales Invoice', '{to_cancel.name}')\n")
    print(audit.format_rows(fam))

    head("9.  IMMUTABILITY — try to edit, then delete, a submitted record")
    if period:
        victim = c.get_doc(audit.DOCTYPE, period[0]["name"])
        for what, fn in (
            ("edit the reason", lambda: _edit(c, victim, "reason", "actually it was fine")),
            ("edit the stored value", lambda: _edit(c, victim, "stored_num", 0.01)),
            # Must differ from what the record already holds: Frappe compares
            # against the stored value, so re-saving an identical value is a
            # no-op that succeeds and would read as a false alarm.
            ("edit the drift flag",
             lambda: _edit(c, victim, "drift",
                           "changed" if victim.get("drift") != "changed" else "none")),
            ("DELETE the record", lambda: _delete(c, victim["name"])),
        ):
            print(f"  {what:<30} -> {fn()}")

    head("10.  INTEGRITY — recompute the hash chain")
    v = audit.verify_chain(c)
    print(f"  records={v['records']}  ok={v['ok']}")
    print(f"  tampered={len(v['tampered'])} broken={len(v['broken'])} "
          f"forked={len(v['forked'])} unsubmitted={len(v['unsubmitted'])}")
    print(f"  head: seq={v['head']['seq']} hash={v['head']['hash']}")

    if args.tamper and period:
        _tamper_demo(c, period)

    if args.boundary:
        _boundary_demo(c, args.url)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"period": period, "by_field": by_field, "chain": v}, fh,
                  indent=2, default=str)
    print(f"\n  full rows written to {args.out}")
    return 0


PROBE_DT = "ZZ Boundary Probe"
PROBE_ROLE = "ZZ Probe Caller"
PROBE_USER = "zz-probe@example.com"
PROBE_PW = "Probe!2026#pw"


def _boundary_demo(c, url):
    """The audit trail records what came through the intent layer. It says
    nothing about a caller that skips the layer and POSTs to /api/resource
    directly. That gap is closed by a permission boundary, not by a log — and
    this shows the boundary working.

    The mechanism is Frappe's field permlevel. A DocField with permlevel > 0 is
    writable only by a role holding write at that permlevel; every other user's
    value is discarded and replaced (document.py::validate_higher_perm_levels
    -> base_document.py::reset_values_if_no_permlevel_access, called from
    Document.insert and Document.save, i.e. on every write path including the
    REST API).

    Proved here on a scratch DocType rather than on Sales Invoice Item.rate,
    because a permlevel Property Setter on Sales Invoice is global, persistent
    metadata and other processes were driving this instance at the time. The
    mechanism is identical; only the target differs.
    """
    head("12.  BOUNDARY — the permlevel gate that stops a caller skipping the intent layer")
    _boundary_cleanup(c)
    if not c.exists("Role", PROBE_ROLE):
        c.insert({"doctype": "Role", "role_name": PROBE_ROLE, "desk_access": 1})
    c.insert({
        "doctype": "DocType", "name": PROBE_DT, "module": "Custom", "custom": 1,
        "autoname": "hash",
        "fields": [
            {"fieldname": "note", "fieldtype": "Data", "label": "Note"},
            # permlevel 1: stands in for Sales Invoice Item.rate
            {"fieldname": "guarded", "fieldtype": "Float", "label": "Guarded", "permlevel": 1},
        ],
        "permissions": [
            {"role": PROBE_ROLE, "permlevel": 0, "read": 1, "write": 1, "create": 1},
            {"role": "System Manager", "permlevel": 0, "read": 1, "write": 1,
             "create": 1, "delete": 1},
            {"role": "System Manager", "permlevel": 1, "read": 1, "write": 1},
        ]})
    print(f"  scratch DocType {PROBE_DT!r}: field 'guarded' at permlevel 1")
    print(f"  role {PROBE_ROLE!r} has write at permlevel 0 only")

    if not c.exists("User", PROBE_USER):
        c.insert({"doctype": "User", "email": PROBE_USER, "first_name": "ZZ Probe",
                  "new_password": PROBE_PW, "send_welcome_email": 0,
                  "roles": [{"role": PROBE_ROLE}]})
    else:
        u = c.get_doc("User", PROBE_USER)
        u["new_password"] = PROBE_PW
        u["roles"] = [{"doctype": "Has Role", "role": PROBE_ROLE}]
        c.call("frappe.client.save", doc=json.dumps(u))

    restricted = FrappeClient(url, PROBE_USER, PROBE_PW)
    d = restricted.insert({"doctype": PROBE_DT, "note": "POSTed straight to /api/resource",
                           "guarded": 999})
    print(f"\n  restricted caller POSTs guarded=999 -> stored {d['guarded']}   "
          f"<- the value never landed")
    print(f"    re-read as Administrator: {c.get_doc(PROBE_DT, d['name'])['guarded']}")

    d2 = c.insert({"doctype": PROBE_DT, "note": "as Administrator", "guarded": 999})
    print(f"  Administrator POSTs guarded=999   -> stored {d2['guarded']}   "
          f"<- permlevel does not apply to Administrator")
    print("    (document.py:1026 — `if frappe.session.user == \"Administrator\": return`)")
    print("\n  Note the failure mode: the write is silently DISCARDED, not refused. A")
    print("  boundary makes the bypass ineffective; making it loud needs a validate")
    print("  hook that rejects rate != price_list_rate without a matching log record.")
    _boundary_cleanup(c)
    print(f"\n  cleaned up: {PROBE_DT!r}, {PROBE_ROLE!r}, {PROBE_USER!r} removed")


def _boundary_cleanup(c):
    for row in (c.call("frappe.client.get_list", doctype=PROBE_DT, fields=["name"],
                       limit_page_length=0) if c.exists("DocType", PROBE_DT) else []) or []:
        c.session.delete(f"{c.base_url}/api/resource/{PROBE_DT}/{row['name']}")
    for dt, name in (("DocType", PROBE_DT), ("User", PROBE_USER), ("Role", PROBE_ROLE)):
        c.session.delete(f"{c.base_url}/api/resource/{dt}/{name}")


def _tamper_demo(c, period):
    """Section 7 proved the *API* cannot alter a submitted record. This proves
    what happens when someone goes underneath it, with a SQL client and the
    site's own database credentials — the strongest attacker the hash chain is
    designed to catch, and the weakest one it cannot stop."""
    sql = _sql_runner()
    if not sql:
        print("\n  --tamper skipped: docker compose stack not reachable")
        return
    head("11.  TAMPER — raw SQL against MariaDB, underneath Frappe entirely")
    victim = period[-1]["name"]
    before = c.get_doc(audit.DOCTYPE, victim)["reason"]
    print(f"  UPDATE `tab{audit.DOCTYPE}` SET reason='...' WHERE name='{victim}'")
    sql(f"UPDATE `tab{audit.DOCTYPE}` SET reason='routine, nothing to see here' "
        f"WHERE name='{victim}';")
    print(f"    the row now reads: {c.get_doc(audit.DOCTYPE, victim)['reason']!r}")
    v = audit.verify_chain(c)
    print(f"    verify_chain -> ok={v['ok']}  tampered={[t['name'] for t in v['tampered']]}")
    for t in v["tampered"]:
        print(f"      {t['name']}  stored     {t['stored']}")
        print(f"      {'':<{len(t['name'])}}  recomputed {t['recomputed']}")
    sql(f"UPDATE `tab{audit.DOCTYPE}` SET reason=%s WHERE name='{victim}';", before)

    other = period[len(period) // 2]["name"]
    print(f"\n  DELETE FROM `tab{audit.DOCTYPE}` WHERE name='{other}'")
    row = c.get_doc(audit.DOCTYPE, other)
    sql(f"DELETE FROM `tab{audit.DOCTYPE}` WHERE name='{other}';")
    v = audit.verify_chain(c)
    print(f"    verify_chain -> ok={v['ok']}  records={v['records']}  "
          f"broken={[b['name'] for b in v['broken']]}")
    for b in v["broken"]:
        print(f"      {b['name']} (seq {b['seq']}) expects prev {b['expected_prev'][:16]}…, "
              f"stores {b['stored_prev'][:16]}…")
    print("\n    the deleted record is gone and cannot be recovered from the chain — "
          "the chain\n    proves only that it WAS there. That is tamper-evidence, "
          "not tamper-proofing.")
    _restore(c, row)
    v = audit.verify_chain(c)
    print(f"\n  restored; verify_chain -> ok={v['ok']} records={v['records']}")


def _restore(c, row):
    """Put the deleted row back exactly as it was, hash included, so the demo
    leaves the chain intact. Uses SQL because the API would refuse to insert a
    record at docstatus 1 with a chosen name."""
    sql = _sql_runner()
    cols = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx"] + [
        f for f in audit.LIST_FIELDS if f not in ("name", "docstatus")] + [
        "schema_version", "seq", "prev_hash", "record_hash"]
    cols = list(dict.fromkeys(c for c in cols if c in row))
    ph = ", ".join(["%s"] * len(cols))
    sql(f"INSERT INTO `tab{audit.DOCTYPE}` ({', '.join('`'+x+'`' for x in cols)}) "
        f"VALUES ({ph});", *[row.get(x) for x in cols])


_SQL_CONF = {}


def _sql_runner():
    """Return a callable that runs SQL in the site's MariaDB container, or None."""
    if "fn" in _SQL_CONF:
        return _SQL_CONF["fn"]
    _SQL_CONF["fn"] = None
    try:
        raw = subprocess.run(
            ["docker", "exec", "headless-erp-backend-1", "sh", "-c",
             "cat sites/*/site_config.json"],
            capture_output=True, text=True, timeout=20)
        conf = json.loads(raw.stdout)
        db, user, pwd = conf["db_name"], conf["db_user"], conf["db_password"]
    except Exception:  # noqa: BLE001
        return None

    def run(stmt, *params):
        for p in params:
            stmt = stmt.replace("%s", "'" + str(p).replace("'", "''") + "'", 1)
        return subprocess.run(
            ["docker", "exec", "headless-erp-db-1", "mariadb",
             f"-u{user}", f"-p{pwd}", db, "-e", stmt],
            capture_output=True, text=True, timeout=30)

    _SQL_CONF["fn"] = run
    return run


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


PRICING_RULE = "PRLE-0001"


def _with_pricing_rule(c, fn):
    """Enable the 10% Pricing Rule for exactly one call, then put it back.

    The rule already exists on this instance and is left disabled, because it
    is global metadata on a shared site: while it is on, every Sales Invoice
    anyone creates is repriced. So it is enabled for one document and disabled
    again in a finally, even if the call raises.
    """
    if not c.exists("Pricing Rule", PRICING_RULE):
        print(f"  skipped: Pricing Rule {PRICING_RULE} not present on this instance")
        return None
    c.call("frappe.client.set_value", doctype="Pricing Rule", name=PRICING_RULE,
           fieldname="disable", value=0)
    print(f"  enabled Pricing Rule {PRICING_RULE} (10% discount) for one document")
    try:
        return fn()
    finally:
        c.call("frappe.client.set_value", doctype="Pricing Rule", name=PRICING_RULE,
               fieldname="disable", value=1)
        print(f"  disabled Pricing Rule {PRICING_RULE} again")


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
    """Pull the one useful line out of a Frappe traceback blob."""
    try:
        body = json.loads(str(e).split("\n", 1)[1])
        msg = body.get("exception") or body.get("exc_type") or ""
    except Exception:  # noqa: BLE001
        msg = str(e)
    msg = re.sub(r"<[^>]+>", "", msg).replace("\n", " ")
    return " ".join(msg.split())[:150]


if __name__ == "__main__":
    raise SystemExit(main())
