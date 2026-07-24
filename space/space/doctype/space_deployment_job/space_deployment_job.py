# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document


class SpaceDeploymentJob(Document):
	@frappe.whitelist()
	def retry(self):
		from space.jobs.backup import enqueue_backup, enqueue_restore
		from space.jobs.lifecycle import enqueue_delete_site, enqueue_resume_site, enqueue_suspend_site
		from space.jobs.provision import enqueue_create_site

		if self.status not in ("Failed", "Cancelled"):
			frappe.throw(f"Cannot retry job in status {self.status}")
		jt = self.job_type
		if jt == "Create":
			return enqueue_create_site(self.site)
		if jt == "Suspend":
			return enqueue_suspend_site(self.site)
		if jt == "Resume":
			return enqueue_resume_site(self.site)
		if jt == "Delete":
			return enqueue_delete_site(self.site)
		if jt == "Backup":
			return enqueue_backup(self.site)
		if jt == "Restore":
			# need last succeeded backup
			backup = frappe.db.get_value(
				"Space Backup",
				{"site": self.site, "status": "Succeeded"},
				"name",
				order_by="finished_at desc",
			)
			if not backup:
				frappe.throw("No restore point")
			return enqueue_restore(backup)
		frappe.throw(f"Retry not supported for {jt}")

	@frappe.whitelist()
	def cancel(self):
		from space.services.deployment import cancel_job

		return cancel_job(self.name)

	@frappe.whitelist()
	def rollback(self):
		"""Rollback = restore latest restore-point backup."""
		from space.jobs.backup import enqueue_restore

		backup = frappe.db.get_value(
			"Space Backup",
			{"site": self.site, "status": "Succeeded", "is_restore_point": 1},
			"name",
			order_by="finished_at desc",
		)
		if not backup:
			frappe.throw("No restore point available")
		return enqueue_restore(backup)
