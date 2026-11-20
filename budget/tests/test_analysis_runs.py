import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from budget.models import Household, HouseholdMembership, ReportAnalysisRun
from budget.report_analysis import (
    OpenAICompatibleReportAnalyzer,
    ReportAnalysisError,
    parse_report_analysis,
)
from budget.tests.test_reports import valid_analysis

RANGE = {"start": "2026-03-01", "end": "2026-03-31"}


@pytest.fixture
def owner(client, db):
    user = get_user_model().objects.create_user("analysis-user")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    return client, home


def test_generate_starts_a_background_run_and_the_page_polls(owner):
    client, home = owner
    started = []
    with patch("budget.analysis_jobs.spawn", lambda target, *args: started.append(args)):
        response = client.post(reverse("reports_analysis_start"), RANGE)
        again = client.post(reverse("reports_analysis_start"), RANGE)
    run = ReportAnalysisRun.objects.get()
    assert response.status_code == 302 and f"analysis={run.pk}" in response["Location"]
    assert f"analysis={run.pk}" in again["Location"] and len(started) == 1  # no duplicate run
    page = client.get(response["Location"]).content.decode()
    assert "Analyzing" in page and "data-analysis-status-url" in page
    assert "http-equiv" not in page


def test_finished_failed_and_stale_runs(owner):
    client, home = owner
    analyzer = Mock()
    analyzer.analyze.return_value = parse_report_analysis(valid_analysis(), model="m")
    with (
        patch("budget.analysis_jobs.configured_report_analyzer", return_value=analyzer),
        patch("budget.analysis_jobs.spawn", lambda target, *args: target(*args)),
    ):
        page = client.post(reverse("reports_analysis_start"), RANGE, follow=True).content.decode()
    assert "Overall spending remained manageable" in page and "refresh" not in page
    assert "Run the analysis again" in page

    analyzer.analyze.side_effect = ReportAnalysisError(
        "The local report model could not analyze this period."
    )
    with (
        patch("budget.analysis_jobs.configured_report_analyzer", return_value=analyzer),
        patch("budget.analysis_jobs.spawn", lambda target, *args: target(*args)),
    ):
        page = client.post(
            reverse("reports_analysis_start"),
            {"start": "2026-01-01", "end": "2026-01-31"},
            follow=True,
        )
    assert b"could not analyze this period" in page.content

    stale = ReportAnalysisRun.objects.create(household=home, start="2026-02-01", end="2026-02-28")
    ReportAnalysisRun.objects.filter(pk=stale.pk).update(
        created_at=stale.created_at - timedelta(minutes=11)
    )
    page = client.get(reverse("reports"), {"analysis": stale.pk}).content.decode()
    assert "took too long" in page


def test_other_profile_cannot_read_a_run(owner, django_user_model):
    client, home = owner
    other = Household.objects.create()
    run = ReportAnalysisRun.objects.create(household=other, start="2026-01-01", end="2026-01-31")
    assert client.get(reverse("reports"), {"analysis": run.pk}).status_code == 404


@pytest.mark.parametrize("disable", [True, False])
def test_thinking_switch_is_sent_only_when_enabled(disable):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(valid_analysis())}}]}
            ).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()

    with (
        override_settings(REPORT_LLM_DISABLE_THINKING=disable, REPORT_LLM_MAX_TOKENS=4000),
        patch("urllib.request.urlopen", fake_urlopen),
        patch("budget.report_analysis.enforce_report_grounding", lambda a, s: a),
    ):
        OpenAICompatibleReportAnalyzer("http://x/v1/chat/completions", "test-model").analyze({})
    assert captured["body"]["max_tokens"] == 4000
    assert ("chat_template_kwargs" in captured["body"]) is disable
