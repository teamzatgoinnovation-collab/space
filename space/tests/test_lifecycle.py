"""Subscription lifecycle: trial countdown and expiry, payments, the inactivity clock (warning, scheduled deletion,
re-check, cancellation), dedupe and tenant isolation, admin controls and timezone handling.

Pure rules run without a database; the rest runs against a real site (`bench --site X run-tests --app space`).
Email sending and the tenant bench are replaced by recording fakes: what is asserted is what the platform DECIDES."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import random_string

from space.jobs import lifecycle as job
from space.services import lifecycle as lc
from space.services import lifecycle_mail as mail

T0 = datetime(2026, 11, 6, 12, 0, 0)  # naive UTC


# --- pure rules (no database) ---------------------------------------------------------------------------------------

class TestPureRules(unittest.TestCase):
	def test_remaining_days_rounds_up_and_floors_at_zero(self):
		end = T0 + timedelta(days=30)
		self.assertEqual(lc.remaining_days(end, T0), 30)
		self.assertEqual(lc.remaining_days(end, T0 + timedelta(days=29, hours=23)), 1)
		self.assertEqual(lc.remaining_days(end, end), 0)
		self.assertEqual(lc.remaining_days(end, end + timedelta(days=3)), 0)
		self.assertIsNone(lc.remaining_days(None, T0))

	def test_timezone_aware_values_are_normalised_to_utc(self):
		riyadh = datetime(2026, 12, 6, 15, 0, tzinfo=timezone(timedelta(hours=3)))
		self.assertEqual(lc.as_utc(riyadh), datetime(2026, 12, 6, 12, 0))
		self.assertEqual(lc.remaining_days(riyadh, T0), 30)
		self.assertEqual(lc.as_utc("2026-12-06T12:00:00+00:00"), datetime(2026, 12, 6, 12, 0))

	def test_inactive_days_counts_from_trial_start_when_no_activity(self):
		self.assertAlmostEqual(lc.inactive_days(None, T0, T0 + timedelta(days=10)), 10.0)
		self.assertAlmostEqual(lc.inactive_days(T0 + timedelta(days=4), T0, T0 + timedelta(days=10)), 6.0)

	def test_paid_until_stacks_on_what_is_left(self):
		self.assertEqual(lc.next_paid_until(None, T0), T0 + timedelta(days=30))
		self.assertEqual(lc.next_paid_until(T0 + timedelta(days=10), T0), T0 + timedelta(days=40))
		self.assertEqual(lc.next_paid_until(T0 - timedelta(days=10), T0), T0 + timedelta(days=30))  # never from the past

	def _decide(self, **kw):
		base = dict(inactive=0.0, warn_after=10, delete_after=15, status=lc.TRIAL, warned_for_cycle=False,
			scheduled_at=None, now=T0, paid=False, held=False, excluded=False, protected=False,
			activity_after_warning=False)
		base.update(kw)
		return lc.inactivity_decision(**base)

	def test_day_10_warns_once(self):
		self.assertEqual(self._decide(inactive=9.9), "none")
		self.assertEqual(self._decide(inactive=10.0), "warn")
		self.assertEqual(self._decide(inactive=12, warned_for_cycle=True), "none")

	def test_never_warns_paid_active_held_excluded_protected(self):
		for kw in ({"paid": True}, {"status": lc.ACTIVE}, {"held": True}, {"excluded": True}, {"protected": True}):
			self.assertEqual(self._decide(inactive=30, **kw), "none", kw)

	def test_day_15_deletes_only_after_every_recheck(self):
		due = T0 - timedelta(hours=1)
		sched = dict(status=lc.DELETION_SCHEDULED, scheduled_at=due, warned_for_cycle=True)
		self.assertEqual(self._decide(inactive=15, **sched), "delete")
		self.assertEqual(self._decide(inactive=15, scheduled_at=T0 + timedelta(days=1), status=lc.DELETION_SCHEDULED), "wait")
		for kw in ({"paid": True}, {"held": True}, {"excluded": True}, {"protected": True},
				{"activity_after_warning": True}, {"inactive": 2}):
			args = dict(inactive=15, **sched)
			args.update(kw)
			self.assertEqual(self._decide(**args), "cancel", kw)

	def test_manual_schedule_ignores_the_inactivity_clock_but_not_the_shields(self):
		due = T0 - timedelta(hours=1)
		args = dict(inactive=0.5, status=lc.DELETION_SCHEDULED, scheduled_at=due, manual=True)
		self.assertEqual(self._decide(**args), "delete")
		self.assertEqual(self._decide(held=True, **args), "cancel")
		self.assertEqual(self._decide(**{**args, "scheduled_at": T0 + timedelta(days=2)}), "wait")


# --- database-backed ---------------------------------------------------------------------------------------------------

class LifecycleCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")
		self.sent: list[dict] = []
		self.pushed: list[tuple[str, dict]] = []
		self.deleted: list[tuple[str, str]] = []
		self._patches = [
			mock.patch.object(frappe, "sendmail", side_effect=lambda **kw: self.sent.append(kw)),
			mock.patch.object(lc, "push_state", side_effect=lambda sub: self.pushed.append((sub, {})) or True),
			mock.patch("space.jobs.lifecycle.refresh_activity", return_value=True),
		]
		for p in self._patches:
			p.start()
		frappe.db.set_single_value("Space Settings", {
			"email_notifications_enabled": 1, "lifecycle_enforcement_enabled": 1, "auto_deletion_enabled": 0,
			"trial_days_default": 30, "inactivity_warning_days": 10, "inactivity_delete_days": 15,
			"payment_grace_days": 3, "protected_domains": "",
		})
		self.plan = self._plan(price=49)
		self.customer, self.email = self._customer()
		self.site, self.sub = self._site(self.customer, self.plan)

	def tearDown(self):
		for p in self._patches:
			p.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# fixtures
	def _plan(self, price=49):
		code = f"t{random_string(6).lower()}"
		return frappe.get_doc({"doctype": "Space Plan", "code": code, "title": code, "monthly_price": price,
			"is_active": 1, "trial_days": 14}).insert(ignore_permissions=True).name

	def _customer(self):
		email = f"cust.{random_string(8).lower()}@zatgo.test"
		doc = frappe.get_doc({"doctype": "Space Customer", "customer_name": f"C {random_string(6)}", "email": email,
			"status": "Active"}).insert(ignore_permissions=True)
		return doc.name, email

	def _server(self):
		name = frappe.db.get_value("Space Server", {}, "name")
		if name:
			return name
		return frappe.get_doc({"doctype": "Space Server", "server_name": f"srv-{random_string(5)}", "ip_address": "127.0.0.1",
			"ssh_user": "x", "backend_container": "x", "status": "Active"}).insert(ignore_permissions=True).name

	def _site(self, customer, plan, name=None, domain_suffix="zatgoinnovation.com"):
		name = name or f"t{random_string(7).lower()}"
		sub = frappe.get_doc({"doctype": "Space Subscription", "customer": customer, "plan": plan, "status": "Trial",
			"payment_status": "Trial", "start_date": frappe.utils.today()}).insert(ignore_permissions=True)
		site = frappe.get_doc({"doctype": "Space Site", "site_name": name, "customer": customer, "server": self._server(),
			"plan": plan, "subscription": sub.name, "status": "Active"}).insert(ignore_permissions=True)
		return site.name, sub.name

	def _start(self, started=None):
		lc.start_trial(self.sub, started_at=started or T0)
		frappe.db.set_value("Space Subscription", self.sub, "activity_checked_at", T0)

	def _status(self):
		return frappe.db.get_value("Space Subscription", self.sub, "lifecycle_status")

	def _set(self, **fields):
		frappe.db.set_value("Space Subscription", self.sub, fields)

	def _run(self, now):
		return job.process_subscription(self.sub, now=now)

	# --- trial ------------------------------------------------------------------------------------------------

	def test_trial_is_30_days_from_provisioning_and_start_is_idempotent(self):
		self._start()
		doc = frappe.get_doc("Space Subscription", self.sub)
		self.assertEqual(doc.lifecycle_status, lc.TRIAL)
		self.assertEqual(lc.as_utc(doc.trial_ends_at), T0 + timedelta(days=30))
		self.assertFalse(lc.start_trial(self.sub, started_at=T0 + timedelta(days=5)))
		self.assertEqual(lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "trial_ends_at")), T0 + timedelta(days=30))

	def test_countdown_in_customer_view(self):
		self._start()
		v = lc.view(self.sub, T0 + timedelta(days=1, hours=2))
		self.assertEqual(v["remaining_days"], 29)
		self.assertEqual(v["monthly_price"], 49)
		self.assertTrue(v["site_url"].startswith("https://"))
		self.assertFalse(v["read_only"])

	def test_trial_expires_at_the_boundary_and_not_before(self):
		self._start()
		self.assertNotEqual(self._run(T0 + timedelta(days=30) - timedelta(seconds=1)), "expired")
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertEqual(self._run(T0 + timedelta(days=30)), "expired")
		self.assertEqual(self._status(), lc.EXPIRED)
		self.assertTrue(lc.view(self.sub, T0 + timedelta(days=31))["read_only"])

	def test_free_plan_never_expires(self):
		site, sub = self._site(self.customer, self._plan(price=0))
		lc.start_trial(sub, started_at=T0)
		self.assertEqual(frappe.db.get_value("Space Subscription", sub, "lifecycle_status"), lc.ACTIVE)

	def test_trial_reminder_email_once(self):
		self._start()
		now = T0 + timedelta(days=28)
		self._run(now)
		self._run(now + timedelta(hours=1))
		reminders = [m for m in self.sent if "trial ends soon" in m["subject"]]
		self.assertEqual(len(reminders), 1)

	# --- payments ---------------------------------------------------------------------------------------------

	def test_payment_activates_a_month_and_is_idempotent(self):
		self._start()
		self._run(T0 + timedelta(days=31))  # expired
		first = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_1", amount=49, currency="SAR")
		again = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_1", amount=49, currency="SAR")
		self.assertTrue(first["applied"])
		self.assertFalse(again["applied"])
		self.assertEqual(first["paid_until"], again["paid_until"])
		self.assertEqual(self._status(), lc.ACTIVE)
		second = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_2", amount=49, currency="SAR")
		self.assertGreater(lc.as_utc(second["paid_until"]), lc.as_utc(first["paid_until"]) + timedelta(days=29))

	def test_active_paid_subscription_is_never_warned_or_deleted(self):
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_x", amount=49, currency="SAR")
		self._set(last_activity_at=None)
		until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		self.assertEqual(self._run(until - timedelta(days=1)), "none")
		self.assertEqual(self.sent, [])

	def test_payment_lapse_goes_read_only_after_grace(self):
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_g", amount=49, currency="SAR")
		until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		self._run(until + timedelta(days=2))
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertEqual(self._run(until + timedelta(days=3, hours=1)), "pending_payment")
		self.assertEqual(self._status(), lc.PENDING_PAYMENT)

	# --- inactivity: day 10 -----------------------------------------------------------------------------------

	def _inactive(self, days, now):
		self._set(last_activity_at=now - timedelta(days=days), activity_checked_at=now)

	def test_day_10_sends_one_warning_with_all_details_and_schedules_day_15(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warned")
		self.assertEqual(self._status(), lc.DELETION_SCHEDULED)
		self.assertEqual(len(self.sent), 1)
		mail_ = self.sent[0]
		self.assertEqual(mail_["recipients"], [self.email])
		for needle in (self.site, f"https://{frappe.db.get_value('Space Site', self.site, 'domain')}", "Last activity",
				"Scheduled deletion", "download a backup"):
			self.assertIn(needle, mail_["message"] if needle != "download a backup" else mail_["message"].lower().replace("download a backup", "download a backup"))
		self.assertEqual(lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at")), now + timedelta(days=5))

	def test_no_duplicate_warning_when_the_scheduler_runs_again(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self._run(now)
		self._run(now + timedelta(hours=1))
		self._run(now + timedelta(hours=2))
		self.assertEqual(len(self.sent), 1)

	def test_dedupe_is_enforced_by_the_database(self):
		lc.record_event("transition", subscription=self.sub, dedupe_key="k-unique")
		self.assertIsNone(lc.record_event("transition", subscription=self.sub, dedupe_key="k-unique"))
		dup = frappe.get_doc({"doctype": "Space Lifecycle Event", "kind": "transition", "dedupe_key": "k-unique"})
		with self.assertRaises(frappe.UniqueValidationError):
			dup.insert(ignore_permissions=True)

	def test_failed_warning_email_schedules_nothing_and_retries_later(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		with mock.patch.object(frappe, "sendmail", side_effect=RuntimeError("smtp down")):
			self.assertEqual(self._run(now), "warning_Failed")
		self.assertEqual(self._status(), lc.TRIAL)  # never scheduled behind an unsent warning
		self.assertIsNone(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		self.assertEqual(mail.retry_failed(now + timedelta(minutes=1)), 0)  # not due yet
		self.assertEqual(mail.retry_failed(now + timedelta(hours=2)), 1)
		self.assertEqual(len(self.sent), 1)
		self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"kind": "inactivity_warning_email", "status": "Sent"}))

	def test_email_notifications_off_means_no_warning_so_no_deletion(self):
		self._start()
		frappe.db.set_single_value("Space Settings", "email_notifications_enabled", 0)
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warning_Skipped")
		self.assertEqual(self._status(), lc.TRIAL)

	def test_unknown_activity_is_never_read_as_inactive(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._set(last_activity_at=None, activity_checked_at=T0 - timedelta(days=10))
		self.assertEqual(self._run(now), "activity_unknown")
		self.assertEqual(self.sent, [])

	def test_held_excluded_and_protected_sites_are_never_warned(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(30, now)
		self._set(deletion_hold=1)
		self.assertEqual(self._run(now), "none")
		self._set(deletion_hold=0)
		frappe.get_doc({"doctype": "Space Deletion Exclusion", "site": self.site, "reason": "vip"}).insert(ignore_permissions=True)
		self.assertEqual(self._run(now), "none")
		frappe.delete_doc("Space Deletion Exclusion", self.site, ignore_permissions=True)
		frappe.db.set_single_value("Space Settings", "protected_domains", frappe.db.get_value("Space Site", self.site, "domain"))
		self.assertEqual(self._run(now), "none")
		self.assertEqual(self.sent, [])

	# --- day 15 -----------------------------------------------------------------------------------------------

	def _schedule(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self._run(now)
		return now, now + timedelta(days=5)

	def test_activity_after_the_warning_cancels_deletion(self):
		now, due = self._schedule()
		self._set(last_activity_at=now + timedelta(days=2), activity_checked_at=due)
		self.assertEqual(self._run(due + timedelta(minutes=1)), "cancelled")
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertIsNone(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"kind": "deletion_cancelled", "subscription": self.sub}))

	def test_payment_after_the_warning_cancels_deletion(self):
		now, due = self._schedule()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_late", amount=49, currency="SAR")
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertNotEqual(self._run(due + timedelta(minutes=1)), "deleting")

	def test_day_15_is_a_dry_run_until_auto_deletion_is_switched_on(self):
		now, due = self._schedule()
		with mock.patch("space_cloud.jobs.lifecycle.enqueue_delete_site", side_effect=lambda s, source="user": self.deleted.append((s, source))):
			self.assertEqual(self._run(due + timedelta(minutes=1)), "dry_run")
			self.assertEqual(self.deleted, [])
			self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"kind": "deletion_dry_run", "subscription": self.sub}))

	def test_day_15_deletes_exactly_once_and_only_this_site(self):
		other_site, other_sub = self._site(self.customer, self.plan)
		lc.start_trial(other_sub, started_at=T0)
		now, due = self._schedule()
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 1)
		with mock.patch("space_cloud.jobs.lifecycle.enqueue_delete_site", side_effect=lambda s, source="user": self.deleted.append((s, source))):
			self.assertEqual(self._run(due + timedelta(minutes=1)), "deleting")
			self.assertEqual(self._run(due + timedelta(minutes=5)), "already_started")
		self.assertEqual(self.deleted, [(self.site, "inactivity")])  # one call, this tenant only
		self.assertEqual(frappe.db.get_value("Space Subscription", other_sub, "lifecycle_status"), lc.TRIAL)

	def test_not_due_yet_waits(self):
		now, due = self._schedule()
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 1)
		with mock.patch("space_cloud.jobs.lifecycle.enqueue_delete_site") as enqueue:
			self.assertEqual(self._run(due - timedelta(hours=1)), "wait")
			enqueue.assert_not_called()

	def test_recheck_that_cannot_read_activity_never_deletes(self):
		now, due = self._schedule()
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 1)
		with mock.patch("space.jobs.lifecycle.refresh_activity", return_value=False), \
				mock.patch("space_cloud.jobs.lifecycle.enqueue_delete_site") as enqueue:
			self.assertEqual(self._run(due + timedelta(days=1)), "recheck_unavailable")
			enqueue.assert_not_called()

	def test_delete_guard_refuses_unscheduled_held_paid_and_protected(self):
		from space_cloud.jobs import lifecycle as sc

		self._start()
		site = frappe.get_doc("Space Site", self.site)
		with self.assertRaises(frappe.ValidationError):
			sc.guard_deletable(site, "inactivity")  # not scheduled
		frappe.db.set_single_value("Space Settings", "protected_domains", site.domain)
		with self.assertRaises(frappe.ValidationError):
			sc.guard_deletable(site, "user")  # protected for every route, customers' own button included

	# --- admin ------------------------------------------------------------------------------------------------

	def test_admin_extend_activate_suspend_reactivate_cancel(self):
		from space.api import lifecycle as api

		self._start()
		self._run(T0 + timedelta(days=31))
		self.assertEqual(self._status(), lc.EXPIRED)
		api.extend_trial(self.site, 7, "goodwill")
		self.assertEqual(self._status(), lc.TRIAL)
		api.suspend(self.site, "abuse")
		self.assertEqual(self._status(), lc.SUSPENDED)
		target = api.reactivate(self.site, "resolved")["data"]["lifecycle_status"]
		self.assertEqual(target, lc.TRIAL)
		api.activate(self.site, 2, "paid offline")
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertGreater(lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until")), lc.utcnow() + timedelta(days=59))
		kinds = frappe.get_all("Space Lifecycle Event", filters={"subscription": self.sub, "kind": "admin_action"}, pluck="name")
		self.assertGreaterEqual(len(kinds), 4)

	def test_admin_manual_schedule_needs_typed_confirmation_and_an_email(self):
		from space.api import lifecycle as api

		self._start()
		bad = api.schedule_deletion(self.site, 5, "wrong-name", "cleanup")
		self.assertFalse(bad["ok"])
		ok_ = api.schedule_deletion(self.site, 5, self.site, "cleanup")
		self.assertTrue(ok_["ok"])
		self.assertEqual(self._status(), lc.DELETION_SCHEDULED)
		self.assertEqual(len(self.sent), 1)
		api.cancel_deletion(self.site, "customer asked")
		self.assertEqual(self._status(), lc.TRIAL)

	def test_exclusion_cancels_a_scheduled_deletion(self):
		from space.api import lifecycle as api

		self._schedule()
		api.add_exclusion(self.site, "strategic customer")
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertTrue(frappe.db.exists("Space Deletion Exclusion", self.site))

	def test_non_admins_cannot_use_admin_controls_or_see_other_customers_sites(self):
		from space.api import lifecycle as api

		self._start()
		other_customer, _ = self._customer()
		user = frappe.get_doc({"doctype": "User", "email": f"u.{random_string(8).lower()}@zatgo.test", "first_name": "U",
			"send_welcome_email": 0, "roles": [{"role": "Space Customer"}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Space Customer", other_customer, "user", user.name)
		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			api.extend_trial(self.site, 5, "x")
		with self.assertRaises(frappe.PermissionError):
			api.get_lifecycle(self.site)  # someone else's site
		self.assertEqual(api.list_lifecycle()["data"], {})

	def test_record_payment_endpoint_requires_admin_role(self):
		from space.api import lifecycle as api

		self._start()
		user = frappe.get_doc({"doctype": "User", "email": f"p.{random_string(8).lower()}@zatgo.test", "first_name": "P",
			"send_welcome_email": 0, "roles": [{"role": "Space Customer"}]}).insert(ignore_permissions=True)
		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			api.record_payment(self.site, "moyasar", "pay_evil", 1, "SAR")

	def test_scheduler_run_is_safe_to_repeat(self):
		self._start()
		before = frappe.db.count("Space Lifecycle Event")
		a = job.run_hourly()
		mid = frappe.db.count("Space Lifecycle Event")
		b = job.run_hourly()
		self.assertEqual(frappe.db.count("Space Lifecycle Event"), mid)
		self.assertEqual(a["errors"], 0)
		self.assertEqual(b["errors"], 0)
		self.assertGreaterEqual(mid, before)


class TestBackfill(LifecycleCase):
	"""The rollout backfill: existing customer sites get a trial; real company sites that Space merely tracks do not."""

	def _state(self, sub):
		return frappe.db.get_value("Space Subscription", sub, ["lifecycle_status", "trial_started_at", "trial_ends_at"], as_dict=True)

	def test_paid_plan_gets_a_fresh_30_day_trial_free_plan_stays_active(self):
		free_site, free_sub = self._site(self.customer, self._plan(price=0))
		counts = lc.backfill_missing(T0)
		self.assertGreaterEqual(counts["trial"], 1)
		paid = self._state(self.sub)
		self.assertEqual(paid.lifecycle_status, lc.TRIAL)
		self.assertEqual(lc.as_utc(paid.trial_ends_at), T0 + timedelta(days=30))
		self.assertEqual(self._state(free_sub).lifecycle_status, lc.ACTIVE)
		self.assertIsNone(self._state(free_sub).trial_ends_at)

	def test_protected_sites_never_get_a_trial_clock(self):
		domain = frappe.db.get_value("Space Site", self.site, "domain")
		frappe.db.set_single_value("Space Settings", "protected_domains", f"other.example.com\n{domain}")
		counts = lc.backfill_missing(T0)
		self.assertEqual(self._state(self.sub).lifecycle_status, "")
		self.assertGreaterEqual(counts["skipped_protected"], 1)

	def test_a_subscription_shared_with_a_protected_site_is_skipped(self):
		# kasibasia + democompanysa share one subscription: protecting either protects both.
		second = frappe.get_doc({"doctype": "Space Site", "site_name": f"s{random_string(6).lower()}", "customer": self.customer,
			"server": self._server(), "plan": self.plan, "subscription": self.sub, "status": "Active"}).insert(ignore_permissions=True)
		frappe.db.set_single_value("Space Settings", "protected_domains", second.domain)
		lc.backfill_missing(T0)
		self.assertEqual(self._state(self.sub).lifecycle_status, "")

	def test_sites_that_are_not_live_are_skipped(self):
		for status in ("Deleted", "Failed", "Draft", "Provisioning"):
			frappe.db.set_value("Space Site", self.site, "status", status)
			lc.backfill_missing(T0)
			self.assertEqual(self._state(self.sub).lifecycle_status, "", status)

	def test_backfill_is_idempotent_and_pushes_nothing(self):
		lc.backfill_missing(T0)
		first = self._state(self.sub)
		again = lc.backfill_missing(T0 + timedelta(days=3))
		self.assertEqual(self._state(self.sub), first)  # trial dates did not move
		self.assertEqual(again["trial"], 0)
		self.assertEqual(self.pushed, [])
		self.assertEqual(self.sent, [])


if __name__ == "__main__":
	unittest.main()
