"""Health / monitoring jobs (Phase 2: metrics history + richer dashboard)."""

from __future__ import annotations

import json
import re

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
		rich = bench_client.get_backend_stats(server_name)
		stats.update(rich)
		sites = bench_client.list_sites(server_name)
		stats["sites"] = sites
		server.active_sites = len(sites)

		# Parse memory used if possible
		mem = rich.get("memory") or {}
		used_raw = (mem.get("used") or "").strip()
		m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", used_raw, re.I)
		if m:
			val = float(m.group(1))
			unit = m.group(2).upper()
			mult = {"B": 1 / 1024 / 1024, "KB": 1 / 1024, "KIB": 1 / 1024, "MB": 1, "MIB": 1, "GB": 1024, "GIB": 1024}.get(
				unit, 1
			)
			server.ram_used_mb = val * mult
		server.disk_used_mb = float(rich.get("disk_used_mb") or 0)
		if server.ram_mb and server.ram_used_mb:
			# soft CPU placeholder from queue pressure
			server.cpu_used_percent = min(100.0, float(rich.get("queue_length") or 0) * 5)

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

		# Historical snapshot
		frappe.get_doc(
			{
				"doctype": "Space Metric Snapshot",
				"server": server_name,
				"captured_at": now_datetime(),
				"cpu_percent": server.cpu_used_percent,
				"ram_percent": (server.ram_used_mb / server.ram_mb * 100) if server.ram_mb else 0,
				"ram_used_mb": server.ram_used_mb,
				"disk_used_mb": server.disk_used_mb,
				"disk_percent": (server.disk_used_mb / server.disk_mb * 100) if server.disk_mb else 0,
				"redis_ok": 1 if rich.get("redis_ok") else 0,
				"workers": rich.get("workers") or 0,
				"queue_length": rich.get("queue_length") or 0,
				"bench_status": rich.get("bench_status"),
				"scheduler_status": rich.get("scheduler_status"),
				"raw_json": json.dumps(stats, default=str)[:100000],
			}
		).insert(ignore_permissions=True)

	except Exception as e:
		health = "Critical"
		stats["error"] = str(e)[:500]

	server.health = health
	server.last_health_at = now_datetime()
	server.stats_json = json.dumps(stats, indent=2, default=str)[:100000]
	server.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "health": health, "stats": stats}


def dashboard_summary() -> dict:
	"""Phase 1-compatible summary + Phase 2 extras."""
	running = frappe.db.count("Space Deployment Job", {"status": ("in", ["Queued", "Running"])})
	trials = frappe.db.count("Space Subscription", {"status": "Trial"})
	expiring = frappe.db.count(
		"Space Subscription",
		{"status": ("in", ["Active", "Trial"]), "end_date": ("<=", frappe.utils.add_days(frappe.utils.today(), 14))},
	)
	backups_ok = frappe.db.count("Space Backup", {"status": "Succeeded"})
	backups_failed = frappe.db.count("Space Backup", {"status": "Failed"})
	revenue = frappe.db.sql(
		"""
		select coalesce(sum(amount), 0) from `tabSpace Invoice`
		where payment_status = 'Paid' and status != 'Void'
		"""
	)[0][0]

	servers = frappe.get_all(
		"Space Server",
		fields=["name", "health", "cpu_used_percent", "ram_used_mb", "disk_used_mb", "status"],
	)
	cpu_avg = 0.0
	disk_total = 0.0
	if servers:
		cpu_avg = sum(float(s.cpu_used_percent or 0) for s in servers) / len(servers)
		disk_total = sum(float(s.disk_used_mb or 0) for s in servers)

	return {
		"customers": frappe.db.count("Space Customer"),
		"servers": frappe.db.count("Space Server"),
		"sites": frappe.db.count("Space Site", {"status": ("!=", "Deleted")}),
		"active_sites": frappe.db.count("Space Site", {"status": "Active"}),
		"failed_jobs": frappe.db.count("Space Deployment Job", {"status": "Failed"}),
		"running_jobs": running,
		"subscriptions": frappe.db.count("Space Subscription", {"status": ("in", ["Active", "Trial"])}),
		"trials": trials,
		"expiring_plans": expiring,
		"revenue": float(revenue or 0),
		"cpu_usage": round(cpu_avg, 2),
		"disk_usage": round(disk_total, 2),
		"backup_status": {"succeeded": backups_ok, "failed": backups_failed},
		"server_health": [
			{"name": s.name, "health": s.health, "status": s.status} for s in servers
		],
	}


def metrics_history(server: str | None = None, site: str | None = None, limit: int = 48) -> list[dict]:
	filters = {}
	if server:
		filters["server"] = server
	if site:
		filters["site"] = site
	return frappe.get_all(
		"Space Metric Snapshot",
		filters=filters,
		fields=[
			"name",
			"server",
			"site",
			"captured_at",
			"cpu_percent",
			"ram_percent",
			"ram_used_mb",
			"disk_used_mb",
			"disk_percent",
			"mariadb_size_mb",
			"redis_ok",
			"workers",
			"queue_length",
			"bench_status",
			"scheduler_status",
		],
		order_by="captured_at desc",
		limit_page_length=min(int(limit or 48), 200),
	)
