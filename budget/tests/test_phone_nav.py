import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from budget.models import Household, HouseholdMembership


def mobile_nav(response):
    html = response.content.decode()
    return html.split('aria-label="Mobile primary"', 1)[1].split("</nav>", 1)[0]


class PhoneNavTests(TestCase):
    def setUp(self):
        household = Household.objects.create()
        self.staff = get_user_model().objects.create_user("boss", password="pw", is_staff=True)
        self.member = get_user_model().objects.create_user("kid", password="pw")
        for user in (self.staff, self.member):
            HouseholdMembership.objects.create(household=household, user=user)

    def test_four_items_plus_more(self):
        self.client.force_login(self.member)
        nav = mobile_nav(self.client.get(reverse("dashboard")))
        top_level = re.sub(r"<details.*?</details>", "<details/>", nav, flags=re.S)
        links = re.findall(r"<a [^>]*>(.*?)</a>", top_level)
        self.assertEqual(links, ["Home", "History", "Add＋", "Bills"])
        self.assertIn(">More</summary>", nav)
        more = nav.split("<details", 1)[1]
        for name in ("budgets", "savings", "reports", "accounts"):
            self.assertIn(f'href="{reverse(name)}"', more)

    def test_admin_only_in_more_for_staff(self):
        self.client.force_login(self.staff)
        nav = mobile_nav(self.client.get(reverse("dashboard")))
        self.assertIn(reverse("application_admin"), nav.split("<details", 1)[1])
        self.assertNotIn(reverse("application_admin"), nav.split("<details", 1)[0])
        self.client.force_login(self.member)
        self.assertNotIn(
            reverse("application_admin"), mobile_nav(self.client.get(reverse("dashboard")))
        )

    def test_aria_current(self):
        self.client.force_login(self.member)
        nav = mobile_nav(self.client.get(reverse("bills")))
        self.assertRegex(nav, rf'<a href="{reverse("bills")}" aria-current="page">')
        self.assertEqual(nav.count('aria-current="page"'), 1)
        nav = mobile_nav(self.client.get(reverse("savings")))
        self.assertRegex(nav, r'<summary[^>]*aria-current="page"')

    def test_reports_css_does_not_hide_outlook_content(self):
        css = Path(finders.find("budget/reports.css")).read_text()
        for selector in (
            ".report-filters",
            ".pagehead .page-actions",
            ".outlook-settings",
            ".deep-analysis-card .button",
        ):
            for block in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
                if selector in block[0]:
                    self.assertNotIn("display: none", block[1])
        self.assertNotIn("display: none", css)
