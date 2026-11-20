from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budget.models import (
    Account,
    ApplicationSetting,
    AuditEvent,
    Category,
    Expense,
    Household,
    HouseholdMembership,
)


class ApplicationAdminTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create()
        self.staff = get_user_model().objects.create_user(
            username="admin", password="test-password", is_staff=True, is_superuser=True
        )
        self.member = get_user_model().objects.create_user(
            username="member", password="test-password"
        )
        HouseholdMembership.objects.create(household=self.household, user=self.staff)
        HouseholdMembership.objects.create(household=self.household, user=self.member)

    def test_admin_page_requires_staff_access(self):
        self.client.force_login(self.member)

        response = self.client.get(reverse("application_admin"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_staff_can_rename_pages(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_admin"),
            {"intent": "save_page_name", "page-page": "reports", "page-name": "  Insights "},
        )

        self.assertRedirects(response, reverse("application_admin") + "?page=reports")
        setting = ApplicationSetting.objects.get(key="page_labels")
        self.assertEqual(setting.value, {"reports": "Insights"})
        self.assertContains(self.client.get(reverse("reports")), "<h1>Insights</h1>", html=True)

    def test_every_menu_page_can_be_renamed_and_reset(self):
        self.client.force_login(self.staff)
        admin = self.client.get(reverse("application_admin")).content.decode()
        for name in (
            "Home",
            "History",
            "Bills",
            "Budgets",
            "Savings",
            "Outlook",
            "Habits",
            "Debts",
            "Accounts",
            "Categories",
            "Admin",
        ):
            self.assertIn(f'data-original="{name}"', admin)

        self.client.post(
            reverse("application_admin"),
            {"intent": "save_page_name", "page-page": "habits", "page-name": "Patterns"},
        )
        page = self.client.get(reverse("habits")).content.decode()
        self.assertIn("<h1>Patterns</h1>", page)
        self.assertEqual(page.count(">Patterns</a>"), 2)  # sidebar and phone More menu
        self.assertIn(
            "Habits — now “Patterns”",
            self.client.get(reverse("application_admin")).content.decode(),
        )

        self.client.post(
            reverse("application_admin"),
            {"intent": "save_page_name", "page-page": "habits", "page-name": ""},
        )
        self.assertEqual(ApplicationSetting.objects.get(key="page_labels").value, {})
        self.assertIn("<h1>Habits</h1>", self.client.get(reverse("habits")).content.decode())

    def test_unknown_page_is_refused(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("application_admin"),
            {"intent": "save_page_name", "page-page": "nope", "page-name": "X"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ApplicationSetting.objects.filter(key="page_labels").exists())

    def test_staff_can_add_and_remove_quick_action(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_admin"),
            {
                "intent": "add_action",
                "action-label": "Review activity",
                "action-destination": "transactions",
            },
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.assertContains(self.client.get(reverse("dashboard")), "Review activity")

        response = self.client.post(
            reverse("application_admin"), {"intent": "delete_action", "index": "0"}
        )
        self.assertRedirects(response, reverse("application_admin"))
        self.assertNotContains(self.client.get(reverse("dashboard")), "Review activity")

    def test_staff_can_edit_profile_alerts_without_logging_threshold_value(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_admin"),
            {
                "intent": "save_alerts",
                "alerts-spending_low_balance_enabled": "on",
                "alerts-spending_low_balance_threshold": "125.50",
                "alerts-bill_confirmation_enabled": "on",
            },
        )

        self.assertRedirects(response, reverse("application_admin"))
        setting = ApplicationSetting.objects.get(household=self.household, key="alert_settings")
        self.assertEqual(setting.value["spending_low_balance_threshold"], "125.50")
        self.assertFalse(setting.value["bills_shortage_enabled"])
        event = AuditEvent.objects.get(record_type="budget.ApplicationSetting")
        self.assertNotIn("125.50", str(event.changes))

    def test_staff_can_create_user_without_logging_password(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_admin"),
            {
                "intent": "add_user",
                "user-username": "second-user",
                "user-email": "second@example.com",
                "user-first_name": "Second",
                "user-last_name": "User",
                "user-password1": "Strong-test-password-482!",
                "user-password2": "Strong-test-password-482!",
                "user-is_staff": "on",
            },
        )

        self.assertRedirects(response, reverse("application_admin"))
        created = get_user_model().objects.get(username="second-user")
        self.assertTrue(created.is_staff)
        self.assertTrue(created.check_password("Strong-test-password-482!"))
        event = AuditEvent.objects.get(record_type="auth.User", record_id=str(created.pk))
        self.assertNotIn("password", str(event.changes).lower())

    def test_staff_can_reset_member_password_without_logging_it(self):
        self.client.force_login(self.staff)
        new_password = "Fresh-member-password-482!"

        response = self.client.post(
            reverse("application_user_password", args=[self.member.pk]),
            {"new_password1": new_password, "new_password2": new_password},
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(new_password))
        event = AuditEvent.objects.get(record_type="auth.User", record_id=str(self.member.pk))
        self.assertEqual(event.changes, {"password_reset": True})
        self.assertNotIn(new_password, str(event.changes))

    def test_password_reset_uses_configured_password_validation(self):
        self.client.force_login(self.staff)
        old_password = self.member.password

        response = self.client.post(
            reverse("application_user_password", args=[self.member.pk]),
            {"new_password1": "short", "new_password2": "short"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This password is too short")
        self.member.refresh_from_db()
        self.assertEqual(self.member.password, old_password)

    def test_staff_cannot_reset_user_outside_active_profile(self):
        other_profile = Household.objects.create(name="Other budget")
        other_user = get_user_model().objects.create_user(
            username="other-user", password="test-password"
        )
        HouseholdMembership.objects.create(household=other_profile, user=other_user)
        self.client.force_login(self.staff)

        response = self.client.get(reverse("application_user_password", args=[other_user.pk]))

        self.assertEqual(response.status_code, 404)

    def test_standard_member_cannot_reset_passwords(self):
        self.client.force_login(self.member)

        response = self.client.get(reverse("application_user_password", args=[self.staff.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_mobile_navigation_includes_admin_only_for_staff(self):
        self.client.force_login(self.staff)
        staff_response = self.client.get(reverse("dashboard"))
        self.assertContains(
            staff_response,
            f'<a href="{reverse("application_admin")}">Admin</a>',
            html=True,
        )
        mobile_navigation = (
            staff_response.content.decode()
            .split('aria-label="Mobile primary"', 1)[1]
            .split("</nav>", 1)[0]
        )
        expected_labels = (
            "Home",
            "History",
            "Add＋",
            "Bills",
            "More",
            "Budgets",
            "Savings",
            "Outlook",
            "Admin",
        )
        positions = [mobile_navigation.index(f">{label}<") for label in expected_labels]
        self.assertEqual(positions, sorted(positions))

        self.client.force_login(self.member)
        member_response = self.client.get(reverse("dashboard"))
        self.assertNotContains(
            member_response,
            f'<a href="{reverse("application_admin")}">Admin</a>',
            html=True,
        )

    def test_staff_can_delete_user_after_exact_confirmation(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_user_delete", args=[self.member.pk]),
            {"confirmation": self.member.username},
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.assertFalse(get_user_model().objects.filter(pk=self.member.pk).exists())
        event = AuditEvent.objects.get(record_type="auth.User", record_id=str(self.member.pk))
        self.assertEqual(event.changes, {"user_deleted": True})

    def test_deleting_multi_profile_user_only_removes_active_access(self):
        other_profile = Household.objects.create(name="Other profile")
        HouseholdMembership.objects.create(household=other_profile, user=self.member)
        self.client.force_login(self.staff)

        self.client.post(
            reverse("application_user_delete", args=[self.member.pk]),
            {"confirmation": self.member.username},
        )

        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())
        self.assertFalse(
            HouseholdMembership.objects.filter(household=self.household, user=self.member).exists()
        )
        self.assertTrue(
            HouseholdMembership.objects.filter(household=other_profile, user=self.member).exists()
        )

    def test_admin_cannot_delete_self_or_skip_confirmation(self):
        self.client.force_login(self.staff)

        self.client.post(
            reverse("application_user_delete", args=[self.member.pk]),
            {"confirmation": "wrong-user"},
        )
        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())

        response = self.client.get(reverse("application_user_delete", args=[self.staff.pk]))
        self.assertRedirects(response, reverse("application_admin"))
        self.assertTrue(get_user_model().objects.filter(pk=self.staff.pk).exists())

    def test_staff_can_delete_empty_inactive_profile_and_preserve_audit(self):
        profile = Household.objects.create(name="Empty profile")
        AuditEvent.objects.create(
            household=profile,
            actor=self.staff,
            record_type="budget.Household",
            record_id=str(profile.pk),
            action=AuditEvent.Action.CREATE,
        )
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_profile_delete", args=[profile.pk]),
            {"confirmation": profile.name},
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.assertFalse(Household.objects.filter(pk=profile.pk).exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                household=None,
                record_type="budget.Household",
                record_id=str(profile.pk),
                action=AuditEvent.Action.CREATE,
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                household=self.household,
                record_type="budget.Household",
                record_id=str(profile.pk),
                action=AuditEvent.Action.DELETE,
            ).exists()
        )

    def test_active_or_financial_profile_cannot_be_deleted(self):
        self.client.force_login(self.staff)
        active_response = self.client.get(
            reverse("application_profile_delete", args=[self.household.pk])
        )
        self.assertRedirects(active_response, reverse("application_admin"))

        profile = Household.objects.create(name="Profile with history")
        account = Account.objects.create(household=profile, name="Spending", type="spending")
        category = Category.objects.create(
            household=profile, name="General", type=Category.Type.SPENDING
        )
        Expense.objects.create(
            household=profile,
            date="2026-08-01",
            amount="1.00",
            account=account,
            category=category,
        )
        response = self.client.post(
            reverse("application_profile_delete", args=[profile.pk]),
            {"confirmation": profile.name},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contains bills, pay periods, or transaction history")
        self.assertTrue(Household.objects.filter(pk=profile.pk).exists())

    def test_profile_with_only_a_debt_history_is_refused_not_crashed(self):
        # DebtBalance protects its debt (P8), so deleting such a profile must be refused
        # with the normal message instead of raising ProtectedError.
        from budget.models import Debt, DebtBalance

        self.client.force_login(self.staff)
        profile = Household.objects.create(name="Profile with a debt")
        debt = Debt.objects.create(
            household=profile, name="Car loan", apr="5.00", minimum_payment="100.00"
        )
        DebtBalance.objects.create(debt=debt, as_of="2026-08-01", balance="900.00")
        response = self.client.post(
            reverse("application_profile_delete", args=[profile.pk]),
            {"confirmation": profile.name},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contains bills, pay periods, or transaction history")
        self.assertTrue(DebtBalance.objects.filter(debt=debt).exists())

    def test_admin_page_displays_audit_events(self):
        AuditEvent.objects.create(
            household=self.household,
            actor=self.staff,
            record_type="RecurringBill",
            record_id="8",
            action="update",
            changes={"default_amount": "125.00"},
        )
        self.client.force_login(self.staff)

        response = self.client.get(reverse("application_admin"))

        self.assertContains(response, "Activity log")
        self.assertContains(response, "RecurringBill #8")

    def test_staff_can_create_profile(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_admin"),
            {
                "intent": "add_profile",
                "profile-name": "Second Household",
                "profile-timezone": "America/New_York",
            },
        )

        self.assertRedirects(response, reverse("application_admin"))
        profile = Household.objects.get(name="Second Household")
        self.assertTrue(
            HouseholdMembership.objects.filter(household=profile, user=self.staff).exists()
        )
        self.assertEqual(profile.account_set.count(), 3)

    def test_staff_can_grant_existing_user_access_to_profile(self):
        profile = Household.objects.create(name="Third Household")
        casey = get_user_model().objects.create_user(
            username="casey", password="Casey-test-password-482!"
        )
        HouseholdMembership.objects.create(household=profile, user=self.staff)
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_profile_access", args=[profile.pk]),
            {"users": [self.staff.pk, casey.pk]},
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.assertTrue(HouseholdMembership.objects.filter(household=profile, user=casey).exists())
        event = AuditEvent.objects.get(
            household=profile,
            record_type="budget.Household",
            record_id=str(profile.pk),
            action=AuditEvent.Action.UPDATE,
        )
        self.assertEqual(event.changes["access_granted"], ["casey"])
        self.assertNotIn("password", str(event.changes).lower())

        self.client.logout()
        login_response = self.client.post(
            reverse("login"),
            {
                "profile": profile.pk,
                "username": "casey",
                "password": "Casey-test-password-482!",
            },
        )
        self.assertRedirects(login_response, reverse("dashboard"))

    def test_staff_can_revoke_profile_access_without_deleting_user(self):
        profile = Household.objects.create(name="Shared Budget")
        HouseholdMembership.objects.create(household=profile, user=self.staff)
        HouseholdMembership.objects.create(household=profile, user=self.member)
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("application_profile_access", args=[profile.pk]),
            {"users": [self.staff.pk]},
        )

        self.assertRedirects(response, reverse("application_admin"))
        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())
        self.assertFalse(
            HouseholdMembership.objects.filter(household=profile, user=self.member).exists()
        )
        event = AuditEvent.objects.get(household=profile, action=AuditEvent.Action.UPDATE)
        self.assertEqual(event.changes["access_revoked"], ["member"])

    def test_profile_access_preserves_active_user_and_an_administrator(self):
        other_admin = get_user_model().objects.create_user(
            username="other-admin", password="test-password", is_staff=True
        )
        HouseholdMembership.objects.create(household=self.household, user=other_admin)
        self.client.force_login(self.staff)
        url = reverse("application_profile_access", args=[self.household.pk])

        remove_self = self.client.post(url, {"users": [other_admin.pk, self.member.pk]})
        self.assertEqual(remove_self.status_code, 200)
        self.assertContains(remove_self, "cannot remove your own access")

        inactive_profile = Household.objects.create(name="Inactive profile")
        HouseholdMembership.objects.create(household=inactive_profile, user=other_admin)
        HouseholdMembership.objects.create(household=inactive_profile, user=self.member)
        remove_all_admins = self.client.post(
            reverse("application_profile_access", args=[inactive_profile.pk]),
            {"users": [self.member.pk]},
        )
        self.assertEqual(remove_all_admins.status_code, 200)
        self.assertContains(remove_all_admins, "At least one administrator")
        self.assertTrue(
            HouseholdMembership.objects.filter(household=self.household, user=self.staff).exists()
        )

    def test_standard_member_cannot_manage_profile_access(self):
        self.client.force_login(self.member)

        response = self.client.get(reverse("application_profile_access", args=[self.household.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class ProfileLoginTests(TestCase):
    def setUp(self):
        self.first = Household.objects.create(name="Test Household")
        self.second = Household.objects.create(name="Second Household")
        self.user = get_user_model().objects.create_user(
            username="first-user", password="test-password"
        )
        HouseholdMembership.objects.create(household=self.first, user=self.user)

    def test_login_requires_membership_in_selected_profile(self):
        response = self.client.post(
            reverse("login"),
            {
                "profile": self.second.pk,
                "username": "first-user",
                "password": "test-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have access to the selected profile")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_stores_selected_profile(self):
        response = self.client.post(
            reverse("login"),
            {
                "profile": self.first.pk,
                "username": "first-user",
                "password": "test-password",
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.session["active_household_id"], self.first.pk)

    def test_superuser_gains_access_to_selected_profile_at_login(self):
        admin = get_user_model().objects.create_superuser(
            username="new-admin", password="test-password"
        )

        response = self.client.post(
            reverse("login"),
            {
                "profile": self.second.pk,
                "username": "new-admin",
                "password": "test-password",
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(
            HouseholdMembership.objects.filter(household=self.second, user=admin).exists()
        )
