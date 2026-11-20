from pathlib import Path

import pytest
from django.contrib.staticfiles import finders

from budget.forms import ExpenseForm

FONT_PATHS = (
    "fonts/plus-jakarta-sans/PlusJakartaSans-Variable.woff2",
    "fonts/space-grotesk/SpaceGrotesk-Variable.woff2",
)


@pytest.mark.django_db
def test_variable_fonts_and_licenses_resolve_through_staticfiles():
    for path in FONT_PATHS:
        resolved = finders.find(path)
        assert resolved is not None
        assert Path(resolved).read_bytes()[:4] == b"wOF2"
        assert Path(resolved).with_name("OFL.txt").is_file()


def test_global_typography_defines_faces_tokens_and_financial_numerals():
    css_path = finders.find("budget/typography.css")
    assert css_path is not None
    css = Path(css_path).read_text()
    assert css.count("@font-face") == 2
    assert "font-display: swap" in css
    assert '--font-interface: "Plus Jakarta Sans"' in css
    assert '--font-display: "Space Grotesk"' in css
    assert 'font-feature-settings: "tnum" 1, "lnum" 1' in css
    assert "https://" not in css and "http://" not in css


def test_templates_do_not_reference_external_font_services():
    roots = [Path("templates"), Path("budget/templates")]
    content = "\n".join(path.read_text() for root in roots for path in root.rglob("*.html"))
    assert "fonts.googleapis.com" not in content
    assert "fonts.gstatic.com" not in content
    assert "use.typekit.net" not in content


def test_expense_date_uses_native_picker_and_names_no_format():
    field = ExpenseForm().fields["date"]

    assert field.widget.input_type == "date"
    # The browser picks the on-screen format, so neither the help nor the tooltip names one.
    assert "YYYY" not in str(field.help_text) + str(field.widget.attrs)
