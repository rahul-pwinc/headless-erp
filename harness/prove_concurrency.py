"""Race the audit chain. Twenty simultaneous overrides through the enforced endpoint.

The Intent Override Log is a hash chain: each record stores the hash of the one
before it. The endpoint reads the head record, then inserts. Between those two
steps another request can insert, and both writers then chain off the same
parent -- a fork. A forked chain still verifies pairwise but no longer proves
ordering, which is most of what a chain is for.

This has been flagged as untested in LIMITATIONS. Untested is not a result.
Twenty parallel calls, then verify the chain is contiguous and every hash still
validates.

    make concurrency
"""
from __future__ import annotations

import argparse, collections, json, os, sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests                              # noqa: E402
from client import FrappeClient              # noqa: E402
from fixtures import ensure_all              # noqa: E402
from enforce import ensure, AGENT_USER, AGENT_PASSWORD   # noqa: E402
from audit import verify_chain               # noqa: E402

URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
SITE = os.environ.get("ERPNEXT_SITE")


def login_once() -> dict:
    """Authenticate once and reuse the cookie.

    Logging in per thread does not work: Frappe's own session creation races, and
    4 of 5 concurrent logins return 500. That is a fact about frappe login, not
    about this control, and letting it stand would have made the concurrency
    result meaningless -- the first run showed 1 of 20 succeeding and the chain
    "holding" simply because 19 writes never happened.
    """
    s = requests.Session()
    if SITE:
        s.headers["Host"] = SITE
    r = s.post(f"{URL}/api/method/login", json={"usr": AGENT_USER, "pwd": AGENT_PASSWORD})
    if r.status_code != 200:
        raise SystemExit(f"agent login failed: {r.status_code} {r.text[:200]}")
    return dict(s.cookies)


def one_call(i: int, fx: dict, cookies: dict) -> dict:
    """One request per thread, sharing the authenticated cookie."""
    s = requests.Session()
    s.cookies.update(cookies)
    if SITE:
        s.headers["Host"] = SITE
    r = s.post(f"{URL}/api/method/bill_intent", json={
        "customer": fx["customer"], "company": fx["company"],
        "posting_date": "2026-09-08", "due_date": "2026-10-08",
        "selling_price_list": fx["price_list"],
        "items": [{"item_code": fx["item_code"], "qty": 1}],
        "overrides": [{"row": 0, "field": "rate", "value": 100.0 + i,
                       "reason": f"concurrency probe {i}"}],
    })
    if r.status_code != 200:
        return {"i": i, "ok": False, "err": r.text[:120]}
    m = r.json().get("message", {})
    return {"i": i, "ok": True, "invoice": m.get("name"), "audit": m.get("audit")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", default="reports/concurrency.json")
    a = ap.parse_args()

    admin = FrappeClient(URL, "Administrator", "admin")
    fx = ensure_all(admin)
    ensure(admin)

    before = admin.call("frappe.client.get_list", doctype="Intent Override Log",
                        fields=["name"], limit_page_length=0) or []
    print(f"\nfiring {a.n} simultaneous overrides through the enforced endpoint "
          f"({len(before)} records already in the chain)\n")

    with ThreadPoolExecutor(max_workers=a.n) as ex:
        cookies = login_once()
        results = list(ex.map(lambda i: one_call(i, fx, cookies), range(a.n)))

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print(f"  succeeded : {len(ok)}/{a.n}")
    if bad:
        print(f"  failed    : {len(bad)}")
        for b in bad[:3]:
            print(f"     {b['err'][:100]}")

    rows = admin.call("frappe.client.get_list", doctype="Intent Override Log",
                      fields=["name", "seq", "prev_hash", "record_hash"],
                      order_by="seq asc", limit_page_length=0) or []
    seqs = [int(r["seq"]) for r in rows if r.get("seq") is not None]
    dupes = [s for s, n in collections.Counter(seqs).items() if n > 1]
    gaps = [s for s in range(min(seqs), max(seqs) + 1) if s not in set(seqs)] if seqs else []
    forks = [h for h, n in collections.Counter(
        r["prev_hash"] for r in rows if r.get("prev_hash")).items() if n > 1]

    chain = verify_chain(admin)

    print(f"\n  records in chain : {len(rows)}")
    print(f"  duplicate seq    : {len(dupes)}  {dupes[:5]}")
    print(f"  gaps in seq      : {len(gaps)}  {gaps[:5]}")
    print(f"  forks (shared prev_hash) : {len(forks)}")
    print(f"  verify_chain     : {chain}")

    # Two independent properties. Conflating them is how the first run of this
    # test reported "chain did not hold" when what actually happened was that 19
    # of 20 writes never landed, so there was nothing for the chain to get wrong.
    #
    #   INTEGRITY  did the chain stay contiguous and verifiable?
    #   AVAILABILITY  did concurrent writes actually succeed?
    #
    # They have opposite answers here and both are worth stating.
    integrity = not dupes and not gaps and not forks and chain.get("ok") is True
    availability = len(ok) == a.n
    deadlocks = sum(1 for b in bad if "Deadlock" in (b.get("err") or ""))

    print(f"\n  INTEGRITY    : {'HELD' if integrity else 'BROKEN'} "
          f"(no duplicate seq, no gaps, no forks, verify_chain ok)")
    print(f"  AVAILABILITY : {len(ok)}/{a.n} writes landed"
          + (f", {deadlocks} rejected with QueryDeadlockError" if deadlocks else ""))
    if integrity and not availability:
        print("\n  The chain is SAFE under concurrency but not AVAILABLE. MariaDB's row")
        print("  locking prevents the fork this test was written to find; it prevents it")
        print("  by refusing most of the concurrent writes. The endpoint does not retry,")
        print("  so a caller issuing parallel overrides loses them unless it retries.")
        print("  Integrity is the property that matters for an audit chain, and it holds.")
        print("  Availability is a real limitation and belongs in LIMITATIONS, not hidden")
        print("  behind a green check.")
    clean = integrity

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"integrity_held": integrity, "availability_full": availability,
               "attempted": a.n, "succeeded": len(ok), "failed": len(bad),
               "records": len(rows), "duplicate_seq": dupes, "seq_gaps": gaps,
               "forks": forks, "verify_chain": chain, "clean": clean,
               "failures": [b.get("err") for b in bad][:5]},
              open(a.out, "w"), indent=2, default=str)
    print(f"report: {a.out}")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
