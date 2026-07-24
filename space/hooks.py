"""Frappe hooks for Space control plane."""

app_name = "space"
app_title = "Space"
app_publisher = "ZatGo Innovation"
app_description = "Frappe Cloud-like control plane for ERPNext sites"
app_email = "engineering@zatgo.local"
app_license = "mit"
app_version = "0.2.0"

required_apps = ["frappe"]

after_install = "space.install.after_install"
after_migrate = "space.install.after_migrate"

add_to_apps_screen = [
	{
		"name": "space",
		"logo": "/assets/space/images/space.svg",
		"title": "Space",
		"route": "/app/cloud-manager",
	}
]

scheduler_events = {
	"hourly": [
		"space.jobs.monitoring.refresh_all_health",
	],
	"daily": [
		"space.jobs.ssl.check_ssl_all",
		"space.jobs.billing.process_subscription_expiry",
		"space.jobs.backup.cleanup_old_backups",
		"space.jobs.backup.enqueue_scheduled_backups",
		"space.jobs.monitoring.refresh_all_health",
	],
	"weekly": [
		"space.jobs.billing.record_weekly_usage",
	],
	"monthly": [
		"space.jobs.billing.generate_monthly_invoices",
	],
}
