from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse

from budget.models import Household, HouseholdMembership, ReportAnalysisRun


@pytest.fixture
def member(client, db):
    user = get_user_model().objects.create_user("access-member")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client, home


@pytest.fixture
def staff(client, db):
    user = get_user_model().objects.create_user("access-staff", is_staff=True)
    client.force_login(user)
    return client


def _query(response):
    return parse_qs(urlsplit(response["Location"]).query)


# F3: the Django admin sign-in page is replaced by the app's sign-in page.


def test_anonymous_admin_login_goes_to_app_sign_in_with_next(client, db):
    response = client.get("/admin/login/", {"next": "/admin/budget/"})
    assert response.status_code == 302
    assert urlsplit(response["Location"]).path == reverse("login")
    assert _query(response)["next"] == ["/admin/budget/"]


def test_staff_admin_login_redirects_to_next(staff):
    response = staff.get("/admin/login/", {"next": "/admin/budget/"})
    assert response.status_code == 302
    assert response["Location"] == "/admin/budget/"


def test_signed_in_non_staff_admin_login_is_forbidden(member):
    client, _home = member
    response = client.get("/admin/login/", {"next": "/admin/budget/"})
    assert response.status_code == 403


def test_off_site_next_is_replaced_by_admin_index_for_anonymous(client, db):
    response = client.get("/admin/login/", {"next": "https://evil.example/"})
    assert response.status_code == 302
    assert urlsplit(response["Location"]).path == reverse("login")
    assert _query(response)["next"] == [reverse("admin:index")]


def test_off_site_next_is_replaced_by_admin_index_for_staff(staff):
    response = staff.get("/admin/login/", {"next": "https://evil.example/"})
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:index")


def test_anonymous_admin_index_ends_at_app_sign_in(client, db):
    response = client.get("/admin/", follow=True)
    assert response.redirect_chain
    assert urlsplit(response.redirect_chain[-1][0]).path == reverse("login")
    assert response.request["PATH_INFO"] == reverse("login")


# F4: AI analysis starts only by POST.


def test_get_on_analysis_start_is_rejected_and_creates_no_run(member):
    client, _home = member
    response = client.get(reverse("reports_analysis_start"))
    assert response.status_code == 405
    assert not ReportAnalysisRun.objects.exists()


def test_reports_generate_query_does_not_start_an_analysis(member):
    client, _home = member
    response = client.get(reverse("reports") + "?generate=1")
    assert response.status_code == 200
    assert not ReportAnalysisRun.objects.exists()


def test_second_range_reuses_the_running_run(member):
    client, home = member
    started = []
    with patch("budget.analysis_jobs.spawn", lambda target, *args: started.append(args)):
        first = client.post(
            reverse("reports_analysis_start"),
            {"start": "2026-03-01", "end": "2026-03-31"},
        )
        second = client.post(
            reverse("reports_analysis_start"),
            {"start": "2026-04-01", "end": "2026-04-30"},
        )
    run = ReportAnalysisRun.objects.get(household=home)
    assert len(started) == 1
    assert ReportAnalysisRun.objects.count() == 1
    assert run.status == ReportAnalysisRun.Status.RUNNING
    assert second.status_code == 302
    assert f"analysis={run.pk}" in second["Location"]
    assert _query(second)["start"] == [f"{run.start:%Y-%m-%d}"]
    assert _query(second)["end"] == [f"{run.end:%Y-%m-%d}"]
    assert f"start={run.start:%Y-%m-%d}" in first["Location"]
    messages = [str(m) for m in get_messages(second.wsgi_request)]
    assert any("another date range" in m for m in messages)
