"""Subscription lifecycle: trial countdown and expiry, payments, the inactivity clock (warning -> confirmed delivery ->
scheduled deletion -> re-check -> delete), cancellation, dedupe, tenant isolation, admin controls, timezone handling and
the rollout backfill. One regression test per defect found in review.

Pure rules run without a database; the rest runs against a real site (`bench --site X run-tests --app space`).
Email sending and the tenant bench are replaced by recording fakes: what is asserted is what the platform DECIDES.
The fake mailer inserts REAL Email Queue rows, so "Sent" has to be earned the way production earns it: by the queue
row saying so (`_confirm_mail`)."""

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

	def test_a_year_is_365_days_and_a_month_is_30(self):
		self.assertEqual(lc.next_paid_until(None, T0, months=12), T0 + timedelta(days=365))
		self.assertEqual(lc.next_paid_until(T0 + timedelta(days=10), T0, months=12), T0 + timedelta(days=375))
		self.assertEqual(lc.next_paid_until(None, T0, months=14), T0 + timedelta(days=365 + 60))

	def test_period_defaults_to_monthly_for_anything_unknown(self):
		self.assertEqual(lc.period_of({"billing_period": "Yearly"}), "Yearly")
		for v in (None, "", "Weekly", {}, {"billing_period": None}):
			self.assertEqual(lc.period_of(v), "Monthly", v)

	def test_state_hash_ignores_the_timestamp_but_not_the_state(self):
		a = {"enforcement_on": 1, "status": "TRIAL", "grace_days": 3, "synced_at": "t1"}
		self.assertEqual(lc.state_hash(a), lc.state_hash({**a, "synced_at": "t2"}))
		for change in ({"status": "EXPIRED"}, {"enforcement_on": 0}, {"grace_days": 5}, {"billing_period": "Yearly"}):
			self.assertNotEqual(lc.state_hash(a), lc.state_hash({**a, **change}), change)

	def _decide(self, **kw):
		base = dict(inactive=0.0, warn_after=10, delete_after=15, status=lc.TRIAL, warning="none", scheduled_at=None,
			now=T0, paid=False, held=False, excluded=False, protected=False, activity_after_warning=False)
		base.update(kw)
		return lc.inactivity_decision(**base)

	def test_day_10_warns_but_does_not_schedule(self):
		self.assertEqual(self._decide(inactive=9.9), "none")
		self.assertEqual(self._decide(inactive=10.0), "warn")

	def test_deletion_is_only_scheduled_behind_a_delivered_warning(self):
		self.assertEqual(self._decide(inactive=11, warning="sent"), "schedule")
		self.assertEqual(self._decide(inactive=11, warning="pending"), "wait_warning")  # queued: wait for the queue to confirm
		self.assertEqual(self._decide(inactive=11, warning="stalled"), "warn")  # failed / skipped: send_once re-arms or defers

	def test_with_auto_deletion_off_nobody_is_contacted(self):
		self.assertEqual(self._decide(inactive=11, auto_enabled=False), "dry_run_warn")
		self.assertEqual(self._decide(inactive=11, warning="sent", auto_enabled=False), "dry_run_warn")
		self.assertEqual(self._decide(inactive=5, auto_enabled=False), "none")

	def test_never_warns_paid_active_held_excluded_protected(self):
		for kw in ({"paid": True}, {"status": lc.ACTIVE}, {"held": True}, {"excluded": True}, {"protected": True}):
			self.assertEqual(self._decide(inactive=30, **kw), "none", kw)

	def test_day_15_deletes_only_after_every_recheck(self):
		due = T0 - timedelta(hours=1)
		sched = dict(status=lc.DELETION_SCHEDULED, scheduled_at=due)
		self.assertEqual(self._decide(inactive=15, **sched), "delete")
		self.assertEqual(self._decide(inactive=15, status=lc.DELETION_SCHEDULED, scheduled_at=T0 + timedelta(days=1)), "wait")
		for kw in ({"paid": True}, {"held": True}, {"excluded": True}, {"protected": True},
				{"activity_after_warning": True}, {"inactive": 2}):
			args = dict(inactive=15, **sched)
			args.update(kw)
			self.assertEqual(self._decide(**args), "cancel", kw)

	def test_manual_schedule_ignores_activity_and_prior_payment_but_not_the_shields(self):
		due = T0 - timedelta(hours=1)
		args = dict(inactive=0.5, status=lc.DELETION_SCHEDULED, scheduled_at=due, manual=True)
		self.assertEqual(self._decide(**args), "delete")
		self.assertEqual(self._decide(paid=True, **args), "delete")  # paid when scheduled: the admin meant it
		self.assertEqual(self._decide(held=True, **args), "cancel")
		self.assertEqual(self._decide(**{**args, "scheduled_at": T0 + timedelta(days=2)}), "wait")


# --- database-backed ---------------------------------------------------------------------------------------------------

class LifecycleBase(FrappeTestCase):
	"""Fixtures only (no tests). `self.sent` records every mail handed to Frappe; each is a real Email Queue row."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")
		self.sent: list[dict] = []
		self.queues: list[str] = []
		self.pushed: list[tuple[str, dict]] = []
		self.deleted: list[tuple] = []
		self.mail_mode = "queue"  # "queue" | "raise" | "drop"
		self._patches = [
			mock.patch.object(frappe, "sendmail", side_effect=self._fake_sendmail),
			mock.patch("space_cloud.services.tenant_sync.push_state", side_effect=lambda site, payload: self.pushed.append((site, payload)) or True),
			mock.patch("space.jobs.lifecycle.refresh_activity", return_value=True),
			mock.patch("space_cloud.jobs.lifecycle.enqueue_delete_site", side_effect=lambda s, source="user", expected_scheduled_at=None: self.deleted.append((s, source, expected_scheduled_at))),
		]
		for p in self._patches:
			p.start()
		frappe.db.set_single_value("Space Settings", {
			"email_notifications_enabled": 1, "lifecycle_enforcement_enabled": 1, "auto_deletion_enabled": 1,
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

	# fakes
	def _fake_sendmail(self, **kw):
		self.sent.append(kw)
		if self.mail_mode == "raise":
			raise RuntimeError("smtp down")
		if self.mail_mode == "drop":
			return []
		q = frappe.get_doc({
			"doctype": "Email Queue", "sender": "space@zatgo.test", "message": kw.get("message") or "m", "status": "Not Sent",
			"reference_doctype": kw.get("reference_doctype"), "reference_name": kw.get("reference_name"),
			"recipients": [{"recipient": r} for r in kw["recipients"]],
		}).insert(ignore_permissions=True)
		self.queues.append(q.name)
		return q

	def _confirm_mail(self, now, status="Sent"):
		"""What the Email Queue flush does in production: mark every queued mail, then the hourly job confirms."""
		for q in self.queues:
			frappe.db.set_value("Email Queue", q, "status", status)
		return mail.confirm_delivery(now)

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

	def _site(self, customer, plan, name=None):
		name = name or f"t{random_string(7).lower()}"
		sub = frappe.get_doc({"doctype": "Space Subscription", "customer": customer, "plan": plan, "status": "Trial",
			"payment_status": "Trial", "start_date": frappe.utils.today()}).insert(ignore_permissions=True)
		site = frappe.get_doc({"doctype": "Space Site", "site_name": name, "customer": customer, "server": self._server(),
			"plan": plan, "subscription": sub.name, "status": "Active"}).insert(ignore_permissions=True)
		return site.name, sub.name

	def _start(self, started=None):
		lc.start_trial(self.sub, started_at=started or T0)
		frappe.db.set_value("Space Subscription", self.sub, "activity_checked_at", T0)

	def _status(self, sub=None):
		return frappe.db.get_value("Space Subscription", sub or self.sub, "lifecycle_status")

	def _set(self, **fields):
		frappe.db.set_value("Space Subscription", self.sub, fields)

	def _run(self, now):
		return job.process_subscription(self.sub, now=now)

	def _inactive(self, days, now):
		self._set(last_activity_at=now - timedelta(days=days), activity_checked_at=now)

	def _warned_and_scheduled(self):
		"""Drive the real sequence: day 10 warning -> queue confirms delivery -> deletion scheduled. Returns (now, due)."""
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warning_Pending")
		self._confirm_mail(now + timedelta(hours=1))
		self.assertEqual(self._run(now + timedelta(hours=2)), "scheduled")
		due = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		return now + timedelta(hours=2), due


# --- trial and payments ------------------------------------------------------------------------------------------------

class TestTrialAndPayments(LifecycleBase):
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

	def test_trial_reminder_email_once_and_only_while_enforcement_is_on(self):
		self._start()
		now = T0 + timedelta(days=28)
		self._run(now)
		self._run(now + timedelta(hours=1))
		self.assertEqual(len([m for m in self.sent if "trial ends soon" in m["subject"]]), 1)
		self.sent.clear()
		frappe.db.set_single_value("Space Settings", "lifecycle_enforcement_enabled", 0)
		frappe.db.delete("Space Lifecycle Event", {"kind": "trial_ending_email"})
		self._run(now + timedelta(hours=2))
		self.assertEqual(self.sent, [])  # the mail says "becomes read-only": not while nothing is enforced

	def test_payment_activates_a_month_and_is_idempotent(self):
		self._start()
		self._run(T0 + timedelta(days=31))
		first = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_1", amount=49, currency="SAR")
		again = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_1", amount=49, currency="SAR")
		self.assertTrue(first["applied"])
		self.assertFalse(again["applied"])
		self.assertEqual(first["paid_until"], again["paid_until"])
		self.assertEqual(self._status(), lc.ACTIVE)
		second = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_2", amount=49, currency="SAR")
		self.assertGreater(lc.as_utc(second["paid_until"]), lc.as_utc(first["paid_until"]) + timedelta(days=29))

	def test_a_payment_for_a_deleted_site_is_refused_before_anything_is_recorded(self):
		self._start()
		lc.transition(self.sub, lc.DELETED, reason="test", push=False)
		with self.assertRaises(frappe.ValidationError):
			lc.record_payment(self.sub, provider="moyasar", payment_id="pay_dead", amount=49, currency="SAR")
		self.assertFalse(frappe.db.exists("Space Lifecycle Event", {"dedupe_key": "payment:moyasar:pay_dead"}))

	def test_a_payment_does_not_lift_an_admin_suspension(self):
		self._start()
		lc.admin_suspend(self.sub, reason="abuse")
		result = lc.record_payment(self.sub, provider="moyasar", payment_id="pay_s", amount=49, currency="SAR")
		self.assertTrue(result["applied"])
		self.assertTrue(result["suspended"])
		self.assertEqual(self._status(), lc.SUSPENDED)  # the months are credited, the suspension stays
		self.assertTrue(lc.is_paid(frappe.db.get_value("Space Subscription", self.sub, "paid_until")))

	def test_active_paid_subscription_is_never_warned_or_deleted(self):
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_x", amount=49, currency="SAR")
		self._set(last_activity_at=None)
		until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		self.assertEqual(self._run(until - timedelta(days=1)), "none")
		self.assertEqual(self.sent, [])

	def test_payment_lapse_goes_read_only_after_the_configured_grace(self):
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_g", amount=49, currency="SAR")
		until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		self._run(until + timedelta(days=2))
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertEqual(self._run(until + timedelta(days=3, hours=1)), "pending_payment")
		self.assertEqual(self._status(), lc.PENDING_PAYMENT)

	def test_a_grace_of_zero_is_honoured_and_travels_to_the_tenant(self):
		frappe.db.set_single_value("Space Settings", "payment_grace_days", 0)
		self.assertEqual(lc.settings()["payment_grace_days"], 0)
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_z", amount=49, currency="SAR")
		self.assertEqual(lc.state_payload(self.sub)["grace_days"], 0)
		until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		self.assertEqual(self._run(until + timedelta(hours=1)), "pending_payment")

	def test_settings_are_clamped_to_sane_ranges(self):
		frappe.db.set_single_value("Space Settings", {"trial_days_default": 0, "inactivity_warning_days": 20, "inactivity_delete_days": 5})
		s = lc.settings()
		self.assertEqual(s["trial_days_default"], 1)
		self.assertGreater(s["inactivity_delete_days"], s["inactivity_warning_days"])

	def test_plan_changes_follow_through_to_the_lifecycle(self):
		site, sub = self._site(self.customer, self._plan(price=0))
		lc.start_trial(sub, started_at=T0)
		self.assertEqual(self._status(sub), lc.ACTIVE)
		frappe.get_doc("Space Subscription", sub).change_plan(self._plan(price=99))
		self.assertEqual(self._status(sub), lc.TRIAL)  # free -> paid: a fresh trial, no longer a permanent shield
		self.assertGreater(lc.as_utc(frappe.db.get_value("Space Subscription", sub, "trial_ends_at")), lc.utcnow() + timedelta(days=29))
		frappe.get_doc("Space Subscription", sub).change_plan(self._plan(price=0))
		self.assertEqual(self._status(sub), lc.ACTIVE)  # paid -> free: nothing to pay

	def test_subscription_for_site_never_borrows_another_sites_subscription(self):
		site2 = frappe.get_doc({"doctype": "Space Site", "site_name": f"u{random_string(6).lower()}", "customer": self.customer,
			"server": self._server(), "plan": self.plan, "status": "Active"}).insert(ignore_permissions=True)
		self.assertIsNone(lc.subscription_for_site(site2.name))  # no link -> no subscription (not the customer's first)
		self.assertEqual(lc.subscription_for_site(self.site), self.sub)


# --- the inactivity clock ------------------------------------------------------------------------------------------------

class TestInactivity(LifecycleBase):
	def test_day_10_warns_once_with_all_details_and_schedules_only_after_delivery(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warning_Pending")
		self.assertEqual(self._status(), lc.TRIAL)  # nothing scheduled until the mail is confirmed delivered
		self.assertEqual(len(self.sent), 1)
		sent = self.sent[0]
		self.assertEqual(sent["recipients"], [self.email])
		domain = frappe.db.get_value("Space Site", self.site, "domain")
		for needle in (self.site, f"https://{domain}", "Last activity", "Scheduled deletion", "download a backup"):
			self.assertIn(needle.lower(), sent["message"].lower())
		self.assertEqual(self._run(now + timedelta(minutes=30)), "waiting_for_warning")
		self.assertEqual(self._confirm_mail(now + timedelta(hours=1))["sent"], 1)
		self.assertEqual(self._run(now + timedelta(hours=2)), "scheduled")
		self.assertEqual(self._status(), lc.DELETION_SCHEDULED)
		due = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		self.assertEqual(due, (now + timedelta(hours=2) + timedelta(days=5)).replace(microsecond=0))

	def test_no_duplicate_warning_when_the_scheduler_runs_again(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		for i in range(4):
			self._run(now + timedelta(hours=i))
		self.assertEqual(len(self.sent), 1)

	def test_dedupe_is_enforced_by_the_database(self):
		lc.record_event("transition", subscription=self.sub, dedupe_key="k-unique")
		self.assertIsNone(lc.record_event("transition", subscription=self.sub, dedupe_key="k-unique"))
		dup = frappe.get_doc({"doctype": "Space Lifecycle Event", "kind": "transition", "dedupe_key": "k-unique"})
		with self.assertRaises(frappe.UniqueValidationError):
			dup.insert(ignore_permissions=True)

	def test_queued_is_not_delivered_a_queue_error_means_no_deletion(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warning_Pending")
		self._confirm_mail(now + timedelta(hours=1), status="Error")  # SMTP rejected it
		self.assertEqual(self._run(now + timedelta(hours=2)), "warning_duplicate")  # failed, retry pending: nothing is scheduled
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertEqual(frappe.db.get_value("Space Lifecycle Event", {"kind": "inactivity_warning_email"}, "status"), "Failed")

	def test_a_failed_send_is_retried_and_only_then_scheduled(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.mail_mode = "raise"
		self.assertEqual(self._run(now), "warning_Failed")
		self.assertEqual(self._status(), lc.TRIAL)
		self.mail_mode = "queue"
		self.assertEqual(mail.retry_failed(now + timedelta(minutes=1)), 0)  # not due yet
		self.assertEqual(mail.retry_failed(now + timedelta(hours=2)), 1)
		self._confirm_mail(now + timedelta(hours=3))
		self.assertEqual(self._run(now + timedelta(hours=4)), "scheduled")  # the late "Sent" is acted on, not forgotten

	def test_a_mail_frappe_drops_is_a_failure_not_a_success(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.mail_mode = "drop"  # e.g. the recipient unsubscribed: frappe.sendmail returns nothing
		self.assertEqual(self._run(now), "warning_Failed")
		self.assertEqual(self._status(), lc.TRIAL)

	def test_email_notifications_off_means_no_warning_then_it_resumes_when_turned_on(self):
		self._start()
		frappe.db.set_single_value("Space Settings", "email_notifications_enabled", 0)
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "warning_Skipped")
		self.assertEqual(self._status(), lc.TRIAL)
		frappe.db.set_single_value("Space Settings", "email_notifications_enabled", 1)
		self.assertEqual(self._run(now + timedelta(hours=1)), "warning_Pending")  # re-armed, not stuck on "duplicate"
		self.assertEqual(len(self.sent), 1)

	def test_a_notice_that_failed_all_its_retries_is_tried_again_after_a_day(self):
		self._start()
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.mail_mode = "raise"
		self._run(now)
		last = now
		for i in range(1, mail.MAX_RETRIES + 1):
			last = now + timedelta(hours=7 * i)
			mail.retry_failed(last)
		self.assertEqual(self._run(last + timedelta(hours=1)), "warning_duplicate")  # exhausted, but not a day old yet
		self.mail_mode = "queue"
		self.assertEqual(self._run(last + mail.REARM_AFTER + timedelta(hours=1)), "warning_Pending")

	def test_with_auto_deletion_off_nobody_is_contacted_and_nothing_is_parked(self):
		self._start()
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 0)
		now = T0 + timedelta(days=12)
		self._inactive(10, now)
		self.assertEqual(self._run(now), "dry_run_warn")
		self.assertEqual(self._run(now + timedelta(hours=1)), "dry_run_warn")
		self.assertEqual(self.sent, [])
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertEqual(frappe.db.count("Space Lifecycle Event", {"kind": "deletion_dry_run", "subscription": self.sub}), 1)

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

	def test_a_cancelled_episode_is_warned_again_when_the_customer_goes_quiet_again(self):
		now, due = self._warned_and_scheduled()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_e", amount=49, currency="SAR")  # cancels + new episode
		self.assertEqual(self._status(), lc.ACTIVE)
		paid_until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		later = paid_until + timedelta(days=3, hours=2)
		self.assertEqual(self._run(later), "pending_payment")
		self._set(activity_checked_at=later)  # same last_activity as before the payment: the old key would be "duplicate"
		self.sent.clear()
		self.assertEqual(self._run(later + timedelta(hours=1)), "warning_Pending")
		self.assertEqual(len(self.sent), 1)


# --- scheduled deletion --------------------------------------------------------------------------------------------------

class TestDeletion(LifecycleBase):
	def test_activity_after_the_warning_cancels_deletion(self):
		now, due = self._warned_and_scheduled()
		self._set(last_activity_at=now + timedelta(days=2), activity_checked_at=due)
		self.assertEqual(self._run(due + timedelta(minutes=1)), "cancelled")
		self.assertEqual(self._status(), lc.TRIAL)
		self.assertIsNone(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"kind": "deletion_cancelled", "subscription": self.sub}))

	def test_payment_after_the_warning_cancels_deletion(self):
		now, due = self._warned_and_scheduled()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_late", amount=49, currency="SAR")
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertNotEqual(self._run(due + timedelta(minutes=1)), "deleting")
		self.assertEqual(self.deleted, [])

	def test_day_15_deletes_exactly_once_and_only_this_site_with_its_source_and_date(self):
		other_site, other_sub = self._site(self.customer, self.plan)
		lc.start_trial(other_sub, started_at=T0)
		now, due = self._warned_and_scheduled()
		self.assertEqual(self._run(due + timedelta(minutes=1)), "deleting")
		self.assertEqual(self._run(due + timedelta(minutes=5)), "already_started")
		self.assertEqual(self.deleted, [(self.site, "inactivity", due.isoformat())])
		self.assertEqual(frappe.db.get_value("Space Subscription", other_sub, "lifecycle_status"), lc.TRIAL)

	def test_with_auto_deletion_switched_off_after_scheduling_day_15_is_a_dry_run(self):
		now, due = self._warned_and_scheduled()
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 0)
		self.assertEqual(self._run(due + timedelta(minutes=1)), "dry_run")
		self.assertEqual(self.deleted, [])
		self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"kind": "deletion_dry_run", "subscription": self.sub}))

	def test_not_due_yet_waits(self):
		now, due = self._warned_and_scheduled()
		self.assertEqual(self._run(due - timedelta(hours=3)), "wait")
		self.assertEqual(self.deleted, [])

	def test_recheck_that_cannot_read_activity_never_deletes(self):
		now, due = self._warned_and_scheduled()
		with mock.patch("space.jobs.lifecycle.refresh_activity", return_value=False):
			self.assertEqual(self._run(due + timedelta(days=1)), "recheck_unavailable")
		self.assertEqual(self.deleted, [])

	def test_deletion_waits_if_the_warning_mail_is_no_longer_confirmed_delivered(self):
		now, due = self._warned_and_scheduled()
		frappe.db.set_value("Space Lifecycle Event", {"kind": "inactivity_warning_email", "subscription": self.sub}, "status", "Failed")
		self.assertEqual(self._run(due + timedelta(minutes=1)), "waiting_for_warning")
		self.assertEqual(self.deleted, [])

	def test_a_change_of_customer_since_the_warning_cancels_the_deletion(self):
		now, due = self._warned_and_scheduled()
		other_customer, _ = self._customer()
		frappe.db.set_value("Space Site", self.site, "customer", other_customer)
		self.assertEqual(self._run(due + timedelta(minutes=1)), "cancelled")  # the new owner was never warned
		self.assertEqual(self.deleted, [])

	def test_a_stale_manual_flag_cannot_delete_a_site_that_was_used_after_its_warning(self):
		"""The reviewer's sequence: admin schedules -> customer pays (cancels) -> lapses -> automatic warning ->
		customer signs in. The old deletion_manual=1 must not survive and skip the activity re-check."""
		from space.api import lifecycle as api

		self._start()
		api.schedule_deletion(self.site, 5, self.site, "cleanup")
		self.assertEqual(frappe.db.get_value("Space Subscription", self.sub, "deletion_manual"), 1)
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_m", amount=49, currency="SAR")
		self.assertEqual(frappe.db.get_value("Space Subscription", self.sub, "deletion_manual"), 0)
		paid_until = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until"))
		now = paid_until + timedelta(days=3, hours=2)
		self._set(activity_checked_at=now, last_activity_at=now - timedelta(days=11))
		self._run(now)
		self.assertEqual(self._run(now + timedelta(hours=1)), "warning_Pending")
		self._confirm_mail(now + timedelta(hours=2))
		self.assertEqual(self._run(now + timedelta(hours=3)), "scheduled")
		self.assertEqual(frappe.db.get_value("Space Subscription", self.sub, "deletion_manual"), 0)
		due = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		self._set(last_activity_at=now + timedelta(days=1), activity_checked_at=due)  # the customer signed in after the warning
		self.assertEqual(self._run(due + timedelta(minutes=1)), "cancelled")
		self.assertEqual(self.deleted, [])

	def test_every_route_out_of_a_scheduled_deletion_clears_the_episode(self):
		from space.api import lifecycle as api

		routes = {
			"payment": lambda sub: lc.record_payment(sub, provider="moyasar", payment_id=f"p{random_string(6)}", amount=49, currency="SAR"),
			"admin cancel": lambda sub: lc.cancel_deletion(sub, reason="x"),
			"hold": lambda sub: lc.set_hold(sub, True, reason="x"),
			"extend trial": lambda sub: lc.extend_trial(sub, 5, reason="x"),
			"suspend": lambda sub: lc.admin_suspend(sub, reason="x"),
			"activate": lambda sub: lc.admin_activate(sub, 1, reason="x"),
		}
		for name, leave in routes.items():
			site, sub = self._site(self.customer, self.plan)  # a fresh subscription per route
			lc.start_trial(sub, started_at=T0)
			api.schedule_deletion(site, 5, site, "cleanup")
			self.assertEqual(self._status(sub), lc.DELETION_SCHEDULED, name)
			self.assertEqual(frappe.db.get_value("Space Subscription", sub, "deletion_manual"), 1, name)
			leave(sub)
			row = frappe.db.get_value("Space Subscription", sub, ["lifecycle_status", "deletion_manual", "deletion_scheduled_at", "inactivity_warning_sent_at", "pre_deletion_status"], as_dict=True)
			self.assertNotEqual(row.lifecycle_status, lc.DELETION_SCHEDULED, name)
			self.assertEqual((row.deletion_manual, row.deletion_scheduled_at, row.inactivity_warning_sent_at, row.pre_deletion_status), (0, None, None, None), name)

	def test_guard_refuses_unscheduled_held_paid_protected_and_a_moved_date(self):
		from space_cloud.jobs import lifecycle as sc

		self._start()
		site = frappe.get_doc("Space Site", self.site)
		with self.assertRaises(lc.DeletionRefused):
			sc.guard_deletable(site, "inactivity", None)  # not scheduled
		now, due = self._warned_and_scheduled()
		sc.guard_deletable(site, "inactivity", due.isoformat())  # passes: scheduled, warned, same date
		with self.assertRaises(lc.DeletionRefused):
			sc.guard_deletable(site, "inactivity", (due + timedelta(days=1)).isoformat())  # the date moved
		self._set(deletion_hold=1)
		with self.assertRaises(lc.DeletionRefused):
			sc.guard_deletable(site, "inactivity", due.isoformat())
		self._set(deletion_hold=0)
		frappe.db.set_single_value("Space Settings", "protected_domains", site.domain)
		with self.assertRaises(lc.DeletionRefused):
			sc.guard_deletable(site, "user", None)  # protected for every route, the customer's own button included

	def _run_delete_job(self, source, expected, bench_calls):
		from space_cloud.jobs import lifecycle as sc

		site = frappe.get_doc("Space Site", self.site)
		jobdoc = frappe.get_doc({"doctype": "Space Deployment Job", "site": site.name, "server": site.server, "job_type": "Delete",
			"status": "Queued", "progress": 0}).insert(ignore_permissions=True)

		def list_sites(server):
			bench_calls.append("list")
			return [site.domain] if "drop" not in bench_calls else []

		# notifications.notify() commits (it is job code); stubbed so the test's rollback can undo everything
		with mock.patch.object(sc.notifications, "notify"), \
				mock.patch.object(sc.bench_client, "list_sites", side_effect=list_sites), \
				mock.patch.object(sc.bench_client, "drop_site", side_effect=lambda server, domain: bench_calls.append("drop")), \
				mock.patch.object(sc.bench_client, "purge_archived_site", side_effect=lambda server, domain: bench_calls.append("purge")), \
				mock.patch.object(sc.bench_client, "remove_route", side_effect=lambda server, domain: bench_calls.append("route")):
			sc.run_delete_site(jobdoc.name, source=source, expected_scheduled_at=expected)
		return frappe.get_doc("Space Deployment Job", jobdoc.name)

	def test_a_deletion_already_queued_is_stopped_when_the_customer_pays_before_it_runs(self):
		now, due = self._warned_and_scheduled()
		self.assertEqual(self._run(due + timedelta(minutes=1)), "deleting")  # queued...
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_race", amount=49, currency="SAR")  # ...then they pay
		calls: list[str] = []
		result = self._run_delete_job("inactivity", due.isoformat(), calls)
		self.assertEqual(result.status, "Failed")
		self.assertIn("NOT DELETED", result.output)
		self.assertNotIn("drop", calls)  # the site was never dropped
		self.assertEqual(frappe.db.get_value("Space Site", self.site, "status"), "Active")
		self.assertEqual(self._status(), lc.ACTIVE)

	def test_a_customer_deleting_their_own_site_marks_the_subscription_deleted_from_any_state(self):
		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_d", amount=49, currency="SAR")
		self.assertEqual(self._status(), lc.ACTIVE)
		calls: list[str] = []
		result = self._run_delete_job("user", None, calls)
		self.assertEqual(result.status, "Succeeded")
		self.assertEqual(frappe.db.get_value("Space Site", self.site, "status"), "Deleted")
		self.assertEqual(self._status(), lc.DELETED)  # not left looking ACTIVE
		self.assertIn("purge", calls)

	def test_deleting_one_of_two_sites_sharing_a_subscription_keeps_the_subscription(self):
		self._start()
		second = frappe.get_doc({"doctype": "Space Site", "site_name": f"s{random_string(6).lower()}", "customer": self.customer,
			"server": self._server(), "plan": self.plan, "subscription": self.sub, "status": "Active"}).insert(ignore_permissions=True)
		calls: list[str] = []
		self._run_delete_job("user", None, calls)
		self.assertEqual(frappe.db.get_value("Space Site", self.site, "status"), "Deleted")
		self.assertEqual(self._status(), lc.TRIAL)  # the other site still lives on it
		self.assertEqual(frappe.db.get_value("Space Site", second.name, "status"), "Active")


# --- admin ------------------------------------------------------------------------------------------------------------

class TestAdmin(LifecycleBase):
	def test_admin_extend_activate_suspend_reactivate_cancel(self):
		from space.api import lifecycle as api

		self._start()
		self._run(T0 + timedelta(days=31))
		self.assertEqual(self._status(), lc.EXPIRED)
		api.extend_trial(self.site, 7, "goodwill")
		self.assertEqual(self._status(), lc.TRIAL)
		api.suspend(self.site, "abuse")
		self.assertEqual(self._status(), lc.SUSPENDED)
		self.assertEqual(api.reactivate(self.site, "resolved")["data"]["lifecycle_status"], lc.TRIAL)
		api.activate(self.site, 2, "paid offline")
		self.assertEqual(self._status(), lc.ACTIVE)
		self.assertGreater(lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "paid_until")), lc.utcnow() + timedelta(days=59))
		self.assertGreaterEqual(len(frappe.get_all("Space Lifecycle Event", filters={"subscription": self.sub, "kind": "admin_action"})), 4)

	def test_extend_trial_works_from_pending_payment_and_refuses_suspended_and_deleted(self):
		from space.api import lifecycle as api

		self._start()
		lc.transition(self.sub, lc.ACTIVE, reason="test", push=False)
		lc.transition(self.sub, lc.PENDING_PAYMENT, reason="test", push=False)
		api.extend_trial(self.site, 5, "more time")
		self.assertEqual(self._status(), lc.TRIAL)
		lc.admin_suspend(self.sub, reason="abuse")
		with self.assertRaises(frappe.ValidationError):
			api.extend_trial(self.site, 5, "no")
		lc.transition(self.sub, lc.DELETED, reason="test", push=False)
		for call in (lambda: api.extend_trial(self.site, 5, "x"), lambda: api.suspend(self.site, "x"), lambda: api.set_hold(self.site, 1, "x"), lambda: api.activate(self.site, 1, "x")):
			with self.assertRaises(frappe.ValidationError):
				call()

	def test_suspending_and_reactivating_a_free_plan_site_returns_it_to_active(self):
		from space.api import lifecycle as api

		site, sub = self._site(self.customer, self._plan(price=0))
		lc.start_trial(sub, started_at=T0)
		api.suspend(site, "abuse check")
		self.assertEqual(self._status(sub), lc.SUSPENDED)
		result = api.reactivate(site, "resolved")
		self.assertEqual(result["data"]["lifecycle_status"], lc.ACTIVE)  # not EXPIRED: nothing to pay on a free plan
		self.assertFalse(result["data"]["read_only"])

	def test_admin_manual_schedule_needs_typed_confirmation_a_queued_notice_and_runs_without_the_auto_switch(self):
		from space.api import lifecycle as api

		self._start()
		self.assertFalse(api.schedule_deletion(self.site, 5, "wrong-name", "cleanup")["ok"])
		self.assertTrue(api.schedule_deletion(self.site, 5, self.site, "cleanup")["ok"])
		self.assertEqual(self._status(), lc.DELETION_SCHEDULED)
		self.assertEqual(len(self.sent), 1)
		due = lc.as_utc(frappe.db.get_value("Space Subscription", self.sub, "deletion_scheduled_at"))
		frappe.db.set_single_value("Space Settings", "auto_deletion_enabled", 0)  # the explicit admin decision does not need it
		self.assertEqual(self._run(due - timedelta(hours=1)), "wait")
		self.assertEqual(self._run(due + timedelta(minutes=1)), "waiting_for_warning")  # the notice is not yet confirmed delivered
		self._confirm_mail(due + timedelta(minutes=2))
		self.assertEqual(self._run(due + timedelta(minutes=3)), "deleting")
		self.assertEqual(self.deleted, [(self.site, "admin_schedule", due.isoformat())])

	def test_manual_schedule_on_a_paid_site_is_not_silently_undone(self):
		from space.api import lifecycle as api

		self._start()
		lc.record_payment(self.sub, provider="moyasar", payment_id="pay_mp", amount=49, currency="SAR")
		self.assertTrue(api.schedule_deletion(self.site, 5, self.site, "customer asked to close")["ok"])
		self.assertEqual(self._run(lc.utcnow() + timedelta(hours=1)), "wait")  # stays scheduled: paid_until is ignored
		self.assertEqual(self._status(), lc.DELETION_SCHEDULED)

	def test_manual_schedule_is_refused_when_the_customer_cannot_be_emailed(self):
		from space.api import lifecycle as api

		self._start()
		self.mail_mode = "raise"
		result = api.schedule_deletion(self.site, 5, self.site, "cleanup")
		self.assertFalse(result["ok"])
		self.assertEqual(self._status(), lc.TRIAL)  # nothing scheduled behind a notice that was never queued

	def test_exclusion_cancels_a_scheduled_deletion(self):
		from space.api import lifecycle as api

		self._warned_and_scheduled()
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


# --- scheduler, tenant state, backfill ------------------------------------------------------------------------------------

class TestScheduler(LifecycleBase):
	def test_scheduler_run_is_safe_to_repeat(self):
		self._start()
		job.run_hourly()
		mid = frappe.db.count("Space Lifecycle Event")
		a = job.run_hourly()
		self.assertEqual(frappe.db.count("Space Lifecycle Event"), mid)
		self.assertEqual(a["errors"], 0)

	def test_an_exhausted_time_budget_is_reported_not_silent(self):
		self._start()
		result = job.run_hourly(budget_s=-1)
		self.assertGreaterEqual(result["skipped_for_time"], 1)
		self.assertEqual(result["processed"], 0)

	def _mine(self):
		return [p for _site, p in [(s, p) for s, p in self.pushed] if _site == self.site]

	def test_every_managed_tenant_is_told_its_state_and_told_again_when_it_changes(self):
		self._start()
		self.pushed.clear()
		lc.reconcile_pushes(lc.utcnow())
		self.assertEqual(self._mine(), [])  # start_trial already pushed it: its hash is stored, nothing is stale
		frappe.db.set_single_value("Space Settings", "lifecycle_enforcement_enabled", 0)  # the kill switch
		lc.reconcile_pushes(lc.utcnow())
		self.assertEqual(len(self._mine()), 1)
		self.assertEqual(self._mine()[-1]["enforcement_on"], 0)  # the tenant is released, not left read-only
		lc.reconcile_pushes(lc.utcnow())
		self.assertEqual(len(self._mine()), 1)  # told once, not every hour

	def test_a_failed_push_is_retried_by_the_reconcile(self):
		self._start()
		with mock.patch("space_cloud.services.tenant_sync.push_state", side_effect=RuntimeError("tenant unreachable")):
			lc.record_payment(self.sub, provider="moyasar", payment_id="pay_f", amount=49, currency="SAR")  # push fails, swallowed
		self.assertEqual(self._status(), lc.ACTIVE)
		self.pushed.clear()
		lc.reconcile_pushes(lc.utcnow())  # the paying customer's tenant gets ACTIVE after all
		self.assertEqual([p["status"] for p in self._mine()], [lc.ACTIVE])

	def test_a_heartbeat_repushes_once_a_day(self):
		self._start()
		self.pushed.clear()
		lc.reconcile_pushes(lc.utcnow() + timedelta(hours=25))
		self.assertEqual(len(self._mine()), 1)


class TestBackfill(LifecycleBase):
	"""The rollout backfill: existing customer sites get a trial; real company sites that Space merely tracks do not."""

	def _state(self, sub):
		return frappe.db.get_value("Space Subscription", sub, ["lifecycle_status", "trial_started_at", "trial_ends_at", "paid_until"], as_dict=True)

	def test_paid_plan_gets_a_fresh_30_day_trial_free_plan_stays_active(self):
		free_site, free_sub = self._site(self.customer, self._plan(price=0))
		counts = lc.backfill_missing(T0)
		self.assertGreaterEqual(counts["trial"], 1)
		paid = self._state(self.sub)
		self.assertEqual(paid.lifecycle_status, lc.TRIAL)
		self.assertEqual(lc.as_utc(paid.trial_ends_at), T0 + timedelta(days=30))
		self.assertEqual(self._state(free_sub).lifecycle_status, lc.ACTIVE)
		self.assertIsNone(self._state(free_sub).trial_ends_at)

	def test_a_customer_who_really_paid_keeps_what_they_paid_for(self):
		end = (lc.utcnow() + timedelta(days=40)).date()
		self._set(payment_status="Paid", end_date=end)
		lc.backfill_missing()
		state = self._state(self.sub)
		self.assertEqual(state.lifecycle_status, lc.ACTIVE)
		self.assertEqual(lc.as_utc(state.paid_until).date(), end)

	def test_protected_sites_never_get_a_trial_clock(self):
		domain = frappe.db.get_value("Space Site", self.site, "domain")
		frappe.db.set_single_value("Space Settings", "protected_domains", f"other.example.com\n{domain}")
		counts = lc.backfill_missing(T0)
		self.assertEqual(self._state(self.sub).lifecycle_status, "")
		self.assertGreaterEqual(counts["skipped_protected"], 1)

	def test_a_subscription_shared_with_a_protected_site_is_skipped(self):
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


# --- billing period (Monthly / Yearly) and the badge on every site ----------------------------------------------------

class TestBillingPeriod(LifecycleBase):
	def _yearly_plan(self, monthly=49, yearly=490):
		code = f"y{random_string(6).lower()}"
		return frappe.get_doc({"doctype": "Space Plan", "code": code, "title": code, "monthly_price": monthly,
			"yearly_price": yearly, "is_active": 1, "trial_days": 14}).insert(ignore_permissions=True).name

	def _yearly_site(self):
		plan = self._yearly_plan()
		site, sub = self._site(self.customer, plan)
		lc.start_trial(sub, started_at=T0)
		return site, sub

	def test_new_subscriptions_are_monthly_and_the_view_says_so(self):
		self._start()
		v = lc.view(self.sub, T0)
		self.assertEqual(v["billing_period"], "Monthly")
		self.assertEqual(v["period_price"], 49)
		self.assertFalse(v["protected"])

	def test_admin_sets_yearly_and_a_payment_buys_a_year(self):
		site, sub = self._yearly_site()
		self.assertEqual(lc.set_billing_period(sub, "Yearly", reason="annual deal", actor="Administrator"), "Yearly")
		v = lc.view(sub, T0)
		self.assertEqual((v["billing_period"], v["period_price"], v["yearly_price"]), ("Yearly", 490, 490))
		res = lc.record_payment(sub, provider="moyasar", payment_id="yr_1", amount=490, currency="SAR", months=1)
		self.assertEqual(res["months"], 12)  # the caller's months is ignored: the period decides
		paid = lc.as_utc(frappe.db.get_value("Space Subscription", sub, "paid_until"))
		self.assertGreaterEqual(paid - lc.utcnow(), timedelta(days=364))
		again = lc.record_payment(sub, provider="moyasar", payment_id="yr_1", amount=490, currency="SAR")
		self.assertFalse(again["applied"])  # still idempotent
		self.assertEqual(lc.as_utc(frappe.db.get_value("Space Subscription", sub, "paid_until")), paid)

	def test_a_monthly_payment_still_buys_a_month(self):
		self._start()
		res = lc.record_payment(self.sub, provider="moyasar", payment_id="m_1", amount=49, currency="SAR", months=12)
		self.assertEqual(res["months"], 1)  # a caller cannot ask for a year on a monthly site

	def test_changing_the_period_does_not_touch_what_is_already_paid(self):
		site, sub = self._yearly_site()
		lc.record_payment(sub, provider="moyasar", payment_id="m_a", amount=49, currency="SAR")
		before = frappe.db.get_value("Space Subscription", sub, "paid_until")
		lc.set_billing_period(sub, "Yearly", reason="switch", actor="Administrator")
		self.assertEqual(frappe.db.get_value("Space Subscription", sub, "paid_until"), before)

	def test_set_period_is_validated(self):
		site, sub = self._yearly_site()
		with self.assertRaises(frappe.ValidationError):
			lc.set_billing_period(sub, "Weekly", reason="x")
		no_yearly, sub2 = self._site(self.customer, self._yearly_plan(monthly=49, yearly=0))
		with self.assertRaises(frappe.ValidationError):
			lc.set_billing_period(sub2, "Yearly", reason="x")  # no yearly price on the plan
		free_site, sub3 = self._site(self.customer, self._yearly_plan(monthly=0, yearly=0))
		with self.assertRaises(frappe.ValidationError):
			lc.set_billing_period(sub3, "Yearly", reason="x")  # a free plan has nothing to bill
		lc.transition(sub, lc.DELETED, reason="t", push=False)
		with self.assertRaises(frappe.ValidationError):
			lc.set_billing_period(sub, "Yearly", reason="x")

	def test_set_period_is_audited_pushed_and_idempotent(self):
		site, sub = self._yearly_site()
		self.pushed.clear()
		lc.set_billing_period(sub, "Yearly", reason="annual deal", actor="Administrator")
		self.assertTrue(frappe.db.exists("Space Lifecycle Event", {"subscription": sub, "kind": "admin_action"}))
		self.assertEqual(self.pushed[-1][1]["billing_period"], "Yearly")
		n = len(self.pushed)
		lc.set_billing_period(sub, "Yearly", reason="again", actor="Administrator")  # no change: nothing sent
		self.assertEqual(len(self.pushed), n)

	def test_a_period_change_makes_reconcile_resend(self):
		site, sub = self._yearly_site()
		lc.push_state(sub)
		self.assertEqual(lc.reconcile_pushes(now=lc.utcnow())["unchanged"] >= 1, True)
		frappe.db.set_value("Space Subscription", sub, "billing_period", "Yearly")
		self.pushed.clear()
		lc.reconcile_pushes(now=lc.utcnow())
		self.assertTrue(any(p[1].get("billing_period") == "Yearly" for p in self.pushed))

	def test_the_api_requires_an_admin_and_a_reason(self):
		from space.api import lifecycle as api
		site, sub = self._yearly_site()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.set_billing_period(site, "Yearly", "r")
		frappe.set_user("Administrator")
		out = api.set_billing_period(site, "Yearly", "annual deal")
		self.assertEqual(out["data"]["billing_period"], "Yearly")

	def test_protected_sites_show_active_and_nothing_is_written(self):
		from space.api import lifecycle as api
		site, sub = self._yearly_site()
		domain = frappe.db.get_value("Space Site", site, "domain")
		frappe.db.set_value("Space Subscription", sub, "lifecycle_status", None)  # a legacy row with no state
		frappe.db.set_single_value("Space Settings", "protected_domains", domain or site)
		v = api.get_lifecycle(site)["data"]
		self.assertEqual((v["lifecycle_status"], v["protected"], v["read_only"]), ("ACTIVE", True, False))
		self.assertIn(site, api.list_lifecycle()["data"])
		self.assertFalse(frappe.db.get_value("Space Subscription", sub, "lifecycle_status"))  # display only
		self.assertEqual(lc.backfill_missing()["skipped_protected"] >= 1, True)  # still no trial clock

	def test_protected_tenant_gets_an_informational_state_that_enforces_nothing(self):
		site, sub = self._yearly_site()
		frappe.db.set_value("Space Subscription", sub, "lifecycle_status", None)
		domain = frappe.db.get_value("Space Site", site, "domain")
		frappe.db.set_single_value("Space Settings", "protected_domains", domain or site)
		self.pushed.clear()
		lc.reconcile_protected()
		pushed = [p for p in self.pushed if p[0] == site]
		self.assertTrue(pushed)
		self.assertEqual(pushed[0][1]["enforcement_on"], 0)
		self.assertEqual(pushed[0][1]["status"], "ACTIVE")
