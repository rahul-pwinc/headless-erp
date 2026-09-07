"""Phase 4 proof — the defect is structurally unreachable through the intent layer."""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import FrappeClient            # noqa: E402
from fixtures import ensure_all            # noqa: E402
from intent import IntentEngine, IntentRefused   # noqa: E402

TODAY, LATER = "2026-09-07", "2026-10-07"
OK, NO = "  PASS", "  FAIL"
results = []

def case(n, desc, fn, expect_refused: bool):
    try:
        r = fn()
        if expect_refused:
            print(f"{NO}  {n}. {desc}\n        expected refusal, got {r.name}")
            results.append((n, False)); return None
        print(f"{OK}  {n}. {desc}")
        print(f"        {r.name}  grand_total={r.grand_total}  "
              f"overrides={len(r.overrides)}  invariants={len(r.invariants_checked)} "
              f"failures={len(r.invariant_failures)}")
        for o in r.overrides:
            print(f"        recorded: {o.fieldname} {o.derived_value} -> {o.supplied_value} :: {o.reason}")
        for f in r.invariant_failures:
            print(f"        INVARIANT FAILURE: {f}")
        results.append((n, not r.invariant_failures)); return r
    except IntentRefused as e:
        if expect_refused:
            print(f"{OK}  {n}. {desc}")
            print(f"        refused: {str(e).splitlines()[0]}")
            results.append((n, True)); return None
        print(f"{NO}  {n}. {desc}\n        unexpected refusal: {e}")
        results.append((n, False)); return None

def main() -> int:
    c = FrappeClient(os.environ.get("ERPNEXT_URL", "http://localhost:8080"), "Administrator", "admin")
    fx = ensure_all(c)
    eng = IntentEngine(c)
    hdr = {"customer": fx["customer"], "company": fx["company"], "currency": "INR", "selling_price_list": fx["price_list"],
           "price_list_currency": "INR",
           "posting_date": TODAY, "due_date": LATER, "update_stock": 0}
    line = [{"item_code": fx["item_code"], "qty": 4}]
    print(f"\nlist price = {fx['list_price']}, qty 4  ->  a correct invoice is {fx['list_price']*4}\n")

    case(1, "clean bill(): derives the list price, no caller input",
         lambda: eng.execute("bill", hdr, line), False)

    case(2, "bill() with price_list_rate supplied  ->  REFUSED (read-only in the UI)",
         lambda: eng.execute("bill", hdr, [{**line[0], "price_list_rate": 0.48}]), True)

    case(3, "bill() with rate smuggled into the line  ->  REFUSED (must be a declared override)",
         lambda: eng.execute("bill", hdr, [{**line[0], "rate": 1.0}],
                             overrides=[{"field": "rate", "value": 1.0, "reason": ""}]), True)

    case(4, "bill() override with no reason  ->  REFUSED",
         lambda: eng.execute("bill", hdr, line,
                             overrides=[{"field": "rate", "value": 1.0, "reason": "   "}]), True)

    case(5, "bill() override WITH a reason  ->  allowed, recorded, still consistent",
         lambda: eng.execute("bill", hdr, line,
                             overrides=[{"field": "rate", "value": 1.0,
                                         "reason": "goodwill credit, approved by finance"}]), False)

    case(6, "bill() override of a derived-only field  ->  REFUSED",
         lambda: eng.execute("bill", hdr, line,
                             overrides=[{"field": "net_amount", "value": 1.0, "reason": "x"}]), True)

    case(7, "bill() with conversion_rate asserted  ->  REFUSED (the headline finding, closed)",
         lambda: eng.execute("bill", {**hdr, "currency": "USD", "conversion_rate": 1.0}, line), True)

    case(8, "bill() with a caller-supplied income_account  ->  REFUSED",
         lambda: eng.execute("bill", hdr, [{**line[0], "income_account": "Interest Income - HTC"}]), True)

    case(9, "bill() with a caller-supplied conversion_factor  ->  REFUSED (moves stock, not just money)",
         lambda: eng.execute("bill", hdr, [{**line[0], "conversion_factor": 7}]), True)

    case(10, "return with a rate override  ->  REFUSED (intent-level refuse beats the default)",
         lambda: eng.execute("return", {**hdr, "is_return": 1}, line,
                             overrides=[{"field": "rate", "value": 99.0, "reason": "renegotiated"}]), True)

    passed = sum(1 for _, p in results if p)
    print(f"\n{'='*64}\n{passed}/{len(results)} cases behaved as specified")
    json.dump({"passed": passed, "total": len(results),
               "cases": [{"n": n, "pass": p} for n, p in results]},
              open("reports/intent_proof.json", "w"), indent=2)
    return 0 if passed == len(results) else 1

if __name__ == "__main__":
    raise SystemExit(main())
