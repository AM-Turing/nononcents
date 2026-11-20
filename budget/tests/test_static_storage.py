from django.conf import settings as django_settings


def test_production_uses_hashed_static_names():
    # The real settings (not the test override) must name the manifest storage, under the
    # STORAGES key that Django 5.x reads.
    from nononcents import settings as project_settings

    assert not hasattr(project_settings, "STATICFILES_STORAGE")
    backend = project_settings.STORAGES["staticfiles"]["BACKEND"]
    assert backend == "whitenoise.storage.CompressedManifestStaticFilesStorage"
    assert django_settings.STATIC_ROOT
