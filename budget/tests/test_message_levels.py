"""Django messages render with a level class, and errors announce as alerts with a text prefix."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string


def test_levels_render_distinct_classes_roles_and_prefixes(rf, db):
    request = rf.get("/")
    request.session = {}
    request.user = get_user_model().objects.create_user("u", password="pw-123456789")
    items = [
        Message(messages.SUCCESS, "Saved ok"),
        Message(messages.INFO, "Fyi"),
        Message(messages.WARNING, "Careful"),
        Message(messages.ERROR, "Broke"),
    ]
    html = render_to_string("base.html", {"messages": items}, request=request)
    assert '<div class="notice success" role="status">Saved ok</div>' in html
    assert '<div class="notice info" role="status">Fyi</div>' in html
    assert '<div class="notice warning" role="status"><strong>Check:</strong> Careful</div>' in html
    assert '<div class="notice error" role="alert"><strong>Problem:</strong> Broke</div>' in html
