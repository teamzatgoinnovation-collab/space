# Subscription lifecycle (Space)

`Space Subscription.lifecycle_status` is the authoritative state of a customer's ERP site:
TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED. Only
`space/services/lifecycle.py::transition` writes it (allowed moves in `ALLOWED`), each move leaves a
`Space Lifecycle Event`. Dates are UTC.

- **Trial**: 30 days (Space Settings > Free Trial Days) from the moment provisioning succeeds
  (`space_cloud/jobs/provision.py` -> `start_trial`). A plan that costs 0 is ACTIVE and never restricted.
- **Payment**: the portal verifies the provider, then calls `space.api.lifecycle.record_payment` (service
  credential). One month (30 days) per verified payment, stacking on what is left; idempotent on provider + payment
  id through the unique `dedupe_key` of the event table. Automatic recurring charges are not implemented.
- **Read-only**: Space Settings > "Enforce read-only mode on customer sites" (off by default). The platform pushes
  the state to each tenant (`space_cloud/services/tenant_sync.py`); enforcement is zatgo_core's
  `services/subscription_gate.py`. The hourly job re-pushes nothing unless a state changes; admin actions push at once.
- **Inactivity** (`space/jobs/lifecycle.py`, hourly; activity read daily): 10 days with no human activity -> one
  warning email (site, URL, last activity, deletion date, backup advice) and the site is scheduled for deletion in 5
  days. At the due time the job re-reads activity and re-checks payment, hold, exclusion, protected domains and
  ownership; any of them cancels. Otherwise it deletes -- but only when Space Settings > "Delete inactive sites
  automatically" is on; off, it only records a `deletion_dry_run` event.
- **Never deleted**: protected domains (the portal's own site + Space Settings > Protected Domains), sites with
  `deletion_hold`, sites in Space Deletion Exclusion, paid / ACTIVE subscriptions, and any site whose activity could
  not be read in the last 3 days ("unknown" is never "inactive"). No warning sent (`Sent`) means no deletion.
- **Deletion** (`space_cloud/jobs/lifecycle.py::run_delete_site`): guards -> `bench drop-site` -> check the site is
  gone -> purge `archived/sites/<site>*` (where `bench drop-site` moves the folder) -> remove Space Domain, mark Space Backup rows Deleted -> status
  Deleted + subscription DELETED. Each stage is an event; a failed run stays DELETION_SCHEDULED and can be retried.
- **Emails** are stored on the event row (dedupe key per subscription/kind/cycle), sent through the site's outgoing
  Email Account (Gmail SMTP), retried hourly up to 5 times; they require Space Settings > Email notifications.
- **Admin**: `space.api.lifecycle.*` (extend trial, activate, suspend, reactivate, cancel deletion, hold,
  exclusions, schedule deletion with typed confirmation). All need Space Admin / System Manager and are audited.

Tests: `space/tests/test_lifecycle.py`.
