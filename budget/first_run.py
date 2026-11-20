"""First run: a new installation has no budget profile, so the first administrator creates one.

The administrator signs in without choosing a profile (see ProfileAuthenticationForm), and
this middleware sends them to the one-time setup page until a profile exists.
"""

from django.shortcuts import redirect
from django.urls import reverse

from .models import Household

EXEMPT_PREFIXES = ("/static/", "/health/", "/logout/", "/setup/", "/login/")


class FirstRunMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.is_superuser
            and not request.path.startswith(EXEMPT_PREFIXES)
            and not Household.objects.exists()
        ):
            return redirect(reverse("first_profile"))
        return self.get_response(request)
