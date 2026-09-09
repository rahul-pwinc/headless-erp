"""Verify every source citation in the repo against the RUNNING code.

Two mistakes made this necessary. Twice, a line number or a module path was read
out of `vendor/` -- which is a shallow clone of the *develop* branch -- while the
container runs a tagged release. Once that produced a 417 (`get_taxes_and_charges`
moved packages between v16 and v17); once it produced a wrong line number in the
README that an external reviewer caught.

The rule this enforces: cite the version you run, and prove it.

Every KEY_CITATIONS entry is (file, line, expected_substring). The check reads
the line out of the container and fails if the substring is not on it. Line
numbers drift between releases, so this is a canary for "the docs were written
against a different version", not a style check.

    make citations
"""
from __future__ import annotations

import re
import subprocess
import sys

CONTAINER = "headless-erp-backend-1"

# (app, path relative to the app, line, substring that must be on that line)
KEY_CITATIONS = [
    ("erpnext", "erpnext/controllers/accounts_controller.py", 97, "force_item_fields = "),
    ("erpnext", "erpnext/controllers/accounts_controller.py", 1127, "item.get(fieldname) is None"),
    ("erpnext", "erpnext/controllers/accounts_controller.py", 1061, "elif not self.conversion_rate"),
    ("erpnext", "erpnext/controllers/accounts_controller.py", 3241, "def get_taxes_and_charges"),
    ("erpnext", "erpnext/accounts/general_ledger.py", 536, "def raise_debit_credit_not_equal_error"),
    ("erpnext", "erpnext/accounts/general_ledger.py", 491, "raise_debit_credit_not_equal_error("),
    ("erpnext", "erpnext/accounts/general_ledger.py", 503, "raise_debit_credit_not_equal_error("),
    ("erpnext", "erpnext/stock/get_item_details.py", 592, "ctx.weight_per_unit or item.get"),
    ("frappe", "frappe/model/document.py", 477, 'check_permission("create")'),
    ("frappe", "frappe/model/base_document.py", 1078, "def set_fetch_from_value"),
]


def container_versions() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-lc",
         'grep -m1 "^__version__" apps/frappe/frappe/__init__.py; '
         'grep -m1 "^__version__" apps/erpnext/erpnext/__init__.py'],
        capture_output=True, text=True).stdout
    v = re.findall(r'"([\d.]+)"', out)
    return {"frappe": v[0] if v else "?", "erpnext": v[1] if len(v) > 1 else "?"}


def read_line(app: str, path: str, line: int) -> str | None:
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-lc", f"sed -n '{line}p' apps/{app}/{path}"],
        capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def main() -> int:
    vers = container_versions()
    print(f"running: frappe {vers['frappe']}, erpnext {vers['erpnext']}\n")
    bad = 0
    for app, path, line, expect in KEY_CITATIONS:
        actual = read_line(app, path, line)
        ok = actual is not None and expect in actual
        if not ok:
            bad += 1
        mark = "ok  " if ok else "WRONG"
        print(f"  {mark} {path}:{line}")
        print(f"        expect: {expect}")
        if not ok:
            print(f"        actual: {(actual or '<no such line>').strip()[:90]}")
    print(f"\n{len(KEY_CITATIONS) - bad}/{len(KEY_CITATIONS)} citations verified "
          f"against frappe {vers['frappe']} / erpnext {vers['erpnext']}")
    if bad:
        print("\nA wrong line number costs a reader their trust in every other number.")
    stale = check_counts(fix="--fix" in sys.argv)
    # The generated table cannot drift, but it can go out of date if an artifact
    # is regenerated without re-rendering. Fail on that too.
    import subprocess as _sp
    gen = _sp.run([sys.executable, "harness/render_numbers.py", "--check"],
                  capture_output=True, text=True)
    print(gen.stdout.strip() or gen.stderr.strip())
    stale += (1 if gen.returncode else 0)
    return 1 if (bad or stale) else 0


# ---------------------------------------------------------------------------
# Published counts must match the artifacts.
#
# Three review rounds in a row flagged stale numbers in the docs, and the round
# that was meant to fix them by hand introduced a mangled sentence and left
# eleven wrong figures live. Hand-fixing does not converge. So the counts are
# read out of the JSON reports and every doc is checked against them.
#
# A number in a document that disagrees with the artifact behind it is worse
# than no number: a reader who catches one stops trusting the rest.
# ---------------------------------------------------------------------------
import glob
import json
import os

DOCS = ["README.md", "Makefile"] + sorted(glob.glob("docs/*.md"))

# keyword that must appear on the line -> (report file, numerator key, denominator key)
COUNT_RULES = [
    (r"corpus|scenario", "reports/clean/corpus.json", "passed", "total"),
    (r"contract|intent proof|cases behaved", "reports/clean/intent_proof.json", "passed", "total"),
    (r"boundary", "reports/clean/boundary.json", "passed", "total"),
]


def _load(path: str) -> dict | None:
    try:
        return json.load(open(path))
    except Exception:
        return None


def check_counts(fix: bool = False) -> int:
    print("\npublished counts vs artifacts")
    problems = 0
    truth = []
    for kw, path, npk, dpk in COUNT_RULES:
        rep = _load(path)
        if not rep:
            print(f"  SKIP {path} missing")
            continue
        truth.append((kw, int(rep[npk]), int(rep[dpk]), path))
        print(f"  {os.path.basename(path):22} -> {rep[npk]}/{rep[dpk]}")

    frac = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")
    edits: dict[str, list] = {}
    for doc in DOCS:
        if not os.path.exists(doc):
            continue
        for lineno, line in enumerate(open(doc, encoding="utf-8"), 1):
            for kw, good_n, good_d, path in truth:
                if not re.search(kw, line, re.I):
                    continue
                for m in frac.finditer(line):
                    n, d = int(m.group(1)), int(m.group(2))
                    # only judge fractions that look like this metric's shape
                    if d not in (good_d, good_n) and abs(d - good_d) > 6:
                        continue
                    if (n, d) != (good_n, good_d):
                        problems += 1
                        print(f"  WRONG {doc}:{lineno}  says {n}/{d}, "
                              f"{os.path.basename(path)} says {good_n}/{good_d}")
                        print(f"        {line.strip()[:100]}")
                        if fix:
                            edits.setdefault(doc, []).append(
                                (lineno, m.group(0), f"{good_n}/{good_d}"))
    if fix and edits:
        for doc, changes in edits.items():
            lines = open(doc, encoding="utf-8").read().split("\n")
            for lineno, old, new in changes:
                lines[lineno - 1] = lines[lineno - 1].replace(old, new)
            open(doc, "w", encoding="utf-8").write("\n".join(lines))
            print(f"  fixed {len(changes)} in {doc}")
        print("  re-run without --fix to confirm")
    if not problems:
        print("  ok   every published count matches its artifact")
    return 0 if fix else problems




if __name__ == "__main__":
    raise SystemExit(main())
