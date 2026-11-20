from django.apps import AppConfig


class BudgetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "budget"

    def ready(self):
        from django.conf import settings

        from budget import testclock

        testclock.install(settings.DEBUG, settings.TIME_ZONE)
