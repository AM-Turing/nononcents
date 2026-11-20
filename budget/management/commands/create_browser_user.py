import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from budget.models import Category, Household, HouseholdMembership, PayPeriod
from budget.pay_periods import create_period_budget
from budget.services import expected_deposit_date
from budget.views import generate_future_pay_periods, initialize_profile


class Command(BaseCommand):
    help = "Create the isolated Playwright user when BROWSER_TEST_MODE is enabled"

    def handle(self, *args, **options):
        if os.environ.get("BROWSER_TEST_MODE") != "true":
            raise CommandError("This command is restricted to BROWSER_TEST_MODE=true.")
        user, _ = get_user_model().objects.get_or_create(username="browser-admin")
        user.is_staff = True
        user.is_superuser = True
        user.set_password("browser-test-password")
        user.save()
        # A second, empty profile so tests can check the profile chooser and access management.
        second, created = Household.objects.get_or_create(name="Second Household")
        if created:
            initialize_profile(second)
            for order, name in enumerate(("Groceries", "Entertainment"), start=10):
                Category.objects.create(
                    household=second, name=name, type=Category.Type.SPENDING, display_order=order
                )
            add_pay_periods(second)
        for profile in Household.objects.all():
            HouseholdMembership.objects.get_or_create(household=profile, user=user)
        self.stdout.write("Browser test user ready")


def add_pay_periods(home):
    """Semi-monthly pay periods from about three months ago to four months ahead."""
    today = timezone.localdate()
    first_payday = (today.replace(day=1) - timedelta(days=90)).replace(day=1)
    second_payday = first_payday.replace(day=15)
    first = PayPeriod.objects.create(
        household=home,
        nominal_payday=first_payday,
        expected_deposit_date=expected_deposit_date(first_payday),
        start_date=expected_deposit_date(first_payday),
        end_date=expected_deposit_date(second_payday) - timedelta(days=1),
    )
    create_period_budget(first)
    generate_future_pay_periods(home, today + timedelta(days=120))
