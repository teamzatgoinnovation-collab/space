frappe.ui.form.on("Space Deployment Job", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (["Failed", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () => frm.call("retry"));
		}
		if (["Queued", "Running"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Cancel"), () => frm.call("cancel").then(() => frm.reload_doc()));
		}
		if (frm.doc.can_rollback || frm.doc.status === "Failed") {
			frm.add_custom_button(__("Rollback"), () => {
				frappe.confirm(__("Restore latest backup for this site?"), () => frm.call("rollback"));
			});
		}
	},
});
