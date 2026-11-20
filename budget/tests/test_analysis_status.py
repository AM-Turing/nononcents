from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from budget.models import Household, HouseholdMembership, ReportAnalysisRun


@pytest.fixture
def member(client, db):
    user = get_user_model().objects.create_user("status-member")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client, home


def _run(home, status=ReportAnalysisRun.Status.RUNNING):
    return ReportAnalysisRun.objects.create(
        household=home, start=date(2026, 1, 1), end=date(2026, 1, 31), status=status
    )


def test_status_running(member):
    client, home = member
    run = _run(home)
    response = client.get(reverse("reports_analysis_status", args=[run.pk]))
    assert response.json() == {"status": "running", "running": True}


def test_status_done_has_no_analysis_text(member):
    client, home = member
    run = _run(home, ReportAnalysisRun.Status.DONE)
    run.result = {"overview": "SECRET TEXT"}
    run.save()
    response = client.get(reverse("reports_analysis_status", args=[run.pk]))
    assert response.json() == {"status": "done", "running": False}
    assert b"SECRET" not in response.content


def test_status_stale_run_is_failed(member):
    client, home = member
    run = _run(home)
    ReportAnalysisRun.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - timedelta(minutes=11)
    )
    response = client.get(reverse("reports_analysis_status", args=[run.pk]))
    assert response.json() == {"status": "failed", "running": False}


def test_status_other_household_404(member):
    client, _home = member
    run = _run(Household.objects.create())
    assert client.get(reverse("reports_analysis_status", args=[run.pk])).status_code == 404


def test_status_anonymous_redirects(client, db):
    run = _run(Household.objects.create())
    response = client.get(reverse("reports_analysis_status", args=[run.pk]))
    assert response.status_code == 302
    assert "login" in response["Location"]


def test_status_post_not_allowed(member):
    client, home = member
    run = _run(home)
    assert client.post(reverse("reports_analysis_status", args=[run.pk])).status_code == 405


def test_running_page_has_no_meta_refresh_and_has_fallback(member):
    client, home = member
    run = _run(home)
    response = client.get(f"{reverse('reports')}?analysis={run.pk}")
    html = response.content.decode()
    assert 'http-equiv="refresh"' not in html
    assert "Check again" in html
    assert f'data-analysis-status-url="/reports/analysis/{run.pk}/status/"' in html
    assert "data-analysis-live" in html
