"""Run Deep pattern analysis in the background.

The local model needs about a minute (longer when it is busy), more than a web request
should wait behind nginx. Generating starts a ``ReportAnalysisRun`` and a daemon thread;
the Outlook page refreshes until the run is done or failed. A run still "running" after
``STALE_AFTER`` is shown as failed (for example after a restart killed the thread).
"""

import logging
import threading
from datetime import date, timedelta

from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from .models import Household, ReportAnalysisRun
from .report_analysis import ReportAnalysisError, configured_report_analyzer

logger = logging.getLogger("budget.analysis")
STALE_AFTER = timedelta(minutes=10)


def _in_thread(target, *args) -> None:
    close_old_connections()
    try:
        target(*args)
    finally:
        connection.close()  # a thread's connection is never reused


def spawn(target, *args) -> None:
    """Run ``target`` in a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=_in_thread, args=(target, *args), daemon=True).start()


def start_run(household, user, start: date, end: date) -> ReportAnalysisRun:
    """Start an analysis for the range, or return the one already running.

    One run at a time per budget profile: each run holds a thread, a database connection
    and a model call of up to five minutes. The profile row is locked so two requests
    arriving together cannot both start one.
    """
    recent = timezone.now() - STALE_AFTER
    with transaction.atomic():
        Household.objects.select_for_update().get(pk=household.pk)
        running = (
            ReportAnalysisRun.objects.filter(
                household=household,
                status=ReportAnalysisRun.Status.RUNNING,
                created_at__gte=recent,
            )
            .order_by("-created_at")
            .first()
        )
        if running:
            return running
        run = ReportAnalysisRun.objects.create(
            household=household, start=start, end=end, created_by=user
        )
    # Started after the transaction commits, so the thread can read the run.
    spawn(_work, run.pk)
    return run


def _work(run_id: int) -> None:
    try:
        _work_inner(run_id)
    except Exception:  # a database error before or after the analysis; never log report data
        logger.exception("Deep pattern analysis could not be saved (run %s)", run_id)
        _mark_failed(run_id)


def _mark_failed(run_id: int) -> None:
    """Best effort: show the run as failed now rather than 'running' for ten minutes."""
    try:
        ReportAnalysisRun.objects.filter(pk=run_id, status=ReportAnalysisRun.Status.RUNNING).update(
            status=ReportAnalysisRun.Status.FAILED,
            error="The analysis failed unexpectedly. Try again later.",
            finished_at=timezone.now(),
        )
    except Exception:
        logger.exception("Could not mark analysis run %s as failed", run_id)


def _work_inner(run_id: int) -> None:
    from .outlook import outlook_data
    from .views import report_analysis_payload, report_data

    run = ReportAnalysisRun.objects.select_related("household").get(pk=run_id)
    today = timezone.localdate()
    try:
        data = outlook_data(run.household, run.start, run.end, today)
        # The narrative sees only figures through the single as-of cutoff.
        as_of = max(min(today, run.end), run.start)
        payload = report_analysis_payload(report_data(run.household, run.start, as_of), data)
        analysis = configured_report_analyzer().analyze(payload)
        run.result = analysis.json_value()
        run.status = ReportAnalysisRun.Status.DONE
    except ReportAnalysisError as exc:
        run.status, run.error = ReportAnalysisRun.Status.FAILED, str(exc)[:255]
    except Exception:  # never let a thread die silently; never log report data
        logger.exception("Deep pattern analysis failed unexpectedly (run %s)", run_id)
        run.status = ReportAnalysisRun.Status.FAILED
        run.error = "The analysis failed unexpectedly. Try again later."
    run.finished_at = timezone.now()
    run.save(update_fields=["result", "status", "error", "finished_at"])


def run_state(run: ReportAnalysisRun) -> str:
    """'running', 'done' or 'failed' (a run running too long counts as failed)."""
    if run.status == ReportAnalysisRun.Status.RUNNING and (
        timezone.now() - run.created_at > STALE_AFTER
    ):
        return ReportAnalysisRun.Status.FAILED
    return run.status
