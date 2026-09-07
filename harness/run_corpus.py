"""Phase 5 runner — execute the use-case corpus against a live ERPNext."""
from __future__ import annotations
import argparse, collections, json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml                                  # noqa: E402
from client import FrappeClient              # noqa: E402
from fixtures import ensure_all              # noqa: E402
from intent import IntentEngine              # noqa: E402
from corpus import CorpusRunner              # noqa: E402

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--corpus", default="corpus/scenarios.yaml")
    ap.add_argument("--out", default="reports/corpus.json")
    ap.add_argument("--only", default=None, help="substring filter on scenario id")
    a = ap.parse_args()

    c = FrappeClient(a.url, "Administrator", "admin")
    fx = ensure_all(c)
    runner = CorpusRunner(c, IntentEngine(c), fx)
    scenarios = yaml.safe_load(open(a.corpus))["scenarios"]
    if a.only:
        scenarios = [s for s in scenarios if a.only in s["id"]]

    results, by_cat = [], collections.defaultdict(lambda: [0, 0])
    print(f"\nrunning {len(scenarios)} scenarios\n" + "=" * 74)
    cat = None
    for sc in scenarios:
        if sc["category"] != cat:
            cat = sc["category"]
            print(f"\n── {cat} " + "─" * (68 - len(cat)))
        r = runner.run(sc)
        results.append(r)
        by_cat[r.category][1] += 1
        by_cat[r.category][0] += 1 if r.passed else 0
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  {r.id}")
        if not r.passed:
            print(f"        principle: {r.principle}")
            for f in r.failures:
                print(f"        -> {f}")

    passed = sum(1 for r in results if r.passed)
    print("\n" + "=" * 74)
    for k in sorted(by_cat):
        p, t = by_cat[k]
        print(f"  {k:22} {p}/{t}")
    print(f"\n  TOTAL                  {passed}/{len(results)}")

    json.dump({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "erpnext_version": "v16.34.1",
        "passed": passed, "total": len(results),
        "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in by_cat.items()},
        "results": [{"id": r.id, "category": r.category, "principle": r.principle,
                     "passed": r.passed, "failures": r.failures, "docs": r.docs,
                     "notes": r.notes} for r in results],
    }, open(a.out, "w"), indent=2)
    print(f"\nreport: {a.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
