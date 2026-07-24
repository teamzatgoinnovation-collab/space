"""Frappe hooks for Space — generic control-plane framework."""

app_name = "space"
app_title = "Space"
app_publisher = "ZatGo Innovation"
app_description = "General-purpose control-plane framework (providers, resources, jobs, plans)"
app_email = "engineering@zatgo.local"
app_license = "mit"
app_version = "0.5.0"

required_apps = ["frappe"]

after_install = "space.install.after_install"
after_migrate = "space.install.after_migrate"

add_to_apps_screen = [
	{
		"name": "space",
		"logo": "/assets/space/images/space.svg",
		"title": "Space",
		"route": "/app/space-cloud",
	}
]

# Framework hook contract (apps fill these via their own hooks.py)
# space_provider_types / space_resource_types / space_job_handlers
# space_dashboard_cards / space_api_namespaces

scheduler_events = {
	"daily": [
		"space.jobs.billing.process_subscription_expiry",
		"space.services.licenses.expire_licenses",
		"space.services.automation.run_daily_automation",
		"space.services.analytics.capture_daily_analytics",
	],
	"weekly": [
		"space.jobs.billing.record_weekly_usage",
	],
	"monthly": [
		"space.jobs.billing.generate_monthly_invoices",
	],
}
