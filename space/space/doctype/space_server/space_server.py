# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document


class SpaceServer(Document):
	@frappe.whitelist()
	def test_connection(self):
		from space.services.bench_client import test_server_connection
		return test_server_connection(self.name)

	@frappe.whitelist()
	def refresh_statistics(self):
		from space.jobs.monitoring import refresh_server_health
		return refresh_server_health(self.name)

	@frappe.whitelist()
	def restart_docker(self):
		from space.services.bench_client import restart_backend
		frappe.only_for(("System Manager", "Space Admin"))
		return restart_backend(self.name)
