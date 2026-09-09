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
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
