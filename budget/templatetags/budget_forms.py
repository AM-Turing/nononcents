from django import template

register = template.Library()


@register.filter
def submit_label(title):
    """The save button's words for a form page title: "Edit X" -> "Save changes", and so on."""
    title = str(title or "").strip()
    if title.startswith("Edit "):
        return "Save changes"
    if title.startswith("Add "):
        return f"Save {title[4:]}"
    if title.startswith("Skip "):
        return "Skip this month"
    if title.startswith("Reset password"):
        return "Save new password"
    if title.startswith("Move "):
        return title
    return f"Save {title.lower()}" if title else "Save"
