"""HTTPS behind nginx: trusted proxy header, redirect, and the health-check exemption."""

import importlib
import os
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

HTTPS_BEHIND_PROXY = {
    "SECURE_SSL_REDIRECT": True,
    "SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https"),
    "SECURE_REDIRECT_EXEMPT": [r"^health/$"],
}


@pytest.mark.django_db
@override_settings(**HTTPS_BEHIND_PROXY)
def test_plain_http_is_redirected_and_https_from_nginx_is_served(client):
    plain = client.get(reverse("login"))
    assert plain.status_code == 301 and plain.url.startswith("https://")
    via_nginx = client.get(reverse("login"), HTTP_X_FORWARDED_PROTO="https")
    assert via_nginx.status_code == 200


@pytest.mark.django_db
@override_settings(**HTTPS_BEHIND_PROXY)
def test_container_health_check_is_not_redirected(client):
    assert client.get("/health/").status_code == 200


def test_redirect_without_trusting_the_proxy_refuses_to_start():
    from nononcents import settings as project_settings

    env = {"SECURE_SSL_REDIRECT": "true", "TRUST_PROXY_HTTPS": "false"}
    with mock.patch.dict(os.environ, env), pytest.raises(ImproperlyConfigured):
        importlib.reload(project_settings)
    importlib.reload(project_settings)


def test_proxy_header_is_trusted_only_when_asked():
    from nononcents import settings as project_settings

    with mock.patch.dict(os.environ, {"TRUST_PROXY_HTTPS": "true", "SECURE_SSL_REDIRECT": "true"}):
        reloaded = importlib.reload(project_settings)
        assert reloaded.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    reloaded = importlib.reload(project_settings)
    assert reloaded.SECURE_PROXY_SSL_HEADER is None
