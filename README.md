# Space

General-purpose control-plane framework for ZatGo (Frappe-style core).

- Generic DocTypes: Provider, Resource, Job, Plan, Subscription, Customer, Activity Log, Settings
- Hooks contract for vertical apps (`space_provider_types`, `space_resource_types`, `space_job_handlers`, …)
- Install with **space_cloud** on `space.zatgo.online` for hosting

```bash
bench get-app <space-git> --branch main
bench --site space.zatgo.online install-app space
# then install space_cloud
```

Portal APIs remain at `space.api.v1.*` (compatibility shims → `space_cloud`).
