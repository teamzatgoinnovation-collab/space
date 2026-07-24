"""Frappe hooks for Space control plane."""

app_name = "space"
app_title = "Space"
app_publisher = "ZatGo Innovation"
app_description = "Frappe Cloud-like control plane for ERPNext sites"
app_email = "engineering@zatgo.local"
app_license = "mit"
app_version = "0.1.0"

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
}
