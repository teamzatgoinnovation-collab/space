"""Generic Space Job runner — dispatches to handlers registered via hooks."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from space.registry import resolve_job_handler
from space.utils.activity import log_activity


def enqueue_job(
	job_type: str,
	*,
	resource: str | None = None,
	provider: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	queue: str = "long",
	timeout: int = 7200,
	**handler_kwargs,
) -> str:
	"""Create a Space Job and enqueue the registered handler."""
	job = frappe.get_doc(
		{
			"doctype": "Space Job",
			"job_type": job_type,
			"status": "Queued",
			"progress": 0,
			"resource": resource,
			"provider": provider,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"space.jobs.runner.run_job",
		queue=queue,
		timeout=timeout,
		job_id=f"space-job-{job.name}",
		space_job=job.name,
		handler_kwargs=handler_kwargs or {},
	)
	log_activity(f"Job {job_type} enqueued", "Space Job", job.name, reference_name)
	return job.name


def run_job(space_job: str, handler_kwargs: dict | None = None):
	job = frappe.get_doc("Space Job", space_job)
	handler_kwargs = handler_kwargs or {}
	try:
		job.status = "Running"
		job.started_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()

		handler = resolve_job_handler(job.job_type)
		result = handler(space_job=job.name, **handler_kwargs)

		job.reload()
		job.status = "Success"
		job.progress = 100
		job.finished_at = now_datetime()
		if result is not None and not job.output:
			job.output = str(result)[:4000]
		job.save(ignore_permissions=True)
		frappe.db.commit()
		return result
	except Exception as e:
		job.reload()
		job.status = "Failed"
		job.error_log = frappe.get_traceback() or str(e)
		job.finished_at = now_datetime()
		job.save(ignore_permissions=True)
		frappe.db.commit()
		raise
