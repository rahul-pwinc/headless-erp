"""Prove the enforcement boundary end to end. No browser, no trust in the client."""
from __future__ import annotations
import html, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from client import FrappeClient, FrappeError
from enforce import ensure, AGENT_USER, AGENT_PASSWORD, SCRIPT_PATH

URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
HDR = {"customer": "Headless Test Customer", "company": "Headless Test Co",
       "posting_date": "2026-09-08", "due_date": "2026-10-08",
       "selling_price_list": "Standard Selling"}
results = []


def srv_msg(j: dict) -> str:
    try:
        return re.sub(r"<[^>]+>", "", html.unescape(
            json.loads(json.loads(j["_server_messages"])[0])["message"]))
    except Exception:
        return (j.get("exception") or "")[:140]


def main() -> int:
    admin = FrappeClient(URL, "Administrator", "admin")
    print("provisioning boundary:")
    ensure(admin)

    print("\n1. direct write, unconstrained identity")
    try:
        d = admin.insert({**HDR, "doctype": "Sales Invoice",
                          "items": [{"item_code": "HL-WIDGET-001", "qty": 4, "rate": 1.0}]})
        print(f"   Administrator POST /api/resource -> ALLOWED {d['name']} at rate 1.0")
        results.append(("admin direct write allowed", True))
    except FrappeError as e:
        print(f"   unexpected: {str(e)[:90]}"); results.append(("admin direct write allowed", False))

    print("\n2. direct write, constrained identity")
    # The agent path uses a raw session, so it must carry the same Host header
    # the FrappeClient does. Without this every agent-side call silently went to
    # the default site while the Administrator half honoured ERPNEXT_SITE, so a
    # "clean site" run was actually testing two different sites at once.
    s = requests.Session()
    if os.environ.get("ERPNEXT_SITE"):
        s.headers["Host"] = os.environ["ERPNEXT_SITE"]
    s.post(f"{URL}/api/method/login", json={"usr": AGENT_USER, "pwd": AGENT_PASSWORD})
    r = s.post(f"{URL}/api/resource/Sales Invoice",
               json={**HDR, "items": [{"item_code": "HL-WIDGET-001", "qty": 4, "rate": 1.0}]})
    blocked = r.status_code == 403
    print(f"   agent POST /api/resource -> {r.status_code} "
          f"{'BLOCKED, boundary holds' if blocked else 'ALLOWED, boundary FAILED'}")
    results.append(("agent direct write blocked", blocked))

    print("\n3. constrained identity through the enforced endpoint")
    for label, items, ovr, expect_block in [
        ("clean call derives the list price", [{"item_code": "HL-WIDGET-001", "qty": 4}], [], False),
        ("caller supplies rate", [{"item_code": "HL-WIDGET-001", "qty": 4, "rate": 1.0}], [], True),
        ("caller supplies price_list_rate", [{"item_code": "HL-WIDGET-001", "qty": 4, "price_list_rate": 0.48}], [], True),
        ("override without a reason", [{"item_code": "HL-WIDGET-001", "qty": 4}],
         [{"row": 0, "field": "rate", "value": 1.0, "reason": "  "}], True),
        ("override with a reason", [{"item_code": "HL-WIDGET-001", "qty": 4}],
         [{"row": 0, "field": "rate", "value": 1.0, "reason": "goodwill, approved by finance"}], False),
        ("item with no resolvable price", [{"item_code": "HL-UNPRICED-001", "qty": 4}], [], True),
    ]:
        rr = s.post(f"{URL}/api/method/bill_intent", json={**HDR, "items": items, "overrides": ovr})
        if rr.status_code == 200:
            m = rr.json()["message"]; l = m["lines"][0]
            ok = not expect_block
            # The endpoint used to insert and never submit, so every invoice it
            # wrote sat at docstatus 0 with drift checked once at insert and a
            # draft window left open for anyone to edit the rate and submit.
            # Assert the document is actually posted, or the control is writing
            # drafts and calling it done.
            posted = admin.get_doc("Sales Invoice", m["name"])
            if int(posted.get("docstatus") or 0) != 1:
                ok = False
                print(f"        NOT SUBMITTED: {m['name']} is at docstatus "
                      f"{posted.get('docstatus')}")
            print(f"   {'ok  ' if ok else 'FAIL'} {label}")
            print(f"        {m['name']} list={l['derived']} requested={l['intended']} stored={l['stored']}"
                  # `drift` is now always present on the response ("none" /
                  # "changed"), so test the value, not the key: the old
                  # truthiness check printed "DRIFT: none" on every clean line.
                  + (f"  DRIFT: {l['drift']}" if l.get("drift") not in (None, "none") else "")
                  + (f"  audit={l['audit_record']}" if l.get("audit_record") else ""))
        else:
            ok = expect_block
            print(f"   {'ok  ' if ok else 'FAIL'} {label}\n        refused: {srv_msg(rr.json())[:100]}")
        results.append((label, ok))

    print("\n4. identity scope")
    rr = s.post(f"{URL}/api/method/bill_intent",
                json={**HDR, "company": "Some Other Co",
                      "items": [{"item_code": "HL-WIDGET-001", "qty": 4}], "overrides": []})
    scoped = rr.status_code != 200
    print(f"   {'ok  ' if scoped else 'FAIL'} billing for a company this identity does not own")
    if scoped:
        print(f"        refused: {srv_msg(rr.json())[:90]}")
    results.append(("out-of-scope company refused", scoped))

    rr = s.post(f"{URL}/api/method/bill_intent",
                json={**HDR, "customer": "Some Other Customer",
                      "items": [{"item_code": "HL-WIDGET-001", "qty": 4}], "overrides": []})
    party_scoped = rr.status_code != 200
    print(f"   {'ok  ' if party_scoped else 'FAIL'} billing a customer this identity was not provisioned for")
    if party_scoped:
        print(f"        refused: {srv_msg(rr.json())[:90]}")
    results.append(("out-of-scope party refused", party_scoped))

    # ---- forced audit failure -------------------------------------------
    # The endpoint throws when the audit write fails, so an override can never
    # post without a record. That is a claim about a failure path, and an
    # untested failure path is what the original hole was: the old code caught
    # the exception, set audit_error, and returned 200 with an unrecorded
    # override committed. So force it.
    #
    # Break the audit doctype name in a deployed copy of the script, call it
    # with an override, and assert both that the call fails AND that no invoice
    # survives. Restore the real script afterwards.
    print("\n5. forced audit failure rolls the invoice back")
    # Read the DEPLOYED script rather than re-substituting from disk. An earlier
    # version rebuilt it from SCRIPT_PATH and substituted only
    # __ALLOWED_COMPANY__, leaving a literal __ALLOWED_PARTIES__ behind. The
    # restore then matched no customer and every later check failed with 417,
    # which the idempotency checks caught immediately. Restoring from what was
    # actually deployed has no substitutions to forget.
    good = admin.get_doc("Server Script", "bill_intent")["script"]
    broken = good.replace('AUDIT_DOCTYPE = "Intent Override Log"',
                          'AUDIT_DOCTYPE = "Intent Override Log DOES NOT EXIST"')
    assert broken != good, "failed to break the audit doctype name"
    before = len(admin.call("frappe.client.get_list", doctype="Sales Invoice",
                            fields=["name"], limit_page_length=0) or [])
    admin.call("frappe.client.set_value", doctype="Server Script",
               name="bill_intent", fieldname="script", value=broken)
    try:
        rr = s.post(f"{URL}/api/method/bill_intent", json={
            **HDR, "items": [{"item_code": "HL-WIDGET-001", "qty": 4}],
            "overrides": [{"row": 0, "field": "rate", "value": 1.0,
                           "reason": "forced audit failure probe"}]})
        errored = rr.status_code != 200
        after = len(admin.call("frappe.client.get_list", doctype="Sales Invoice",
                               fields=["name"], limit_page_length=0) or [])
        rolled_back = after == before
        ok = errored and rolled_back
        print(f"   {'ok  ' if ok else 'FAIL'} audit write forced to fail")
        print(f"        response      : {rr.status_code}"
              + ("" if errored else "   <-- returned success with no audit record"))
        print(f"        invoices before/after : {before}/{after}"
              + ("" if rolled_back else "   <-- an invoice survived"))
        results.append(("forced audit failure rolls back", ok))
    finally:
        admin.call("frappe.client.set_value", doctype="Server Script",
                   name="bill_intent", fieldname="script", value=good)
        # Re-read and confirm the restore actually took. Restoring is itself a
        # write, and this file has already been bitten once by trusting one.
        back = admin.get_doc("Server Script", "bill_intent")["script"]
        restored = back == good and "__ALLOWED_" not in back
        print(f"        (real script restored: {restored})")
        results.append(("script restored after forced failure", restored))

    # ---- idempotency -----------------------------------------------------
    # Every integration retries. A retry after a deadlock without an
    # idempotency key creates a second invoice, and at scale that is a
    # double-posted ledger. Two cases: sequential replay, and the harder
    # simultaneous case where the database has to settle it rather than the
    # application, because a check-then-insert in the script has a window.
    import uuid as _uuid
    from concurrent.futures import ThreadPoolExecutor as _TPE

    print("\n6. idempotency")
    key = f"boundary-{_uuid.uuid4().hex[:12]}"
    payload = {**HDR, "idempotency_key": key,
               "items": [{"item_code": "HL-WIDGET-001", "qty": 4}],
               "overrides": [{"row": 0, "field": "rate", "value": 7.0,
                              "reason": "idempotency boundary check"}]}
    seen = []
    for _ in range(3):
        rr = s.post(f"{URL}/api/method/bill_intent", json=payload)
        seen.append(rr.json().get("message", {}) if rr.status_code == 200 else {})
    inv = admin.call("frappe.client.get_list", doctype="Sales Invoice",
                     filters={"intent_idempotency_key": key},
                     fields=["name"], limit_page_length=0) or []
    logs = (admin.call("frappe.client.get_list", doctype="Intent Override Log",
                       filters={"target_name": inv[0]["name"]}, fields=["name"],
                       limit_page_length=0) or []) if inv else []
    ok = len(inv) == 1 and len(logs) == 1 and [m.get("replayed") for m in seen] == [False, True, True]
    print(f"   {'ok  ' if ok else 'FAIL'} three identical sends -> one invoice, one audit record")
    print(f"        invoices={len(inv)} audit_records={len(logs)} "
          f"replayed={[m.get('replayed') for m in seen]}")
    results.append(("idempotent replay", ok))

    # Simultaneous first attempts. The unique index on the custom field is what
    # makes this safe; the script's check-then-insert alone would not be.
    ckey = f"boundary-conc-{_uuid.uuid4().hex[:10]}"
    cpayload = {**payload, "idempotency_key": ckey}
    cookies = dict(s.cookies)

    def _fire(_):
        t = requests.Session()
        t.cookies.update(cookies)
        if os.environ.get("ERPNEXT_SITE"):
            t.headers["Host"] = os.environ["ERPNEXT_SITE"]
        rr = t.post(f"{URL}/api/method/bill_intent", json=cpayload)
        return rr.status_code

    with _TPE(max_workers=6) as ex:
        codes = list(ex.map(_fire, range(6)))
    cinv = admin.call("frappe.client.get_list", doctype="Sales Invoice",
                      filters={"intent_idempotency_key": ckey},
                      fields=["name"], limit_page_length=0) or []
    # Six 200s, one invoice, five replayed. An earlier version accepted one 200
    # and five 500s as a pass, which is safe but not integrable: a client that
    # treats 500 as fatal reports failure for a write that succeeded.
    all_ok = all(c == 200 for c in codes)
    cok = len(cinv) == 1 and all_ok
    print(f"   {'ok  ' if cok else 'FAIL'} six simultaneous sends of one key")
    print(f"        invoices={len(cinv)}  responses={codes}")
    if len(cinv) != 1:
        print("        a duplicate got through: the unique index is not holding")
    if not all_ok:
        print("        the race losers got errors, not replays: not integrable")
    results.append(("idempotent under concurrency, all callers get an answer", cok))

    passed = sum(1 for _, p in results if p)
    print(f"\n{'='*66}\n{passed}/{len(results)} boundary checks passed")
    json.dump({"passed": passed, "total": len(results),
               "checks": [{"check": c, "pass": p} for c, p in results]},
              open("reports/boundary.json", "w"), indent=2)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
