"""Create-site deployment job."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from space.services import bench_client
from space.utils.activity import log_activity


def enqueue_create_site(site_name: str) -> dict:
	site = frappe.get_doc("Space Site", site_name)
	if site.status not in ("Draft", "Failed"):
		frappe.throw(f"Cannot create site in status {site.status}")

	job = frappe.get_doc(
		{
			"doctype": "Space Deployment Job",
			"site": site.name,
			"server": site.server,
			"job_type": "Create",
			"status": "Queued",
			"progress": 0,
		}
	).insert(ignore_permissions=True)

	site.status = "Provisioning"
	site.job = job.name
	site.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"space.jobs.provision.run_create_site",
		job_name=job.name,
		queue="long",
		timeout=7200,
		job_id=f"space-create-{site.name}-{job.name}",
	)
	log_activity("Create site enqueued", "Space Site", site.name, job.name)
	return {"ok": True, "job": job.name}


def run_create_site(job_name: str):
	job = frappe.get_doc("Space Deployment Job", job_name)
	site = frappe.get_doc("Space Site", job.site)
	server = job.server

	def progress(pct: int, msg: str):
		job.reload()
		job.progress = pct
		job.status = "Running"
		job.output = ((job.output or "") + f"\n[{pct}%] {msg}").strip()
		if not job.started_at:
			job.started_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()

	try:
		progress(5, "Validating plan and subscription")
		plan = frappe.get_doc("Space Plan", site.plan)
		if not plan.is_active:
			frappe.throw("Plan is not active")

		admin_password = None
		try:
			admin_password = site.get_password("admin_password")
		except Exception:
			pass
		if not admin_password:
			frappe.throw("Admin password is required on the site document before create")

		progress(15, "Checking DNS / hostname")
		domain = site.domain
		existing = bench_client.list_sites(server)
		if domain in existing:
			frappe.throw(f"Site directory already exists on bench: {domain}")

		progress(25, f"Creating site {domain}")
		install_erpnext = any(
			(r.app_package or "").lower() == "erpnext" for r in (plan.allowed_apps or [])
		) or True
		bench_client.new_site(
			server,
			domain,
			admin_password,
			install_erpnext=install_erpnext,
		)

		apps = []
		for row in plan.allowed_apps or []:
			pkg = (row.app_package or "").strip()
			if pkg and pkg not in ("frappe",) and pkg not in apps:
				# erpnext may already be installed via new-site
				if pkg == "erpnext" and install_erpnext:
					continue
				apps.append(pkg)

		total = max(1, len(apps))
		for i, pkg in enumerate(apps):
			pct = 40 + int(40 * (i / total))
			progress(pct, f"Installing app {pkg}")
			try:
				bench_client.install_app(server, domain, pkg)
			except bench_client.BenchError as e:
				# continue but log
				job.reload()
				job.output = (job.output or "") + f"\nWARN install {pkg}: {e}"
				job.save(ignore_permissions=True)
				frappe.db.commit()

		progress(85, "Migrating / clearing cache")
		try:
			bench_client.migrate_site(server, domain)
		except bench_client.BenchError:
			pass
		bench_client.clear_cache(server, domain)

		progress(95, "Recording installed apps")
		installed = bench_client.list_apps_on_site(server, domain)
		site.reload()
		site.set("installed_apps", [])
		for pkg in installed:
			site.append(
				"installed_apps",
				{"app_package": pkg, "app_title": pkg.replace("_", " ").title()},
			)
		site.status = "Active"
		site.storage_used_mb = bench_client.get_site_disk_mb(server, domain)
		site.save(ignore_permissions=True)

		job.reload()
		job.status = "Succeeded"
		job.progress = 100
		job.finished_at = now_datetime()
		job.output = (job.output or "") + "\nDone"
		job.save(ignore_permissions=True)
		frappe.db.commit()
		log_activity("Site created", "Space Site", site.name, domain)

	except Exception as e:
		frappe.db.rollback()
		job.reload()
		job.status = "Failed"
		job.error_log = frappe.get_traceback() or str(e)
		job.finished_at = now_datetime()
		job.save(ignore_permissions=True)
		try:
			site.reload()
			site.status = "Failed"
			site.save(ignore_permissions=True)
		except Exception:
			pass
		frappe.db.commit()
		log_activity("Site create failed", "Space Site", site.name, str(e)[:500])
		raise
