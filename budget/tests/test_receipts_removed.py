import pytest
from django.urls import NoReverseMatch, reverse

from budget.forms import QuickActionForm


def test_receipt_urls_and_quick_action_are_gone():
    with pytest.raises(NoReverseMatch):
        reverse("receipt_upload")
    assert "receipt_upload" not in dict(QuickActionForm.DESTINATIONS)
