"""Server pool selection — Preferred / Round Robin / Least Loaded."""

from __future__ import annotations

import frappe


def select_server(preferred: str | None = None) -> str:
	"""Pick an Active, non-maintenance server for a new site."""
	settings = frappe.get_single("Space Settings")
	mode = (settings.server_selection or "Preferred").strip()

	if preferred and _is_eligible(preferred):
		return preferred

	prefer = settings.prefer_server or None
	if mode == "Preferred" and prefer and _is_eligible(prefer):
		return prefer

	# default / preferred fallback
	default = frappe.db.get_value("Space Server", {"is_default": 1, "status": "Active"}, "name")
	if default and _is_eligible(default) and mode == "Preferred":
		return default

	candidates = frappe.get_all(
		"Space Server",
		filters={"status": "Active"},
		fields=["name", "active_sites", "max_sites", "ram_mb", "ram_used_mb", "disk_mb", "disk_used_mb", "weight", "health"],
		order_by="is_default desc, weight desc, active_sites asc",
	)
	eligible = [c for c in candidates if _capacity_ok(c)]
	if not eligible:
		frappe.throw("No eligible Space Server with capacity")

	if mode == "Round Robin":
		# least recently used by active_sites then name
		eligible.sort(key=lambda c: (c.active_sites or 0, c.name))
		return eligible[0].name

	# Least Loaded
	def load_score(c):
		sites = c.active_sites or 0
		max_s = c.max_sites or 50
		ram_pct = (c.ram_used_mb or 0) / max(c.ram_mb or 1, 1)
		disk_pct = (c.disk_used_mb or 0) / max(c.disk_mb or 1, 1)
		return (sites / max(max_s, 1)) + ram_pct + disk_pct

	eligible.sort(key=load_score)
	return eligible[0].name


def _is_eligible(name: str) -> bool:
	row = frappe.db.get_value(
		"Space Server",
		name,
		["status", "active_sites", "max_sites", "health"],
		as_dict=True,
	)
	if not row or row.status != "Active":
		return False
	return _capacity_ok(row)


def _capacity_ok(row) -> bool:
	max_sites = row.get("max_sites") if isinstance(row, dict) else getattr(row, "max_sites", None)
	active = row.get("active_sites") if isinstance(row, dict) else getattr(row, "active_sites", None)
	max_sites = max_sites or 50
	active = active or 0
	return active < max_sites


def capacity_summary(server_name: str | None = None) -> list[dict]:
	filters = {}
	if server_name:
		filters["name"] = server_name
	rows = frappe.get_all(
		"Space Server",
		filters=filters,
		fields=[
			"name",
			"title",
			"status",
			"health",
			"cpu_cores",
			"cpu_used_percent",
			"ram_mb",
			"ram_used_mb",
			"disk_mb",
			"disk_used_mb",
			"active_sites",
			"max_sites",
			"weight",
			"is_default",
		],
	)
	out = []
	for r in rows:
		out.append(
			{
				**r,
				"capacity_ok": _capacity_ok(r),
				"sites_remaining": max(0, (r.max_sites or 50) - (r.active_sites or 0)),
			}
		)
	return out
