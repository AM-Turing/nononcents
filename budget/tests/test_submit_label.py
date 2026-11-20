import pytest

from budget.templatetags.budget_forms import submit_label


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("Edit Streaming Service — November 2026", "Save changes"),
        ("Edit pay period", "Save changes"),
        ("Add bill", "Save bill"),
        ("Add savings goal", "Save savings goal"),
        ("Skip Internet — December 2026", "Skip this month"),
        ("Reset password for robin", "Save new password"),
        ("Move savings", "Move savings"),
        ("", "Save"),
    ],
)
def test_submit_label(title, label):
    assert submit_label(title) == label
