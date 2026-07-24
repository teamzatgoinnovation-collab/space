"""Re-export site naming from space_cloud when available; fallback local copy."""

try:
	from space_cloud.utils.site_naming import *  # noqa: F401,F403
except ImportError:
	from frappe import _

	RESERVED = {"space", "portal", "erp", "www", "mail", "ftp", "api", "admin", "frontend"}

	def validate_slug(slug: str) -> str:
		s = (slug or "").strip().lower()
		if not s or len(s) < 2:
			raise ValueError("Site name too short")
		if s in RESERVED:
			raise ValueError(f"Reserved site name: {s}")
		import re

		if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", s):
			raise ValueError("Invalid site slug")
		return s

	def build_domain(slug: str, suffix: str | None = None) -> str:
		import frappe

		suffix = suffix or frappe.db.get_single_value("Space Settings", "domain_suffix") or "zatgo.online"
		return f"{validate_slug(slug)}.{suffix}"
