frappe.ui.form.on("Space Domain", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Verify"), () => frm.call("verify").then(() => frm.reload_doc()));
		frm.add_custom_button(__("Attach"), () => frm.call("attach").then(() => frm.reload_doc()));
		frm.add_custom_button(__("Detach"), () => frm.call("detach").then(() => frm.reload_doc()));
	},
});
