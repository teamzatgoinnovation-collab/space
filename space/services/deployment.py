"""Deployment job helpers: timeline, cancel, retry flags."""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime


def append_timeline(job_name: str, event: str, detail: str = "", progress: int | None = None):
	job = frappe.get_doc("Space Deployment Job", job_name)
	try:
		timeline = json.loads(job.timeline_json or "[]")
	except Exception:
		timeline = []
	timeline.append(
		{
			"at": str(now_datetime()),
			"event": event,
			"detail": detail,
			"progress": progress if progress is not None else job.progress,
		}
	)
	job.timeline_json = json.dumps(timeline)[-100000:]
	line = f"[{job.progress}%] {event}: {detail}".strip(": ")
	job.live_log = ((job.live_log or "") + "\n" + line).strip()[-50000:]
	if progress is not None:
		job.progress = progress
	job.save(ignore_permissions=True)
	frappe.db.commit()


def mark_retryable(job_name: str, *, can_rollback: bool = False):
	job = frappe.get_doc("Space Deployment Job", job_name)
	job.can_retry = 1 if job.status == "Failed" else 0
	job.can_cancel = 1 if job.status in ("Queued", "Running") else 0
	job.can_rollback = 1 if can_rollback else 0
	job.save(ignore_permissions=True)
	frappe.db.commit()


def cancel_job(job_name: str) -> dict:
	job = frappe.get_doc("Space Deployment Job", job_name)
	if job.status not in ("Queued", "Running"):
		frappe.throw(f"Cannot cancel job in status {job.status}")
	job.status = "Cancelled"
	job.finished_at = now_datetime()
	job.can_cancel = 0
	job.can_retry = 1
	job.output = ((job.output or "") + "\nCancelled by user").strip()
	job.save(ignore_permissions=True)
	frappe.db.commit()
	append_timeline(job_name, "cancelled", "Cancelled by user")
	return {"ok": True, "job": job_name}


def sanitize_job_dict(d: dict) -> dict:
	"""Hide sensitive fragments from job payloads returned to customers."""
	out = dict(d)
	for key in ("error_log", "output", "live_log"):
		val = out.get(key)
		if isinstance(val, str):
			for needle in ("password", "PRIVATE KEY", "BEGIN RSA", "BEGIN OPENSSH"):
				if needle.lower() in val.lower():
					out[key] = "[redacted sensitive output]"
					break
	return out
