"""Simple in-process / cache rate limiting for Space APIs."""

from __future__ import annotations

import time

import frappe


def check_rate_limit(bucket: str | None = None) -> None:
	"""Raise PermissionError if caller exceeds Space Settings.rate_limit_per_minute."""
	try:
		limit = int(frappe.db.get_single_value("Space Settings", "rate_limit_per_minute") or 120)
	except Exception:
		limit = 120
	if limit <= 0:
		return

	user = frappe.session.user if frappe.session else "Guest"
	ip = "unknown"
	try:
		ip = getattr(frappe.local, "request_ip", None) or "unknown"
	except Exception:
		pass
	key = f"space_rl:{bucket or 'api'}:{user}:{ip}:{int(time.time() // 60)}"
	try:
		count = frappe.cache().get_value(key) or 0
		count = int(count) + 1
		frappe.cache().set_value(key, count, expires_in_sec=90)
		if count > limit:
			frappe.throw("Rate limit exceeded", frappe.PermissionError)
	except frappe.PermissionError:
		raise
	except Exception:
		# cache unavailable — do not block
		pass
