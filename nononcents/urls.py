from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from budget.views import ProfileLoginView, admin_login_redirect

urlpatterns = [
    # The stock admin sign-in has no attempt limit and no activity-log entry, so it is
    # replaced by the app's own sign-in page. This must stay above admin.site.urls.
    path("admin/login/", admin_login_redirect, name="admin_login_redirect"),
    path("admin/", admin.site.urls),
    path(
        "login/",
        ProfileLoginView.as_view(),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("budget.urls")),
]
