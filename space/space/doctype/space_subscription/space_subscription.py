# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, getdate, today


class SpaceSubscription(Document):
	@frappe.whitelist()
	def renew(self, days=30):
		self.end_date = add_days(getdate(self.end_date or today()), int(days))
		self.status = "Active"
		self.save()
		return {"ok": True, "end_date": str(self.end_date)}

	@frappe.whitelist()
	def suspend(self):
		self.status = "Suspended"
		self.save()
		return {"ok": True}

	@frappe.whitelist()
	def change_plan(self, plan):
		if not frappe.db.exists("Space Plan", plan):
			frappe.throw("Unknown plan")
		self.plan = plan
		self.save()
		return {"ok": True, "plan": plan}
