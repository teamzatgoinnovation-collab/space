"""Suspend / resume / delete site jobs."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from space.services import bench_client
from space.utils.activity import log_activity


def _enqueue(site_name: str, job_type: str, method: str) -> dict:
	site = frappe.get_doc("Space Site", site_name)
	job = frappe.get_doc(
		{
			"doctype": "Space Deployment Job",
			"site": site.name,
			"server": site.server,
			"job_type": job_type,
			"status": "Queued",
			"progress": 0,
		}
	).insert(ignore_permissions=True)
	site.job = job.name
	site.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue(
		method,
		queue="long",
		timeout=3600,
		job_id=f"space-{job_type.lower()}-{site.name}-{job.name}",
		deployment_job=job.name,
	)
	log_activity(f"{job_type} enqueued", "Space Site", site.name, job.name)
	return {"ok": True, "job": job.name}


def enqueue_suspend_site(site_name: str) -> dict:
	site = frappe.get_doc("Space Site", site_name)
	if site.status != "Active":
		frappe.throw("Only Active sites can be suspended")
	return _enqueue(site_name, "Suspend", "space.jobs.lifecycle.run_suspend_site")


def enqueue_resume_site(site_name: str) -> dict:
	site = frappe.get_doc("Space Site", site_name)
	if site.status != "Suspended":
		frappe.throw("Only Suspended sites can be resumed")
	return _enqueue(site_name, "Resume", "space.jobs.lifecycle.run_resume_site")


def enqueue_delete_site(site_name: str) -> dict:
	site = frappe.get_doc("Space Site", site_name)
	if site.status == "Deleted":
		frappe.throw("Site already deleted")
	return _enqueue(site_name, "Delete", "space.jobs.lifecycle.run_delete_site")


def run_suspend_site(deployment_job: str | None = None, job_name: str | None = None):
	_run_simple(deployment_job or job_name, maintenance=True, final_status="Suspended")


def run_resume_site(deployment_job: str | None = None, job_name: str | None = None):
	_run_simple(deployment_job or job_name, maintenance=False, final_status="Active")


def _run_simple(job_name: str, *, maintenance: bool, final_status: str):
	if not job_name:
		frappe.throw("deployment_job is required")
	job = frappe.get_doc("Space Deployment Job", job_name)
	site = frappe.get_doc("Space Site", job.site)
	try:
		job.status = "Running"
		job.started_at = now_datetime()
		job.progress = 20
		job.save(ignore_permissions=True)
		frappe.db.commit()

		bench_client.set_maintenance(job.server, site.domain, maintenance)
		job.progress = 80
		job.output = f"maintenance_mode={'on' if maintenance else 'off'}"
		job.save(ignore_permissions=True)

		site.reload()
		site.status = final_status
		site.save(ignore_permissions=True)

		job.reload()
		job.status = "Succeeded"
		job.progress = 100
		job.finished_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()
		log_activity(f"Site {final_status}", "Space Site", site.name)
	except Exception as e:
		job.reload()
		job.status = "Failed"
		job.error_log = frappe.get_traceback() or str(e)
		job.finished_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()
		raise


def run_delete_site(deployment_job: str | None = None, job_name: str | None = None):
	job_name = deployment_job or job_name
	if not job_name:
		frappe.throw("deployment_job is required")
	job = frappe.get_doc("Space Deployment Job", job_name)
	site = frappe.get_doc("Space Site", job.site)
	try:
		job.status = "Running"
		job.started_at = now_datetime()
		job.progress = 10
		job.save(ignore_permissions=True)
		frappe.db.commit()

		if site.domain in bench_client.list_sites(job.server):
			job.progress = 40
			job.output = f"Dropping {site.domain}"
			job.save(ignore_permissions=True)
			frappe.db.commit()
			bench_client.drop_site(job.server, site.domain)

		site.reload()
		site.status = "Deleted"
		site.save(ignore_permissions=True)

		job.reload()
		job.status = "Succeeded"
		job.progress = 100
		job.finished_at = now_datetime()
		job.output = (job.output or "") + "\nDeleted"
		job.save(ignore_permissions=True)
		frappe.db.commit()
		log_activity("Site deleted", "Space Site", site.name)
	except Exception as e:
		job.reload()
		job.status = "Failed"
		job.error_log = frappe.get_traceback() or str(e)
		job.finished_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()
		raise
