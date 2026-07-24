"""Health / monitoring jobs."""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from space.services import bench_client


def refresh_all_health():
	servers = frappe.get_all("Space Server", filters={"status": ("!=", "Offline")}, pluck="name")
	for name in servers:
		try:
			refresh_server_health(name)
		except Exception:
			frappe.log_error(title=f"Space health failed for {name}")


def refresh_server_health(server_name: str) -> dict:
	server = frappe.get_doc("Space Server", server_name)
	stats: dict = {}
	health = "Healthy"
	try:
		ver = bench_client.test_server_connection(server_name)
		stats["bench_version"] = ver.get("version")
		sites = bench_client.list_sites(server_name)
		stats["sites"] = sites
		server.active_sites = len(sites)
		mem = bench_client.get_backend_mem(server_name)
		stats["memory"] = mem

		# Update disk for Active sites on this server
		for site_name in frappe.get_all(
			"Space Site",
			filters={"server": server_name, "status": ("in", ["Active", "Suspended", "Provisioning"])},
			pluck="name",
		):
			site = frappe.get_doc("Space Site", site_name)
			if not site.domain:
				continue
			try:
				site.storage_used_mb = bench_client.get_site_disk_mb(server_name, site.domain)
				site.save(ignore_permissions=True)
			except Exception:
				pass

	except Exception as e:
		health = "Critical"
		stats["error"] = str(e)[:500]

	server.health = health
	server.last_health_at = now_datetime()
	server.stats_json = json.dumps(stats, indent=2)[:100000]
	server.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "health": health, "stats": stats}


def dashboard_summary() -> dict:
	return {
		"customers": frappe.db.count("Space Customer"),
		"servers": frappe.db.count("Space Server"),
		"sites": frappe.db.count("Space Site", {"status": ("!=", "Deleted")}),
		"active_sites": frappe.db.count("Space Site", {"status": "Active"}),
		"failed_jobs": frappe.db.count("Space Deployment Job", {"status": "Failed"}),
		"subscriptions": frappe.db.count("Space Subscription", {"status": ("in", ["Active", "Trial"])}),
	}
