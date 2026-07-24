"""Backup / restore background jobs."""

from __future__ import annotations

import re

import frappe
from frappe.utils import now_datetime

from space.services import bench_client, deployment, notifications
from space.utils.activity import log_activity


def enqueue_backup(site_name: str, *, backup_type: str = "Manual") -> dict:
	site = frappe.get_doc("Space Site", site_name)
	if site.status not in ("Active", "Suspended"):
		frappe.throw(f"Cannot backup site in status {site.status}")

	backup = frappe.get_doc(
		{
			"doctype": "Space Backup",
			"site": site.name,
			"server": site.server,
			"backup_type": backup_type if backup_type in ("Manual", "Automatic") else "Manual",
			"status": "Queued",
		}
	).insert(ignore_permissions=True)

	job = frappe.get_doc(
		{
			"doctype": "Space Deployment Job",
			"site": site.name,
			"server": site.server,
			"job_type": "Backup",
			"status": "Queued",
			"progress": 0,
			"estimated_minutes": 10,
			"can_cancel": 1,
		}
	).insert(ignore_permissions=True)

	backup.job = job.name
	backup.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"space.jobs.backup.run_backup",
		queue="long",
		timeout=3600,
		job_id=f"space-backup-{site.name}-{backup.name}",
		backup_name=backup.name,
		deployment_job=job.name,
	)
	log_activity("Backup enqueued", "Space Backup", backup.name, job.name)
	return {"ok": True, "backup": backup.name, "job": job.name}


def run_backup(backup_name: str, deployment_job: str):
	backup = frappe.get_doc("Space Backup", backup_name)
	job = frappe.get_doc("Space Deployment Job", deployment_job)
	site = frappe.get_doc("Space Site", backup.site)

	def progress(pct: int, msg: str):
		job.reload()
		if job.status == "Cancelled":
			frappe.throw("Job cancelled")
		job.progress = pct
		job.status = "Running"
		job.output = ((job.output or "") + f"\n[{pct}%] {msg}").strip()
		if not job.started_at:
			job.started_at = now_datetime()
		job.save(ignore_permissions=True)
		backup.reload()
		backup.status = "Running"
		backup.started_at = backup.started_at or now_datetime()
		backup.save(ignore_permissions=True)
		frappe.db.commit()
		deployment.append_timeline(job.name, "progress", msg, pct)

	try:
		progress(10, "Starting bench backup")
		result = bench_client.backup_site(site.server, site.domain)
		stdout = result.get("stdout") or ""
		progress(80, "Parsing backup path")
		# Typical: Backup Summary ... /path/to/site.sql.gz
		path = None
		for line in stdout.splitlines():
			m = re.search(r"(/home/frappe/frappe-bench/sites/[^\s]+\.(sql\.gz|tgz|tar))", line)
			if m:
				path = m.group(1)
		if not path:
			m = re.search(r"(sites/[^\s]+\.(sql\.gz|tgz))", stdout)
			if m:
				path = m.group(1)

		size_mb = 0.0
		if path:
			try:
				r = bench_client.run_on_bench(site.server, ["du", "-sm", path], timeout_s=30)
				size_mb = float((r["stdout"] or "0").strip().split()[0])
			except Exception:
				pass

		backup.reload()
		backup.status = "Succeeded"
		backup.file_path = path
		backup.file_size_mb = size_mb
		backup.finished_at = now_datetime()
		backup.output = stdout[-20000:]
		backup.is_restore_point = 1
		backup.save(ignore_permissions=True)

		job.reload()
		job.status = "Succeeded"
		job.progress = 100
		job.finished_at = now_datetime()
		job.output = ((job.output or "") + "\n" + stdout).strip()[-50000:]
		job.can_cancel = 0
		job.can_retry = 0
		job.save(ignore_permissions=True)

		site.reload()
		site.last_backup = now_datetime()
		site.save(ignore_permissions=True)
		frappe.db.commit()

		notifications.notify(
			title=f"Backup finished: {site.name}",
			event_type="backup_finished",
			message=f"Backup {backup.name} completed ({size_mb} MB)",
			customer=site.customer,
			ref_doctype="Space Backup",
			ref_name=backup.name,
		)
		log_activity("Backup succeeded", "Space Backup", backup.name)
	except Exception as e:
		backup.reload()
		backup.status = "Failed"
		backup.error_log = str(e)[:5000]
		backup.finished_at = now_datetime()
		backup.save(ignore_permissions=True)
		job.reload()
		job.status = "Failed"
		job.error_log = str(e)[:5000]
		job.finished_at = now_datetime()
		job.can_retry = 1
		job.can_cancel = 0
		job.save(ignore_permissions=True)
		frappe.db.commit()
		notifications.notify(
			title=f"Backup failed: {site.name}",
			event_type="backup_failed",
			message=str(e)[:500],
			customer=site.customer,
			ref_doctype="Space Backup",
			ref_name=backup.name,
		)
		deployment.mark_retryable(job.name)
		raise


def enqueue_restore(backup_name: str) -> dict:
	backup = frappe.get_doc("Space Backup", backup_name)
	if backup.status != "Succeeded" or not backup.file_path:
		frappe.throw("Backup is not restorable")
	site = frappe.get_doc("Space Site", backup.site)

	job = frappe.get_doc(
		{
			"doctype": "Space Deployment Job",
			"site": site.name,
			"server": site.server,
			"job_type": "Restore",
			"status": "Queued",
			"progress": 0,
			"estimated_minutes": 20,
			"can_cancel": 1,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"space.jobs.backup.run_restore",
		queue="long",
		timeout=7200,
		job_id=f"space-restore-{site.name}-{job.name}",
		backup_name=backup.name,
		deployment_job=job.name,
	)
	return {"ok": True, "job": job.name, "backup": backup.name}


def run_restore(backup_name: str, deployment_job: str):
	backup = frappe.get_doc("Space Backup", backup_name)
	job = frappe.get_doc("Space Deployment Job", deployment_job)
	site = frappe.get_doc("Space Site", backup.site)
	try:
		job.status = "Running"
		job.started_at = now_datetime()
		job.progress = 20
		job.save(ignore_permissions=True)
		frappe.db.commit()
		# Prefer SQL dump path for restore
		path = backup.file_path
		if path and path.endswith(".tgz"):
			# try sibling .sql.gz
			alt = path.replace("-files.tgz", ".sql.gz").replace(".tgz", ".sql.gz")
			path = alt
		result = bench_client.restore_site(site.server, site.domain, path)
		job.reload()
		job.status = "Succeeded"
		job.progress = 100
		job.finished_at = now_datetime()
		job.output = (result.get("stdout") or "")[-50000:]
		job.save(ignore_permissions=True)
		frappe.db.commit()
		log_activity("Restore succeeded", "Space Backup", backup.name)
	except Exception as e:
		job.reload()
		job.status = "Failed"
		job.error_log = str(e)[:5000]
		job.finished_at = now_datetime()
		job.can_retry = 1
		job.save(ignore_permissions=True)
		frappe.db.commit()
		notifications.notify(
			title=f"Restore failed: {site.name}",
			event_type="deployment_failed",
			message=str(e)[:500],
			customer=site.customer,
			ref_doctype="Space Deployment Job",
			ref_name=job.name,
		)
		raise


def delete_backup_record(backup_name: str) -> dict:
	backup = frappe.get_doc("Space Backup", backup_name)
	backup.status = "Deleted"
	backup.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


def enqueue_scheduled_backups():
	settings = frappe.get_single("Space Settings")
	if (settings.backup_schedule or "Disabled") == "Disabled":
		return
	for name in frappe.get_all("Space Site", filters={"status": "Active"}, pluck="name"):
		try:
			enqueue_backup(name, backup_type="Automatic")
		except Exception:
			frappe.log_error(title=f"Auto backup enqueue failed for {name}")


def cleanup_old_backups():
	settings = frappe.get_single("Space Settings")
	days = int(settings.backup_retention_days or 14)
	if days <= 0:
		return
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -days)
	old = frappe.get_all(
		"Space Backup",
		filters={"status": "Succeeded", "finished_at": ("<", cutoff)},
		pluck="name",
	)
	for name in old:
		try:
			delete_backup_record(name)
		except Exception:
			pass
