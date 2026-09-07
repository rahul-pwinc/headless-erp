"""Prove the enforcement boundary end to end. No browser, no trust in the client."""
from __future__ import annotations
import html, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from client import FrappeClient, FrappeError
from enforce import ensure, AGENT_USER, AGENT_PASSWORD

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
    s = requests.Session()
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

    passed = sum(1 for _, p in results if p)
    print(f"\n{'='*66}\n{passed}/{len(results)} boundary checks passed")
    json.dump({"passed": passed, "total": len(results),
               "checks": [{"check": c, "pass": p} for c, p in results]},
              open("reports/boundary.json", "w"), indent=2)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
