"""Daily analytics snapshots — MRR/ARR/churn/downloads."""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, today


def capture_daily_analytics():
	day = today()
	if frappe.db.exists("Space Analytics Snapshot", day):
		doc = frappe.get_doc("Space Analytics Snapshot", day)
	else:
		doc = frappe.get_doc({"doctype": "Space Analytics Snapshot", "snapshot_date": day})

	paid = frappe.db.sql(
		"""
		select coalesce(sum(amount),0) from `tabSpace Invoice`
		where payment_status='Paid' and status!='Void'
		  and period_start >= %s
		""",
		(day[:8] + "01" if len(day) >= 8 else day,),
	)[0][0]
	mrr = frappe.db.sql(
		"""
		select coalesce(sum(p.monthly_price),0)
		from `tabSpace Subscription` s
		join `tabSpace Plan` p on p.name=s.plan
		where s.status in ('Active','Trial')
		"""
	)[0][0]
	active_customers = frappe.db.count("Space Customer", {"status": "Active"}) if frappe.db.has_column("Space Customer", "status") else frappe.db.count("Space Customer")
	new_customers = frappe.db.count("Space Customer", {"creation": (">=", day)})
	# simple churn: expired subs today / active yesterday proxy
	expired = frappe.db.count("Space Subscription", {"status": "Expired", "modified": (">=", day)})
	churn = (expired / max(active_customers, 1)) * 100.0
	deployments = (
		frappe.db.count("Space Deployment Job", {"creation": (">=", day)})
		if frappe.db.exists("DocType", "Space Deployment Job")
		else 0
	)
	downloads = frappe.db.sql("select coalesce(sum(downloads),0) from `tabSpace App`")[0][0] if frappe.db.exists("DocType", "Space App") else 0
	top = []
	if frappe.db.exists("DocType", "Space App"):
		top = frappe.get_all(
			"Space App",
			filters={"status": "Published"},
			fields=["name", "app_name", "downloads", "avg_rating"],
			order_by="downloads desc",
			limit_page_length=10,
		)
	servers = []
	if frappe.db.exists("DocType", "Space Server"):
		servers = frappe.get_all(
			"Space Server",
			fields=["name", "cpu_used_percent", "ram_used_mb", "disk_used_mb", "active_sites", "health"],
		)

	doc.revenue = float(paid or 0)
	doc.mrr = float(mrr or 0)
	doc.arr = float(mrr or 0) * 12
	doc.new_customers = int(new_customers or 0)
	doc.active_customers = int(active_customers or 0)
	doc.churn = round(churn, 2)
	doc.deployments = int(deployments or 0)
	doc.marketplace_downloads = int(downloads or 0)
	doc.top_apps_json = json.dumps(top, default=str)
	doc.server_usage_json = json.dumps(servers, default=str)
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


def analytics_summary() -> dict:
	latest = frappe.get_all(
		"Space Analytics Snapshot",
		fields=["*"],
		order_by="snapshot_date desc",
		limit_page_length=1,
	)
	return latest[0] if latest else capture_daily_analytics()
