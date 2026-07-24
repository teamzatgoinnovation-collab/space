frappe.ui.form.on("Space Provider", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Test Connection"), () => {
				frm.call("test_connection").then((r) => frappe.msgprint({ message: JSON.stringify(r.message), indicator: "green" }));
			});
			frm.add_custom_button(__("Health Check"), () => {
				frm.call("health_check").then((r) => frappe.msgprint({ message: JSON.stringify(r.message), indicator: "blue" }));
			});
		}
	},
});
