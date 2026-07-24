# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document


class SpaceBackup(Document):
	@frappe.whitelist()
	def backup_now(self):
		from space.jobs.backup import enqueue_backup

		return enqueue_backup(self.site, backup_type="Manual")

	@frappe.whitelist()
	def restore(self):
		from space.jobs.backup import enqueue_restore

		return enqueue_restore(self.name)

	@frappe.whitelist()
	def delete_backup(self):
		from space.jobs.backup import delete_backup_record

		return delete_backup_record(self.name)
