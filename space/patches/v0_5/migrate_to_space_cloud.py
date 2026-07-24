"""Reassign hosting DocTypes to Space Cloud module; seed Provider/Resource links."""

from __future__ import annotations

import json

import frappe


HOSTING_DOCTYPES = [
	"Space Server",
	"Space Site",
	"Space Site App",
	"Space Deployment Job",
	"Space Backup",
	"Space Domain",
	"Space Metric Snapshot",
	"Space Meter Snapshot",
	"Space Region",
	"Space Cluster",
	"Space Node",
	"Space Availability Zone",
	"Space Storage Pool",
	"Space Volume",
	"Space DR Plan",
	"Space Firewall Rule",
	"Space IP Allow List",
	"Space Secret",
	"Space Observability Log",
	"Space Capacity Forecast",
	"Space Maintenance Window",
	"Space Site Migration",
	"Space Migration History",
	"Space Update Queue",
	"Space App Install History",
	"Space Alert",
	"Space Alert Rule",
]


def execute():
	_reassign_modules()
	_ensure_provider()
	_link_sites_as_resources()
	_mirror_deployment_jobs()
	frappe.db.commit()


def _reassign_modules():
	for dt in HOSTING_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		frappe.db.set_value("DocType", dt, "module", "Space Cloud", update_modified=False)
		# Ensure Module Def exists
		if not frappe.db.exists("Module Def", "Space Cloud"):
			frappe.get_doc(
				{
					"doctype": "Module Def",
					"module_name": "Space Cloud",
					"app_name": "space_cloud",
				}
			).insert(ignore_permissions=True)


def _ensure_provider():
	if not frappe.db.exists("DocType", "Space Provider"):
		return
	if frappe.db.exists("Space Provider", "docker-bench-primary"):
		return
	frappe.get_doc(
		{
			"doctype": "Space Provider",
			"provider_name": "docker-bench-primary",
			"title": "DigitalOcean Docker Bench",
			"provider_type": "docker_bench",
			"status": "Active",
			"is_default": 1,
			"capabilities": "bench\ndocker\nssh\nsites",
			"config_json": json.dumps({"server": "primary-do", "host": "157.230.8.164"}),
		}
	).insert(ignore_permissions=True)


def _link_sites_as_resources():
	if not frappe.db.exists("DocType", "Space Site") or not frappe.db.exists("DocType", "Space Resource"):
		return
	provider = "docker-bench-primary" if frappe.db.exists("Space Provider", "docker-bench-primary") else None
	for site in frappe.get_all(
		"Space Site",
		fields=["name", "site_name", "domain", "status", "customer"],
		filters={"status": ("!=", "Deleted")},
	):
		existing = frappe.db.exists(
			"Space Resource",
			{"reference_doctype": "Space Site", "reference_name": site.name},
		)
		if existing:
			continue
		status = site.status if site.status in ("Draft", "Active", "Suspended", "Deleted", "Failed") else "Active"
		frappe.get_doc(
			{
				"doctype": "Space Resource",
				"resource_type": "site",
				"title": site.domain or site.site_name or site.name,
				"status": status,
				"provider": provider,
				"customer": site.customer,
				"external_id": site.site_name or site.name,
				"reference_doctype": "Space Site",
				"reference_name": site.name,
			}
		).insert(ignore_permissions=True)


def _mirror_deployment_jobs():
	"""Create Space Job envelopes for recent Deployment Jobs (idempotent)."""
	if not frappe.db.exists("DocType", "Space Deployment Job") or not frappe.db.exists("DocType", "Space Job"):
		return
	type_map = {
		"Create": "create_site",
		"Suspend": "suspend_site",
		"Resume": "resume_site",
		"Delete": "delete_site",
	}
	for job in frappe.get_all(
		"Space Deployment Job",
		fields=["name", "job_type", "status", "progress", "site", "server", "started_at", "finished_at"],
		order_by="creation desc",
		limit_page_length=200,
	):
		if frappe.db.exists("Space Job", {"reference_doctype": "Space Deployment Job", "reference_name": job.name}):
			continue
		resource = None
		if job.site:
			resource = frappe.db.get_value(
				"Space Resource",
				{"reference_doctype": "Space Site", "reference_name": job.site},
			)
		status = job.status if job.status in ("Queued", "Running", "Success", "Failed", "Cancelled") else "Queued"
		if status == "Completed":
			status = "Success"
		frappe.get_doc(
			{
				"doctype": "Space Job",
				"job_type": type_map.get(job.job_type, (job.job_type or "unknown").lower()),
				"status": status,
				"progress": job.progress or 0,
				"resource": resource,
				"reference_doctype": "Space Deployment Job",
				"reference_name": job.name,
				"started_at": job.started_at,
				"finished_at": job.finished_at,
			}
		).insert(ignore_permissions=True)
