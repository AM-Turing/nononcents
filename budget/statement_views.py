"""Monthly statement check screens. Matching rules live in ``budget.statements``."""

import hashlib
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Account, StatementImport, StatementLine
from .services import audit
from .statement_pdf import MAX_PDF_BYTES, StatementParseError, parse_statement_pdf
from .statements import (
    ParsedLine,
    ParsedSection,
    build_review,
    confirm_all_proposed,
    confirm_match,
    dismiss_line,
    reopen_line,
    stage_statement,
)
from .views import common, household


def _statement(request, pk):
    return get_object_or_404(
        StatementImport.objects.select_related("account"), pk=pk, household=household(request)
    )


@login_required
def statements(request):
    home = household(request)
    rows = StatementImport.objects.filter(household=home).annotate(
        line_count=Count("lines"),
        pending_count=Count("lines", filter=Q(lines__status=StatementLine.Status.PENDING)),
    )
    return render(request, "budget/statements.html", {**common(request), "statements": rows})


@login_required
def statement_review(request, pk):
    statement = _statement(request, pk)
    return render(
        request,
        "budget/statement_review.html",
        {**common(request), "statement": statement, "review": build_review(statement)},
    )


def _back(statement, line_id=None):
    anchor = f"#line-{line_id}" if line_id else ""
    return redirect(f"/statements/{statement.pk}/{anchor}")


def _run(request, action):
    try:
        action()
        return True
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return False


@login_required
@require_POST
def statement_line_action(request, pk):
    statement = _statement(request, pk)
    home = statement.household
    raw = request.POST.get("line", "")
    line_id = int(raw) if raw.isdigit() else 0
    action = request.POST.get("action")
    if action == "match":
        if _run(
            request,
            lambda: confirm_match(
                household=home, actor=request.user, line_id=line_id, key=request.POST.get("key", "")
            ),
        ):
            messages.success(request, "Marked as the same purchase and cleared.")
    elif action == "dismiss":
        if _run(request, lambda: dismiss_line(household=home, actor=request.user, line_id=line_id)):
            messages.success(request, "Statement line dismissed.")
    elif action == "reopen":
        if _run(request, lambda: reopen_line(household=home, actor=request.user, line_id=line_id)):
            messages.success(request, "Statement line moved back to review.")
    elif action == "match_all":
        count = 0

        def run_all():
            nonlocal count
            count = confirm_all_proposed(household=home, actor=request.user, statement=statement)

        if _run(request, run_all):
            messages.success(
                request, f"Confirmed {count} proposed match{'es' if count != 1 else ''}."
            )
        return _back(statement)
    else:
        messages.error(request, "Unknown action.")
    return _back(statement, line_id if action == "reopen" else None)


@login_required
@require_POST
def statement_delete(request, pk):
    statement = _statement(request, pk)
    with transaction.atomic():
        audit(
            statement.household,
            request.user,
            statement,
            "delete",
            {"account": statement.account.name, "lines": statement.lines.count()},
        )
        statement.delete()
    messages.success(
        request, "Statement removed. Entries keep their cleared marks; nothing else changed."
    )
    return redirect("statements")


UPLOAD_SESSION_KEY = "statement_upload"


def _section_to_json(section: ParsedSection) -> dict:
    return {
        "label": section.account_label,
        "suffix": section.account_suffix,
        "start": section.period_start.isoformat(),
        "end": section.period_end.isoformat(),
        "opening": str(section.opening_balance),
        "closing": str(section.closing_balance),
        "lines": [[ln.date.isoformat(), str(ln.amount), ln.description] for ln in section.lines],
    }


def _section_from_json(data: dict) -> ParsedSection:
    return ParsedSection(
        account_label=data["label"],
        account_suffix=data["suffix"],
        period_start=date.fromisoformat(data["start"]),
        period_end=date.fromisoformat(data["end"]),
        opening_balance=Decimal(data["opening"]),
        closing_balance=Decimal(data["closing"]),
        lines=[ParsedLine(date.fromisoformat(d), Decimal(a), t) for d, a, t in data["lines"]],
    )


def _stage_all(request, home, sha, pairs):
    """Stage (account, section) pairs; one message summarising the result."""
    created = repeated = 0
    with transaction.atomic():
        for account, section in pairs:
            result = stage_statement(
                household=home,
                actor=request.user,
                account=account,
                section=section,
                file_sha256=sha,
            )
            created += result.created
            repeated += result.already_imported
    names = ", ".join(account.name for account, _ in pairs)
    note = (
        f" {repeated} line{'s' if repeated != 1 else ''} were already imported." if repeated else ""
    )
    plural = "s" if created != 1 else ""
    messages.success(
        request, f"Statement read for {names}: {created} new line{plural} to check.{note}"
    )


def _accounts(home):
    return list(Account.objects.filter(household=home, archived_at=None))


@login_required
def statement_upload(request):
    home = household(request)
    context = {**common(request), "max_mb": MAX_PDF_BYTES // (1024 * 1024)}
    if request.method != "POST":
        request.session.pop(UPLOAD_SESSION_KEY, None)
        return render(request, "budget/statement_upload.html", context)
    request.session.pop(UPLOAD_SESSION_KEY, None)  # a new attempt replaces any abandoned one
    upload = request.FILES.get("statement")
    if upload is None:
        context["error"] = "Choose the statement PDF to upload."
        return render(request, "budget/statement_upload.html", context)
    if upload.size > MAX_PDF_BYTES:
        context["error"] = f"The file is larger than {context['max_mb']} MB."
        return render(request, "budget/statement_upload.html", context)
    data = upload.read()
    try:
        sections = parse_statement_pdf(data)
    except StatementParseError as exc:
        context["error"] = str(exc)
        return render(request, "budget/statement_upload.html", context)
    sha = hashlib.sha256(data).hexdigest()
    accounts = _accounts(home)
    by_suffix = {a.statement_suffix: a for a in accounts if a.statement_suffix}
    if all(section.account_suffix in by_suffix for section in sections):
        try:
            _stage_all(request, home, sha, [(by_suffix[s.account_suffix], s) for s in sections])
        except ValidationError as exc:
            context["error"] = " ".join(exc.messages)
            return render(request, "budget/statement_upload.html", context)
        return redirect("statements")
    request.session[UPLOAD_SESSION_KEY] = {
        "household": home.pk,
        "sha": sha,
        "sections": [_section_to_json(s) for s in sections],
    }
    return redirect("statement_map")


def _guess(section: ParsedSection, accounts):
    """First-time suggestion: a bank savings account goes to the app's Savings account."""
    if "saving" in section.account_label.casefold():
        return next((a for a in accounts if a.type == Account.Type.SAVINGS), None)
    return None


@login_required
def statement_map(request):
    home = household(request)
    pending = request.session.get(UPLOAD_SESSION_KEY)
    if not pending or pending.get("household") != home.pk:
        messages.info(request, "Upload the statement again to continue.")
        return redirect("statement_upload")
    sections = [_section_from_json(item) for item in pending["sections"]]
    accounts = _accounts(home)
    by_pk = {str(a.pk): a for a in accounts}
    by_suffix = {a.statement_suffix: a for a in accounts if a.statement_suffix}
    rows = [
        {
            "index": index,
            "section": section,
            "account": by_suffix.get(section.account_suffix) or _guess(section, accounts),
        }
        for index, section in enumerate(sections)
    ]
    if request.method == "POST" and request.POST.get("cancel"):
        request.session.pop(UPLOAD_SESSION_KEY, None)
        messages.info(request, "Nothing was imported.")
        return redirect("statements")
    if request.method == "POST":
        pairs, used = [], set()
        for row in rows:
            choice = request.POST.get(f"account_{row['index']}", "")
            if choice == "skip":
                continue
            account = by_pk.get(choice)
            if account is None or account.pk in used:
                messages.error(request, "Choose a different app account for each bank account.")
                return redirect("statement_map")
            used.add(account.pk)
            pairs.append((account, row["section"]))
        if not pairs:
            request.session.pop(UPLOAD_SESSION_KEY, None)
            messages.info(request, "Nothing was imported.")
            return redirect("statements")
        try:
            with transaction.atomic():
                for account, section in pairs:
                    if account.statement_suffix != section.account_suffix:
                        Account.objects.filter(
                            household=home, statement_suffix=section.account_suffix
                        ).update(statement_suffix="")
                        account.statement_suffix = section.account_suffix
                        account.save(update_fields=["statement_suffix"])
                        audit(home, request.user, account, "update", {"statement_suffix": "set"})
                _stage_all(request, home, pending["sha"], pairs)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("statement_map")
        request.session.pop(UPLOAD_SESSION_KEY, None)
        return redirect("statements")
    return render(
        request,
        "budget/statement_map.html",
        {**common(request), "rows": rows, "accounts": accounts},
    )
