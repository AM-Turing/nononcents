"""First run: an empty database, one administrator, no profiles."""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from budget.models import Account, Household, HouseholdMembership


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser(
        "first-admin", password="first-admin-password-91"
    )


@pytest.mark.django_db
def test_fresh_database_has_no_profiles_or_seed_data():
    assert not Household.objects.exists()
    assert not Account.objects.exists()
    assert not get_user_model().objects.exists()


@pytest.mark.django_db
def test_login_page_has_no_profile_chooser_on_first_run(client):
    page = client.get(reverse("login")).content.decode()
    assert "First time here?" in page
    assert 'name="profile"' not in page


@pytest.mark.django_db
def test_administrator_signs_in_creates_first_profile_and_lands_home(client, admin_user):
    response = client.post(
        reverse("login"), {"username": "first-admin", "password": "first-admin-password-91"}
    )
    assert response.status_code == 302
    # Every page sends the administrator to the one-time setup until a profile exists.
    assert client.get(reverse("dashboard")).headers["Location"] == reverse("first_profile")
    assert "Welcome to NoNonCents" in client.get(reverse("first_profile")).content.decode()

    response = client.post(reverse("first_profile"), {"profile-name": "Our Household"})
    assert response.status_code == 302
    home = Household.objects.get()
    assert home.name == "Our Household"
    assert HouseholdMembership.objects.filter(household=home, user=admin_user).exists()
    assert set(Account.objects.filter(household=home).values_list("name", flat=True)) == {
        "Spending",
        "Bills",
        "Savings",
    }
    assert client.get(reverse("dashboard")).status_code == 200
    # The setup page is for the first profile only.
    assert client.get(reverse("first_profile")).status_code == 302


@pytest.mark.django_db
def test_ordinary_user_cannot_sign_in_before_a_profile_exists(client):
    get_user_model().objects.create_user("member", password="member-password-4821")
    response = client.post(
        reverse("login"), {"username": "member", "password": "member-password-4821"}
    )
    assert response.status_code == 200
    assert "administrator must sign in first" in response.content.decode()


@pytest.mark.django_db
def test_demo_profile_loads_with_yes_flag_and_leaves_other_profiles(monkeypatch):
    monkeypatch.delenv("BROWSER_TEST_MODE", raising=False)
    mine = Household.objects.create(name="Mine")
    call_command("seed_demo_profile", "--yes")
    assert set(Household.objects.values_list("name", flat=True)) == {"Mine", "Demo Household"}
    assert mine.pk
