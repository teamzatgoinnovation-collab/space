# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, getdate, today


class SpaceSubscription(Document):
	@frappe.whitelist()
	def renew(self, days=30):
		self.end_date = add_days(getdate(self.end_date or today()), int(days))
		self.renewal_date = self.end_date
		self.status = "Active"
		if self.payment_status in ("Expired", "Suspended", "Unpaid"):
			self.payment_status = "Paid" if float(frappe.db.get_value("Space Plan", self.plan, "monthly_price") or 0) > 0 else "Free"
		self.save()
		return {"ok": True, "end_date": str(self.end_date), "renewal_date": str(self.renewal_date)}

	@frappe.whitelist()
	def suspend(self):
		self.status = "Suspended"
		self.payment_status = "Suspended"
		self.save()
		return {"ok": True}

	@frappe.whitelist()
	def change_plan(self, plan):
		if not frappe.db.exists("Space Plan", plan):
			frappe.throw("Unknown plan")
		self.plan = plan
		self.save()
		return {"ok": True, "plan": plan}
