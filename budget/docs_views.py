"""Help → the in-app documentation pages (chapters are listed in ``budget.guide``)."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from . import guide
from .views import household


def _household(request):
    try:
        return household(request)
    except PermissionDenied:
        return None


@login_required
@require_GET
def docs_home(request):
    return redirect("docs_chapter", key=guide.visible_chapters(request.user)[0].key)


@login_required
@require_GET
def docs_chapter(request, key):
    chapter = guide.BY_KEY.get(key)
    # Administrator chapters do not exist for everyone else.
    if chapter is None or not guide.can_read(request.user, chapter):
        raise Http404("No such guide")
    rendered = guide.render(chapter)
    chapters = guide.visible_chapters(request.user)
    position = chapters.index(chapter)
    return render(
        request,
        "budget/docs.html",
        {
            "household": _household(request),
            "chapter": chapter,
            "chapters": chapters,
            "rendered": rendered,
            "previous_chapter": chapters[position - 1] if position > 0 else None,
            "next_chapter": chapters[position + 1] if position + 1 < len(chapters) else None,
            "has_admin_chapters": any(c.admin_only for c in chapters),
        },
    )


@login_required
@require_GET
@cache_control(private=True, max_age=300)
def docs_search_index(request):
    return JsonResponse({"sections": guide.search_index(request.user)})


@login_required
@require_GET
@cache_control(private=True, max_age=86400)
def docs_image(request, name):
    path = guide.image_path(name)
    # An image used only by administrator chapters is as hidden as those chapters.
    if path is None or not guide.can_read_image(request.user, name):
        raise Http404("No such image")
    return FileResponse(path.open("rb"))
