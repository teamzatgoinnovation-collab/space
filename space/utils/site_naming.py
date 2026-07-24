"""Site naming helpers."""

from __future__ import annotations

import re

import frappe

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def get_domain_suffix() -> str:
	try:
		return frappe.db.get_single_value("Space Settings", "domain_suffix") or "zatgo.online"
	except Exception:
		return "zatgo.online"


def reserved_slugs() -> set[str]:
	raw = ""
	try:
		raw = frappe.db.get_single_value("Space Settings", "reserved_slugs") or ""
	except Exception:
		pass
	base = {"space", "portal", "erp", "www", "mail", "api", "frontend"}
	extra = {s.strip().lower() for s in raw.replace("\n", ",").split(",") if s.strip()}
	return base | extra


def validate_slug(slug: str) -> str:
	s = (slug or "").strip().lower()
	if not SLUG_RE.match(s):
		frappe.throw("Enter a valid subdomain (lowercase letters, numbers, hyphens)")
	if s in reserved_slugs():
		frappe.throw(f"Subdomain '{s}' is reserved")
	return s


def build_domain(slug: str) -> str:
	s = validate_slug(slug)
	return f"{s}.{get_domain_suffix()}"
