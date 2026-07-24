"""Global search across Space DocTypes."""

from __future__ import annotations

import frappe


SEARCHABLE = [
	("Space Customer", ["name", "customer_name", "email", "company"]),
	("Space Site", ["name", "site_name", "domain", "status"]),
	("Space App", ["name", "app_name", "slug", "category", "description"]),
	("Space Deployment Job", ["name", "site", "job_type", "status"]),
	("Space Server", ["name", "server_name", "title", "ip_address"]),
	("Space Subscription", ["name", "customer", "plan", "status"]),
	("Space Support Ticket", ["name", "subject", "status", "priority"]),
]


def global_search(query: str, limit: int = 20) -> list[dict]:
	q = (query or "").strip()
	if len(q) < 2:
		return []
	like = f"%{q}%"
	results = []
	per = max(3, int(limit) // len(SEARCHABLE))
	for doctype, fields in SEARCHABLE:
		if not frappe.db.exists("DocType", doctype):
			continue
		or_filters = [[doctype, f, "like", like] for f in fields if frappe.db.has_column(doctype, f)]
		if not or_filters:
			continue
		try:
			rows = frappe.get_list(
				doctype,
				or_filters=or_filters,
				fields=["name"] + [f for f in fields if f != "name"][:3],
				limit_page_length=per,
				ignore_permissions=False,
			)
		except Exception:
			rows = frappe.get_all(
				doctype,
				or_filters=or_filters,
				fields=["name"],
				limit_page_length=per,
			)
		for r in rows:
			results.append({"doctype": doctype, "name": r.name, "title": r.get(fields[1], r.name) if hasattr(r, "get") else r.name, "row": dict(r)})
	return results[: int(limit or 20)]
