"""Activity / audit helpers."""

from __future__ import annotations

import frappe


def log_activity(subject: str, ref_doctype: str | None = None, ref_name: str | None = None, details: str = ""):
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Space Activity Log",
				"subject": subject[:140],
				"ref_doctype": ref_doctype,
				"ref_name": ref_name,
				"user": frappe.session.user if frappe.session else None,
				"details": details[:5000] if details else None,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Space activity log failed")
