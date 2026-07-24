"""Customer API token helpers."""

from __future__ import annotations

import hashlib
import secrets

import frappe
from frappe.utils import now_datetime


def create_token(customer: str, token_name: str, scopes: str = "sites:read,apps:read", expires_on=None) -> dict:
	api_key = "sk_" + secrets.token_hex(12)
	api_secret = secrets.token_urlsafe(32)
	secret_hash = hashlib.sha256(api_secret.encode()).hexdigest()
	doc = frappe.get_doc(
		{
			"doctype": "Space API Token",
			"token_name": token_name,
			"customer": customer,
			"api_key": api_key,
			"api_secret_hash": secret_hash,
			"scopes": scopes,
			"status": "Active",
			"expires_on": expires_on,
		}
	)
	# Password field — set after insert via set_password if needed; store hash in Data-like Password
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"name": doc.name, "api_key": api_key, "api_secret": api_secret, "scopes": scopes}


def rotate_token(name: str) -> dict:
	doc = frappe.get_doc("Space API Token", name)
	doc.status = "Rotated"
	doc.save(ignore_permissions=True)
	return create_token(doc.customer, doc.token_name + " (rotated)", doc.scopes or "", doc.expires_on)


def revoke_token(name: str) -> dict:
	doc = frappe.get_doc("Space API Token", name)
	doc.status = "Revoked"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


def log_token_use(token_name: str, method: str, result: str = "Success", duration_ms: int = 0):
	try:
		frappe.get_doc(
			{
				"doctype": "Space API Token Log",
				"token": token_name,
				"method": method,
				"ip_address": getattr(frappe.local, "request_ip", None),
				"result": result,
				"duration_ms": duration_ms,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Space API Token",
			token_name,
			{"last_used_at": now_datetime(), "usage_count": (frappe.db.get_value("Space API Token", token_name, "usage_count") or 0) + 1},
		)
		frappe.db.commit()
	except Exception:
		pass
