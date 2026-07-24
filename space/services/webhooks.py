"""Outgoing webhook dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe
import requests


def dispatch(event: str, payload: dict, customer: str | None = None):
	filters = {"is_active": 1, "direction": "Outgoing"}
	hooks = frappe.get_all("Space Webhook", filters=filters, fields=["name", "url", "events", "secret", "customer"])
	for h in hooks:
		events = [e.strip() for e in (h.events or "").split(",") if e.strip()]
		if events and event not in events:
			continue
		if h.customer and customer and h.customer != customer:
			continue
		_deliver(h.name, h.url, event, payload, h.secret)


def _deliver(webhook_name: str, url: str, event: str, payload: dict, secret: str | None):
	body = json.dumps({"event": event, "data": payload}, default=str)
	headers = {"Content-Type": "application/json", "X-Space-Event": event}
	try:
		if secret:
			try:
				sec = frappe.get_doc("Space Webhook", webhook_name).get_password("secret")
			except Exception:
				sec = secret
			if sec:
				sig = hmac.new(sec.encode(), body.encode(), hashlib.sha256).hexdigest()
				headers["X-Space-Signature"] = sig
		resp = requests.post(url, data=body, headers=headers, timeout=15)
		status = "Success" if 200 <= resp.status_code < 300 else "Failed"
		code = resp.status_code
		text = (resp.text or "")[:2000]
	except Exception as e:
		status = "Failed"
		code = 0
		text = str(e)[:2000]

	frappe.get_doc(
		{
			"doctype": "Space Webhook Log",
			"webhook": webhook_name,
			"event": event,
			"payload": body[:50000],
			"response_code": code,
			"response_body": text,
			"status": status,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("Space Webhook", webhook_name, "last_status", status)
	frappe.db.commit()
