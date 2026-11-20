"""Sign-in throttling per username + device (client IP), with a device-wide backstop.

Stores only a casefolded username and IP. Failures from one device never lock the same
username on another device; one device cannot try more than IP_LIMIT passwords at all.
"""

import ipaddress
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

from .models import LoginFailure

WINDOW = timedelta(minutes=15)
USERNAME_LIMIT = 5
IP_LIMIT = 20
RETENTION = timedelta(days=1)

logger = logging.getLogger("budget.security")


def username_key(username: str) -> str:
    return (username or "").strip().casefold()[:150]


def client_ip(request: HttpRequest) -> str | None:
    remote = request.META.get("REMOTE_ADDR", "")
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        return None
    if any(
        remote_ip in net
        for net in settings.TRUSTED_PROXY_NETWORKS
        if net.version == remote_ip.version
    ):
        real = request.META.get("HTTP_X_REAL_IP", "").strip()
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return str(remote_ip)


def _remaining(qs, limit: int, now) -> timedelta | None:
    times = list(qs.filter(at__gt=now - WINDOW).order_by("at").values_list("at", flat=True)[:limit])
    if len(times) < limit:
        return None
    return times[0] + WINDOW - now


def lock_message(request: HttpRequest, username: str) -> str | None:
    """Return the lockout message if this username or IP is locked, else None."""
    now = timezone.now()
    waits = []
    key = username_key(username)
    ip = client_ip(request)
    if key:
        pair = LoginFailure.objects.filter(username_key=key, ip=ip)
        waits.append(_remaining(pair, USERNAME_LIMIT, now))
    if ip:
        waits.append(_remaining(LoginFailure.objects.filter(ip=ip), IP_LIMIT, now))
    active = [w for w in waits if w is not None]
    if not active:
        return None
    minutes = max(1, math.ceil(max(active).total_seconds() / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many sign-in attempts. Try again in {minutes} {unit}."


def record_failure(request: HttpRequest, username: str) -> None:
    now = timezone.now()
    key = username_key(username)
    ip = client_ip(request)
    LoginFailure.objects.filter(at__lt=now - RETENTION).delete()
    LoginFailure.objects.create(username_key=key, ip=ip, at=now)
    window_start = now - WINDOW
    started = LoginFailure.objects.filter(username_key=key, ip=ip, at__gt=window_start).count() == (
        USERNAME_LIMIT
    )
    if ip and LoginFailure.objects.filter(ip=ip, at__gt=window_start).count() == IP_LIMIT:
        started = True
    if started:
        logger.warning("Sign-in locked for an account after repeated failures from %s", ip)


def clear_failures(request: HttpRequest, username: str) -> None:
    """Forget this username's failures from this device only."""
    LoginFailure.objects.filter(username_key=username_key(username), ip=client_ip(request)).delete()
