"""Minimal Frappe/ERPNext REST client.

Session-cookie auth, same path a browser uses, so nothing here depends on
API-key-only code paths.
"""
from __future__ import annotations

import json
import os
from typing import Any

import requests


class FrappeError(RuntimeError):
    pass


class FrappeClient:
    def __init__(self, base_url: str, username: str, password: str,
                 site: str | None = None) -> None:
        """`site` sets the Host header, which is how a Frappe bench routes to one
        of several sites on the same containers. Set ERPNEXT_SITE to run any
        script in this repo against a clean site instead of the shared one:

            ERPNEXT_SITE=clean.local ./.venv/bin/python harness/run_corpus.py

        Published numbers should come from a freshly created site. A long-lived
        shared instance accumulates state, and every report here is a snapshot
        of whatever that state happened to be.
        """
        self.base_url = base_url.rstrip("/")
        self.site = site or os.environ.get("ERPNEXT_SITE")
        self.session = requests.Session()
        if self.site:
            self.session.headers["Host"] = self.site
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        r = self.session.post(
            f"{self.base_url}/api/method/login",
            json={"usr": username, "pwd": password},
            timeout=30,
        )
        if r.status_code != 200:
            raise FrappeError(f"login failed [{r.status_code}]: {r.text[:400]}")

    def _unwrap(self, r: requests.Response) -> Any:
        if r.status_code >= 400:
            raise FrappeError(f"[{r.status_code}] {r.request.method} {r.request.url}\n{r.text[:1500]}")
        body = r.json()
        return body.get("message", body.get("data", body))

    def call(self, method: str, **kwargs: Any) -> Any:
        """Invoke a whitelisted server method — the same entry point the Desk JS uses."""
        r = self.session.post(f"{self.base_url}/api/method/{method}", json=kwargs, timeout=120)
        return self._unwrap(r)

    def insert(self, doc: dict) -> dict:
        r = self.session.post(
            f"{self.base_url}/api/resource/{doc['doctype']}",
            data=json.dumps(doc),
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        return self._unwrap(r)

    def submit(self, doc: dict) -> dict:
        """Submit a saved document (docstatus 0 -> 1). This is what posts to the
        General Ledger, and it is the step that proves the books still balance."""
        r = self.session.post(
            f"{self.base_url}/api/method/frappe.client.submit",
            data=json.dumps({"doc": doc}),
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        return self._unwrap(r)

    def get_doc(self, doctype: str, name: str) -> dict:
        r = self.session.get(f"{self.base_url}/api/resource/{doctype}/{name}", timeout=60)
        return self._unwrap(r)

    def exists(self, doctype: str, name: str) -> bool:
        r = self.session.get(f"{self.base_url}/api/resource/{doctype}/{name}", timeout=60)
        return r.status_code == 200
