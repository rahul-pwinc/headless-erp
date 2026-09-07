"""Minimal Frappe/ERPNext REST client.

Session-cookie auth, same path a browser uses, so nothing here depends on
API-key-only code paths.
"""
from __future__ import annotations

import json
from typing import Any

import requests


class FrappeError(RuntimeError):
    pass


class FrappeClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
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
