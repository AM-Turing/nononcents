"""Money entry fields.

People may type an amount as ``1234.56``, ``1,234.56`` or ``$1,234.56`` (and a leading
minus where the field allows negatives). The server removes the dollar sign and the
thousands commas before the usual Decimal validation, so the stored value is the same
whichever way it was typed.

A comma is only accepted as a thousands separator (``1,234``, ``12,345,678.90``). Text
such as ``12,50`` is rejected rather than guessed at, because some people write that for
twelve dollars fifty and reading it as 1,250 would record the wrong amount.
"""

import re
from decimal import Decimal

from django import forms

_AMOUNT = re.compile(
    r"^(?P<sign>-?)\s*\$?\s*(?P<sign_after>-?)"
    r"(?P<whole>\d{1,3}(?:,\d{3})+|\d*)"
    r"(?P<fraction>\.\d*)?$"
)

INVALID_AMOUNT = "Enter an amount such as 1234.56 or 1,234.56."


def normalize_money_text(value):
    """Return ``value`` without ``$`` and thousands commas. Text that is not a well-formed
    amount comes back as ``NaN`` so validation rejects it; Decimal alone would read forms
    such as ``1e3`` or ``1_000`` as valid numbers."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = _AMOUNT.match(text)
    if not match:
        return "NaN"
    if match["sign"] and match["sign_after"]:
        return text
    whole = match["whole"].replace(",", "")
    fraction = match["fraction"] or ""
    if not whole and fraction in ("", "."):
        return text
    return f"{match['sign'] or match['sign_after']}{whole}{fraction}"


class MoneyInput(forms.TextInput):
    """A text box (not ``type=number``, which refuses commas) with the decimal keypad."""

    def __init__(self, attrs=None):
        base = {"inputmode": "decimal", "autocomplete": "off", "spellcheck": "false"}
        super().__init__({**base, **(attrs or {})})


class MoneyField(forms.DecimalField):
    """A two-decimal money amount that accepts ``$`` and thousands commas."""

    widget = MoneyInput
    default_error_messages = {"invalid": INVALID_AMOUNT}

    def __init__(self, *, max_digits=14, decimal_places=2, **kwargs):
        # A float bound compares wrongly with Decimal input (0.01 as a float is above
        # Decimal("0.01")), so bounds are always held as exact Decimals.
        for bound in ("min_value", "max_value"):
            if isinstance(kwargs.get(bound), float):
                kwargs[bound] = Decimal(str(kwargs[bound]))
        super().__init__(max_digits=max_digits, decimal_places=decimal_places, **kwargs)

    def to_python(self, value):
        return super().to_python(normalize_money_text(value))
