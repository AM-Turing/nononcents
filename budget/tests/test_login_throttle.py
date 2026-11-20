import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from budget import login_throttle
from budget.models import AuditEvent, Household, HouseholdMembership, LoginFailure
from nononcents import settings

PASSWORD = "Correct-horse-battery-91!"
MESSAGE = "Too many sign-in attempts. Try again in"


class LoginThrottleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.household = Household.objects.create(name="Home")
        self.user = User.objects.create_user("Alice", password=PASSWORD)
        self.other = User.objects.create_user("bob", password=PASSWORD)
        for user in (self.user, self.other):
            HouseholdMembership.objects.create(household=self.household, user=user)

    def login(self, username="Alice", password="wrong", ip="10.9.9.9", **extra):
        return self.client.post(
            reverse("login"),
            {"profile": self.household.pk, "username": username, "password": password},
            REMOTE_ADDR=ip,
            **extra,
        )

    def fail_attempts(self, n, username="Alice", ip="10.9.9.9"):
        for _ in range(n):
            self.login(username, "wrong", ip)

    def test_five_failures_lock_username_even_for_correct_password(self):
        self.fail_attempts(5)
        response = self.login("Alice", PASSWORD)
        self.assertContains(response, MESSAGE)
        self.assertContains(response, "15 minutes")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_username_match_is_case_insensitive(self):
        self.fail_attempts(5, "ALICE")
        self.assertContains(self.login(" alice ", PASSWORD), MESSAGE)

    def test_four_failures_do_not_lock(self):
        self.fail_attempts(4)
        self.assertEqual(self.login("Alice", PASSWORD).status_code, 302)

    def test_other_username_from_other_ip_still_works(self):
        self.fail_attempts(5)
        response = self.login("bob", PASSWORD, ip="10.8.8.8")
        self.assertEqual(response.status_code, 302)

    def test_username_locked_on_one_device_still_works_on_another(self):
        self.fail_attempts(5, "Alice", ip="10.9.9.9")
        self.assertContains(self.login("Alice", PASSWORD, ip="10.9.9.9"), MESSAGE)
        self.assertEqual(self.login("Alice", PASSWORD, ip="10.8.8.8").status_code, 302)

    def test_blocked_attempts_do_not_add_rows(self):
        self.fail_attempts(5)
        self.fail_attempts(4)
        self.assertEqual(LoginFailure.objects.count(), 5)
        self.assertEqual(AuditEvent.objects.filter(action="login_failed").count(), 5)

    def test_unknown_user_gets_same_message(self):
        self.fail_attempts(5, "ghost")
        self.assertContains(self.login("ghost", "x"), MESSAGE)

    def test_twenty_failures_across_usernames_lock_ip(self):
        for i in range(20):
            self.login(f"user{i}", "wrong", "10.7.7.7")
        response = self.login("bob", PASSWORD, ip="10.7.7.7")
        self.assertContains(response, MESSAGE)

    def test_lock_expires_after_window(self):
        self.fail_attempts(5)
        LoginFailure.objects.update(
            at=timezone.now() - login_throttle.WINDOW - timedelta(seconds=1)
        )
        self.assertEqual(self.login("Alice", PASSWORD).status_code, 302)

    def test_remaining_time_is_rounded_up(self):
        self.fail_attempts(5)
        LoginFailure.objects.update(at=timezone.now() - timedelta(minutes=10, seconds=30))
        self.assertContains(self.login("Alice", PASSWORD), "Try again in 5 minutes.")

    def test_success_clears_only_that_username_and_device(self):
        self.fail_attempts(2, "Alice")
        self.fail_attempts(2, "bob")
        self.assertEqual(self.login("Alice", PASSWORD).status_code, 302)
        self.assertFalse(LoginFailure.objects.filter(username_key="alice").exists())
        self.assertEqual(LoginFailure.objects.filter(username_key="bob").count(), 2)
        self.fail_attempts(1, "Alice", ip="10.8.8.8")
        self.assertEqual(self.login("Alice", PASSWORD).status_code, 302)
        self.assertEqual(LoginFailure.objects.filter(username_key="alice").count(), 1)

    def test_old_rows_pruned_on_failure(self):
        LoginFailure.objects.create(
            username_key="old", ip="10.1.1.1", at=timezone.now() - timedelta(days=2)
        )
        self.fail_attempts(1)
        self.assertFalse(LoginFailure.objects.filter(username_key="old").exists())

    def test_x_real_ip_honoured_only_from_trusted_proxy(self):
        self.login("Alice", "wrong", ip="172.20.0.1", HTTP_X_REAL_IP="203.0.113.5")
        self.login("Alice", "wrong", ip="198.51.100.9", HTTP_X_REAL_IP="203.0.113.6")
        self.login("Alice", "wrong", ip="172.20.0.1", HTTP_X_REAL_IP="not-an-ip")
        ips = list(LoginFailure.objects.order_by("id").values_list("ip", flat=True))
        self.assertEqual(ips, ["203.0.113.5", "198.51.100.9", "172.20.0.1"])

    def test_x_forwarded_for_ignored(self):
        self.login("Alice", "wrong", ip="172.20.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.login("Alice", "wrong", ip="198.51.100.9", HTTP_X_FORWARDED_FOR="203.0.113.7")
        ips = list(LoginFailure.objects.order_by("id").values_list("ip", flat=True))
        self.assertEqual(ips, ["172.20.0.1", "198.51.100.9"])

    def test_no_password_stored(self):
        self.login("Alice", "Secret-typed-pw-1!")
        fields = {f.name for f in LoginFailure._meta.get_fields()}
        self.assertEqual(fields, {"id", "username_key", "ip", "at"})
        row = LoginFailure.objects.get()
        self.assertNotIn("Secret-typed-pw", repr(row.__dict__))

    def test_lock_log_has_no_username_or_password(self):
        with self.assertLogs("budget.security", level="WARNING") as logs:
            self.fail_attempts(5)
        self.assertEqual(len(logs.records), 1)
        text = logs.output[0].lower()
        self.assertIn("10.9.9.9", text)
        self.assertNotIn("alice", text)
        self.assertNotIn("wrong", text)

    def test_trusted_proxy_setting_validation(self):
        with mock.patch.dict(os.environ, {"TRUSTED_PROXY_NETWORKS": "nonsense"}):
            with self.assertRaises(ImproperlyConfigured):
                settings._trusted_proxy_networks()
        with mock.patch.dict(os.environ, {"TRUSTED_PROXY_NETWORKS": "10.0.0.5/8"}):
            self.assertEqual(str(settings._trusted_proxy_networks()[0]), "10.0.0.0/8")
