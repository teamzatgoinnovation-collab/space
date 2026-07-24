frappe.ui.form.on("Space Backup", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Backup Now"), () => {
				frm.call("backup_now").then((r) => {
					frappe.show_alert({ message: __("Backup queued"), indicator: "green" });
					frm.reload_doc();
				});
			});
			if (frm.doc.status === "Succeeded") {
				frm.add_custom_button(__("Restore"), () => {
					frappe.confirm(__("Restore this backup?"), () => {
						frm.call("restore").then(() => {
							frappe.show_alert({ message: __("Restore queued"), indicator: "orange" });
						});
					});
				});
			}
			frm.add_custom_button(__("Delete Backup"), () => {
				frm.call("delete_backup").then(() => frm.reload_doc());
			});
		}
	},
});
