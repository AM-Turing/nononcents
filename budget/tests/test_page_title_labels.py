import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budget.models import Household, HouseholdMembership

PAGEHEAD = re.compile(r'<header class="pagehead[^"]*">(.*?)</header>', re.S)


class PageTitleLabelTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create()
        self.user = get_user_model().objects.create_user(
            username="admin", password="test-password", is_staff=True, is_superuser=True
        )
        HouseholdMembership.objects.create(household=self.household, user=self.user)
        self.client.force_login(self.user)

    def test_no_eyebrow_label_above_page_titles(self):
        names = [
            "dashboard",
            "transactions",
            "transaction_add",
            "budgets",
            "bills",
            "savings",
            "reports",
            "habits",
            "categories",
            "accounts",
            "debts",
            "pay_periods",
            "statements",
            "application_admin",
            "docs",
        ]
        for name in names:
            with self.subTest(page=name):
                response = self.client.get(reverse(name), follow=True)
                self.assertEqual(response.status_code, 200)
                heads = PAGEHEAD.findall(response.content.decode())
                self.assertTrue(heads, "page has no pagehead")
                for head in heads:
                    self.assertNotIn('class="eyebrow"', head)

    def test_help_chapter_picker_has_no_guide_label(self):
        response = self.client.get(reverse("docs"), follow=True)
        html = response.content.decode()
        summary = re.search(r"<summary>(.*?)</summary>", html, re.S).group(1)
        self.assertNotIn("Guide", summary)
        self.assertIn("docs-current", summary)
