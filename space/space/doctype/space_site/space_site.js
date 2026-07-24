frappe.ui.form.on("Space Site", {
	refresh(frm) {
		frm.add_custom_button(__("Open Site"), () => {
			if (frm.doc.domain) window.open("https://" + frm.doc.domain, "_blank");
		});
		if (["Draft", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Create Site"), () => {
				frappe.call({
					method: "create_site",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Enqueueing create…"),
					callback: (r) => {
						frm.reload_doc();
						frappe.show_alert({ message: __("Create job started"), indicator: "green" });
					},
				});
			}).addClass("btn-primary");
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Suspend"), () => {
				frappe.call({ method: "suspend_site", doc: frm.doc, freeze: true, callback: () => frm.reload_doc() });
			});
		}
		if (frm.doc.status === "Suspended") {
			frm.add_custom_button(__("Resume"), () => {
				frappe.call({ method: "resume_site", doc: frm.doc, freeze: true, callback: () => frm.reload_doc() });
			});
		}
		if (frm.doc.status !== "Deleted") {
			frm.add_custom_button(__("Delete"), () => {
				frappe.confirm(__("Drop this site from the bench?"), () => {
					frappe.call({ method: "delete_site", doc: frm.doc, freeze: true, callback: () => frm.reload_doc() });
				});
			});
		}
		if (["Active", "Suspended"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Backup Now"), () => {
				frappe.call({
					method: "backup_now",
					doc: frm.doc,
					freeze: true,
					callback: () => {
						frappe.show_alert({ message: __("Backup queued"), indicator: "green" });
						frm.reload_doc();
					},
				});
			});
		}
		if (frm.doc.job) {
			frm.add_custom_button(__("View Job"), () => {
				frappe.set_route("Form", "Space Deployment Job", frm.doc.job);
			});
		}
	},
});
