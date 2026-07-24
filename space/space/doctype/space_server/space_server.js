frappe.ui.form.on("Space Server", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "test_connection",
				doc: frm.doc,
				freeze: true,
				callback: (r) => {
					frappe.msgprint({ title: __("Connection"), message: JSON.stringify(r.message, null, 2) });
				},
			});
		});
		frm.add_custom_button(__("Refresh Statistics"), () => {
			frappe.call({
				method: "refresh_statistics",
				doc: frm.doc,
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		});
		if (frappe.user.has_role("System Manager") || frappe.user.has_role("Space Admin")) {
			frm.add_custom_button(__("Restart Docker"), () => {
				frappe.confirm(__("Restart backend container?"), () => {
					frappe.call({ method: "restart_docker", doc: frm.doc, freeze: true });
				});
			});
		}
	},
});
