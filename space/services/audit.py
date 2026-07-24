"""Searchable audit trail (extends activity logging)."""

from __future__ import annotations

import json
import time
from typing import Any

import frappe


SENSITIVE_KEYS = {
	"password",
	"admin_password",
	"private_key",
	"ssh_password",
	"db_root_password",
	"internal_token",
	"api_secret",
	"api_key",
	"token",
	"secret",
}


def _redact(obj: Any) -> Any:
	if isinstance(obj, dict):
		out = {}
		for k, v in obj.items():
			if str(k).lower() in SENSITIVE_KEYS or str(k).lower().endswith("_password"):
				out[k] = "***"
			else:
				out[k] = _redact(v)
		return out
	if isinstance(obj, list):
		return [_redact(x) for x in obj]
	return obj


def log_audit(
	action: str,
	*,
	api_method: str | None = None,
	result: str = "Success",
	duration_ms: int | None = None,
	ref_doctype: str | None = None,
	ref_name: str | None = None,
	before: Any = None,
	after: Any = None,
	details: str = "",
):
	try:
		ip = None
		try:
			ip = getattr(frappe.local, "request_ip", None) or (
				frappe.get_request_header("X-Forwarded-For") or frappe.get_request_header("X-Real-IP")
			)
		except Exception:
			pass
		doc = frappe.get_doc(
			{
				"doctype": "Space Audit Log",
				"user": frappe.session.user if frappe.session else None,
				"action": action[:140],
				"api_method": (api_method or "")[:200],
				"ip_address": (str(ip)[:64] if ip else None),
				"result": result if result in ("Success", "Failure", "Denied") else "Success",
				"duration_ms": duration_ms,
				"ref_doctype": ref_doctype,
				"ref_name": ref_name,
				"before_json": json.dumps(_redact(before), default=str)[:100000] if before is not None else None,
				"after_json": json.dumps(_redact(after), default=str)[:100000] if after is not None else None,
				"details": (details or "")[:5000],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.log_error(title="Space audit log failed")
		return None


class AuditTimer:
	def __init__(self, action: str, **kwargs):
		self.action = action
		self.kwargs = kwargs
		self.t0 = time.monotonic()
		self.result = "Success"

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc, tb):
		ms = int((time.monotonic() - self.t0) * 1000)
		result = "Failure" if exc_type else self.result
		log_audit(self.action, duration_ms=ms, result=result, **self.kwargs)
		return False
