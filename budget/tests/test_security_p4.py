"""P4 security fixes: CSP, CSV injection, statement PDF limits, guide images, deploy files."""

import csv
import io
import re
from datetime import date
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget import guide
from budget.csp import POLICY
from budget.models import (
    Account,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Refund,
)
from budget.statement_pdf import MAX_PAGES, StatementParseError, parse_statement_pdf
from budget.views import csv_text

ROOT = Path(settings.BASE_DIR)


@pytest.fixture
def signed_in(client, db):
    user = get_user_model().objects.create_user("p4-user", password="a-secure-password-123")
    home = Household.objects.create(name="P4 home")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    return client, home, user


# ---- Content-Security-Policy -------------------------------------------------------------


def test_every_response_carries_the_policy(client, signed_in, db):
    anonymous = client.__class__().get(reverse("login"))
    assert anonymous["Content-Security-Policy"] == POLICY
    signed = client.get(reverse("dashboard"))
    assert signed["Content-Security-Policy"] == POLICY
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in POLICY
    assert "unsafe-inline" not in POLICY.split("style-src-attr")[0]
    assert "unsafe-eval" not in POLICY


def test_templates_have_no_inline_scripts_or_event_handlers():
    """The policy forbids both, so a new one would silently break a page."""
    found = []
    for folder in (ROOT / "templates", ROOT / "budget" / "templates"):
        for path in folder.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for tag in re.findall(r"<script\b[^>]*>", text):
                if "src=" not in tag and "application/json" not in tag:
                    found.append(f"{path.name}: {tag}")
            for handler in re.findall(r"\son[a-z]+\s*=", text):
                found.append(f"{path.name}: {handler.strip()}")
            if re.search(r"<style\b", text):
                found.append(f"{path.name}: <style> block")
    assert found == []


def test_theme_script_is_a_blocking_script_in_the_head(client, db):
    page = client.get(reverse("login")).content.decode()
    head = page.split("</head>")[0]
    tag = re.search(r"<script[^>]*theme-init[^>]*>", head)
    assert tag, "theme script must load in <head>"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0)


# ---- CSV formula injection ---------------------------------------------------------------


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_text_prefixes_formula_starters(lead):
    assert csv_text(f"{lead}SUM(A1)") == f"'{lead}SUM(A1)"


def test_csv_text_leaves_ordinary_text_alone():
    assert csv_text("Groceries") == "Groceries"
    assert csv_text("Spending") == "Spending"


def test_outlook_csv_prefixes_text_but_keeps_negative_amounts_numeric(signed_in):
    client, home, user = signed_in
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    evil = Category.objects.create(household=home, name="=HYPERLINK(1)", type="spending")
    credit = Category.objects.create(household=home, name="-Credits", type="spending")
    Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 3),
        amount="10.00",
        account=spending,
        category=evil,
    )
    expense = Expense.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 3),
        amount="2.00",
        account=spending,
        category=credit,
    )
    Refund.objects.create(
        household=home,
        created_by=user,
        date=date(2026, 8, 4),
        amount="7.00",
        account=spending,
        category=credit,
        original_expense=expense,
    )
    response = client.get(
        reverse("reports"), {"start": "2026-08-01", "end": "2026-08-31", "format": "csv"}
    )
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    by_name = {row[0]: row for row in rows if len(row) == 4 and row[0].lstrip("'")[:1] in "=-"}
    # Text cells are defused; no cell in the file starts with a formula character as text.
    assert "'=HYPERLINK(1)" in by_name
    assert "'-Credits" in by_name
    # Refund 7.00 against 2.00 spent: net -5.00 stays a plain negative number.
    assert by_name["'-Credits"][2] == "-5.00"
    assert by_name["'=HYPERLINK(1)"][2] == "10.00"
    assert not any(cell.startswith("=") for row in rows for cell in row)


# ---- Statement PDF -----------------------------------------------------------------------


def _blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_with_too_many_pages_is_refused():
    with pytest.raises(StatementParseError, match=f"more than {MAX_PAGES} pages"):
        parse_statement_pdf(_blank_pdf(MAX_PAGES + 1))


def test_pdf_page_cap_is_checked_before_reading_text(monkeypatch):
    from pypdf import PageObject

    def explode(self, *args, **kwargs):
        raise AssertionError("text was extracted from an over-long file")

    monkeypatch.setattr(PageObject, "extract_text", explode)
    with pytest.raises(StatementParseError, match="more than"):
        parse_statement_pdf(_blank_pdf(MAX_PAGES + 1))


def test_other_pypdf_errors_become_statement_parse_errors(monkeypatch):
    from pypdf import PageObject

    def explode(self, *args, **kwargs):
        raise KeyError("/Resources")

    monkeypatch.setattr(PageObject, "extract_text", explode)
    with pytest.raises(StatementParseError, match="could not be read"):
        parse_statement_pdf(_blank_pdf(1))


def test_truncated_pdf_is_a_parse_error():
    with pytest.raises(StatementParseError):
        parse_statement_pdf(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog")


def test_cancelling_the_mapping_clears_parsed_lines(signed_in):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from budget.statement_views import UPLOAD_SESSION_KEY
    from budget.tests.test_statement_pdf import PAGE_1, PAGE_2, PAGE_3, _pdf

    client, home, _ = signed_in
    pdf = _pdf([PAGE_1, PAGE_2, PAGE_3])
    response = client.post(
        reverse("statement_upload"),
        {"statement": SimpleUploadedFile("s.pdf", pdf, content_type="application/pdf")},
    )
    assert response["Location"] == reverse("statement_map")  # no accounts known: parsed lines wait
    assert UPLOAD_SESSION_KEY in client.session
    response = client.post(reverse("statement_map"), {"cancel": "1"})
    assert response["Location"] == reverse("statements")
    assert UPLOAD_SESSION_KEY not in client.session


def test_a_failed_new_upload_does_not_keep_the_abandoned_one(signed_in):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from budget.statement_views import UPLOAD_SESSION_KEY

    client, home, _ = signed_in
    session = client.session
    session[UPLOAD_SESSION_KEY] = {"household": home.pk, "sha": "x", "sections": []}
    session.save()
    client.post(
        reverse("statement_upload"),
        {"statement": SimpleUploadedFile("x.pdf", b"not a pdf", content_type="application/pdf")},
    )
    assert UPLOAD_SESSION_KEY not in client.session


# ---- Guide images ------------------------------------------------------------------------


@pytest.fixture
def guide_files(tmp_path, settings):
    settings.GUIDE_ROOT = tmp_path
    images = tmp_path / guide.IMAGE_DIR
    images.mkdir(parents=True)
    for name in ("shared.png", "admin-only.png", "orphan.png"):
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    for chapter in guide.CHAPTERS:
        path = tmp_path / chapter.path
        path.parent.mkdir(parents=True, exist_ok=True)
        picture = "admin-only" if chapter.admin_only else "shared"
        extra = "\n![x](images/shared.png)" if chapter.admin_only else ""
        path.write_text(f"# {chapter.title}\n\n![x](images/{picture}.png){extra}\n")
    guide._cache.clear()
    yield
    guide._cache.clear()


def _member(client, staff):
    user = get_user_model().objects.create_user(f"g-{staff}", password="pw", is_staff=staff)
    home = Household.objects.create(name=f"g{staff}")
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)


@pytest.mark.django_db
def test_admin_only_image_is_hidden_from_members(client, guide_files):
    _member(client, staff=False)
    assert client.get(reverse("docs_image", args=["shared.png"])).status_code == 200
    assert client.get(reverse("docs_image", args=["admin-only.png"])).status_code == 404
    assert client.get(reverse("docs_image", args=["orphan.png"])).status_code == 404


@pytest.mark.django_db
def test_staff_can_read_every_guide_image(client, guide_files):
    _member(client, staff=True)
    for name in ("shared.png", "admin-only.png", "orphan.png"):
        assert client.get(reverse("docs_image", args=[name])).status_code == 200


@pytest.mark.django_db
def test_every_image_in_a_member_chapter_is_still_served_to_members(client):
    """Real chapter files: the gate must not hide a picture a member chapter shows."""
    guide._cache.clear()
    _member(client, staff=False)
    shown = {
        name
        for chapter in guide.CHAPTERS
        if not chapter.admin_only
        for name in guide._chapter_images(chapter)
    }
    assert shown
    for name in shown:
        if guide.image_path(name) is not None:
            assert client.get(reverse("docs_image", args=[name])).status_code == 200, name


# ---- Deployment files --------------------------------------------------------------------


def test_container_code_is_not_owned_by_the_runtime_user():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--chown=app" not in dockerfile
    assert dockerfile.index("collectstatic") < dockerfile.index("USER app")


def test_db_service_gets_only_postgres_variables():
    compose = (ROOT / "compose.yml").read_text()
    db = compose.split("\n  db:\n")[1].split("\nnetworks:")[0]
    assert "env_file" not in db
    assert set(re.findall(r"^\s+([A-Z_]+):", db.split("environment:")[1], re.M)) >= {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }


def test_dockerignore_keeps_development_files_out_of_the_image():
    lines = set((ROOT / ".dockerignore").read_text().split())
    assert {
        "tests",
        "node_modules",
        ".claude",
        ".codex",
        ".agents",
        "*.log",
        ".env.*",
        "test-results",
    } <= lines


def test_backup_lock_is_not_a_predictable_tmp_path():
    for relative in (
        "scripts/backup-request.sh",
        "scripts/backup.sh",
    ):
        assert "/tmp/nononcents-backup.lock" not in (ROOT / relative).read_text(), relative
