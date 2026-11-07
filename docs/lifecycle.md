# Subscription lifecycle (Space)

`Space Subscription.lifecycle_status` is the authoritative state of a customer's ERP site:
TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED. Only
`space/services/lifecycle.py::transition` writes it (allowed moves in `ALLOWED`; DELETED is terminal and reachable
from every state). Each move leaves a `Space Lifecycle Event`. All dates are UTC.

## States and money

- **Trial**: 30 days (Space Settings > Free Trial Days) from the moment provisioning succeeds
  (`space_cloud/jobs/provision.py` -> `start_trial`). A plan that costs 0 is ACTIVE and never restricted (open product
  decision: it is also never deleted for inactivity). Moving a free site to a paid plan (`Space Subscription.change_plan`)
  starts a fresh trial on the paid plan; paid -> free ends the clock.
- **Payment**: the portal verifies the provider itself, then calls `space.api.lifecycle.record_payment` (service
  credential). One month (30 days) per verified payment, stacking on what is left; idempotent on provider + payment id
  through the unique `dedupe_key` of the event table. A deleted site refuses the payment before anything is recorded.
  A SUSPENDED site (only an admin suspends) keeps its suspension: the months are credited, the site stays suspended
  until an admin reactivates it. Automatic recurring charges are not implemented.
- **Grace**: Space Settings > Payment Grace Days (default 3, 0 allowed). The platform moves ACTIVE -> PENDING_PAYMENT
  after `paid_until + grace`; the same number travels to the tenant (`grace_days`) so both sides agree.

## Read-only mode

Space Settings > "Enforce read-only mode on customer sites" (off by default). The platform pushes the state to each
tenant (`space_cloud/services/tenant_sync.py`); enforcement is zatgo_core's `services/subscription_gate.py`.
`lifecycle.push_state` runs on every transition and stores a hash of what was sent; the hourly `reconcile_pushes`
re-sends whatever differs from the desired state (a failed push, a payment whose push failed, the enforcement switch
flipped in either direction, the grace changed) and, as a heartbeat, once a day.

## Inactivity (`space/jobs/lifecycle.py`, hourly on the long queue; activity read daily)

The sequence, each step only after the previous one is **confirmed**:

1. 10 days without human activity -> ONE warning email is queued. (With "Delete inactive sites automatically" OFF, only
   a `deletion_dry_run` "would warn" event is logged: nobody is contacted and nothing is parked.)
2. The Email Queue row for that mail says **Sent** (`lifecycle_mail.confirm_delivery`) -> the deletion is scheduled for
   5 days later and the subscription becomes DELETION_SCHEDULED.
3. At the due time everything is re-checked: activity (re-read from the tenant), payment, hold, exclusion, protected
   domain, same customer who was warned, warning delivered. Any failure cancels (or, for an undelivered warning, waits).
4. Only then -- and only with auto-deletion ticked -- the delete job is queued with its source and the scheduled date.
   The job repeats the same re-check the instant it runs (a payment or login can arrive while it waits in the queue).

An admin-scheduled deletion (`space.api.lifecycle.schedule_deletion`, typed site name, customer emailed first) skips the
activity condition and the auto switch, but not the shields, the delivered notice or the same-customer check; a payment
made after it was scheduled cancels it.

**Never deleted**: protected domains (the portal's own site + Space Settings > Protected Domains -- list every real
company site Space tracks, e.g. kasibasia, democompanysa), sites with `deletion_hold`, sites in Space Deletion
Exclusion, paid / ACTIVE subscriptions, and any site whose activity could not be read in the last 3 days ("unknown" is
never "inactive"). No warning confirmed delivered means no deletion.

An inactivity *episode* is keyed by the last activity seen plus the number of cancelled deletions and payments, so a
customer who was warned, cancelled and went quiet again is warned again.

## Emails (`space/services/lifecycle_mail.py`)

Stored on the event row with a unique dedupe key per (subscription, kind, episode). Status: Pending (queued) -> Sent
(confirmed by the Email Queue) / Failed (retried with backoff, 5 tries, then once more after 24 h) / Skipped (Space
Settings > Email notifications off; re-sent when it is turned on). They go through the site's outgoing Email Account
(Gmail SMTP) and need Space Settings > Email notifications. The trial-ending reminder is only sent while enforcement is on.

## Deletion (`space_cloud/jobs/lifecycle.py::run_delete_site`)

Guards -> `bench drop-site` -> verify the site is gone -> purge `archived/sites/<site>*` (where `bench drop-site`
moves the folder) -> remove Space Domain, mark Space Backup rows Deleted -> site Deleted and the subscription DELETED
(unless another live site shares it). Each stage is an event; a failed run stays DELETION_SCHEDULED and can be retried
from the job. A refused re-check is not an error: the job is marked Failed with "NOT DELETED: <reason>".

## Rollout notes

- `patches.txt` has `[pre_model_sync]` / `[post_model_sync]` sections. The backfill is a **post-sync** patch: a legacy
  (section-less) patches.txt runs everything *before* the schema sync, so a patch needing the new columns would find
  nothing and still be recorded as done.
- The backfill skips sites in Protected Domains, sites that are not live, and subscriptions shared with a protected
  site; a paid plan gets a fresh 30-day trial, a free plan ACTIVE, a legacy paid subscription ACTIVE until its end date.
- New zatgo_core hooks are cached per site until `bench --site X clear-cache`: run it on a tenant before relying on
  enforcement there.
- Seeds on a production bench: set Protected Domains **before** the first migrate/backfill.

## Switching it on (in this order, one at a time)

1. **Protected Domains** lists every real company site Space tracks (the portal's own site is always protected).
2. A working **outgoing Email Account** (Gmail SMTP + app password) and Space Settings > Email notifications ticked;
   send yourself a test through a throwaway site. No confirmed warning means no deletion, so a broken mail setup is safe
   but silent: check `Space Lifecycle Event` rows of kind `inactivity_warning_email` for Failed/Skipped.
3. zatgo_core with the gate deployed on the tenant, and `bench --site <tenant> clear-cache` (hooks are cached per site).
4. **Enforce read-only mode** ticked: within the hour `reconcile_pushes` tells every managed tenant. Verify on a test
   tenant: `bench --site <tenant> execute zatgo_core.api.v1.subscription.get_status`.
5. Leave **Delete inactive sites automatically** OFF for a few days and read the `deletion_dry_run` events: they list
   what would have been warned. Only then tick it.

To undo: untick the switch; the next hourly reconcile releases every tenant (`enforcement_on` 0).

## Admin

`space.api.lifecycle.*` (extend trial, activate, suspend, reactivate, cancel deletion, hold, exclusions, schedule
deletion with typed confirmation). All need Space Admin / System Manager, require a reason, and are audited.

Tests: `space/tests/test_lifecycle.py`, `space_cloud/tests/test_tenant_sync.py`.
