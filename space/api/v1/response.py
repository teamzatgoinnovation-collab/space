"""Shared API helpers."""

from __future__ import annotations

import frappe


def ok(data=None, **kwargs):
	out = {"ok": True}
	if data is not None:
		out["data"] = data
	out.update(kwargs)
	return out


def fail(message: str, code: str = "ERROR", http_status: int = 400):
	frappe.local.response["http_status_code"] = http_status
	return {"ok": False, "error": message, "code": code}


def require_roles(*roles):
	if frappe.session.user == "Administrator":
		return
	user_roles = set(frappe.get_roles())
	if not user_roles.intersection(set(roles) | {"System Manager", "Space Admin"}):
		frappe.throw("Not permitted", frappe.PermissionError)


def customer_for_user(user: str | None = None) -> str | None:
	user = user or frappe.session.user
	if user in ("Guest", "Administrator"):
		return None
	return frappe.db.get_value("Space Customer", {"user": user}, "name")
