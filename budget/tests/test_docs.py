"""Help → guides: who sees which chapter, rendering, the search index and the real chapter files."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from budget import guide
from budget.models import Household, HouseholdMembership


def _user(staff=False, name="reader"):
    user = get_user_model().objects.create_user(name, password="pw", is_staff=staff)
    home = Household.objects.create(name=f"{name} budget")
    HouseholdMembership.objects.create(household=home, user=user)
    return user


@pytest.fixture
def chapters(tmp_path, settings):
    """Small stand-in chapter files, so these tests do not depend on the real text."""
    settings.GUIDE_ROOT = tmp_path
    for chapter in guide.CHAPTERS:
        path = tmp_path / chapter.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {chapter.title}\n\nIntro for {chapter.key}.\n\n"
            "## Add a recurring bill\n\nOpen **Bills** and tap **Add bill**.\nSafe to\nspend.\n\n"
            "### Choose how often\n\nMonthly or every two weeks.\n\n"
            "See [the other guide](../guide/everyday-use.md#add-an-expense) and "
            "[outside](https://example.com).\n\n<script>alert(1)</script>\n\n"
            "![Home page](images/home.png)\n",
            encoding="utf-8",
        )
    images = tmp_path / guide.IMAGE_DIR
    images.mkdir(parents=True, exist_ok=True)
    (images / "home.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    guide._cache.clear()
    yield tmp_path
    guide._cache.clear()


@pytest.mark.django_db
def test_docs_need_sign_in(client, chapters):
    response = client.get(reverse("docs_chapter", args=["getting-started"]))
    assert response.status_code == 302
    assert "/login/" in response["Location"]
    assert client.get(reverse("docs_search_index")).status_code == 302


@pytest.mark.django_db
def test_members_see_only_the_everyday_chapters(client, chapters):
    client.force_login(_user())
    response = client.get(reverse("docs"))
    assert response["Location"] == reverse("docs_chapter", args=["getting-started"])
    page = client.get(reverse("docs_chapter", args=["getting-started"])).content.decode()
    assert "Troubleshooting" in page
    assert "Administrator guide" not in page
    assert "For administrators" not in page
    for chapter in guide.CHAPTERS:
        status = client.get(reverse("docs_chapter", args=[chapter.key])).status_code
        assert status == (404 if chapter.admin_only else 200), chapter.key
    index = client.get(reverse("docs_search_index")).json()["sections"]
    assert {s["chapter"] for s in index} == {c.key for c in guide.CHAPTERS if not c.admin_only}


@pytest.mark.django_db
def test_administrators_see_every_chapter(client, chapters):
    client.force_login(_user(staff=True, name="admin"))
    page = client.get(reverse("docs_chapter", args=["administrator"])).content.decode()
    assert "For administrators" in page
    for chapter in guide.CHAPTERS:
        assert client.get(reverse("docs_chapter", args=[chapter.key])).status_code == 200
    index = client.get(reverse("docs_search_index")).json()["sections"]
    assert {s["chapter"] for s in index} == {c.key for c in guide.CHAPTERS}


@pytest.mark.django_db
def test_unknown_chapter_and_image_are_not_found(client, chapters):
    client.force_login(_user())
    assert client.get(reverse("docs_chapter", args=["nope"])).status_code == 404
    assert client.get(reverse("docs_image", args=["missing.png"])).status_code == 404
    assert client.get("/docs/images/..%2Fsecret.png").status_code == 404
    response = client.get(reverse("docs_image", args=["home.png"]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content).startswith(b"\x89PNG")


@pytest.mark.django_db
def test_chapter_renders_headings_links_images_and_no_raw_html(client, chapters):
    client.force_login(_user())
    page = client.get(reverse("docs_chapter", args=["getting-started"])).content.decode()
    assert '<h2 id="add-a-recurring-bill">Add a recurring bill</h2>' in page
    assert 'href="#choose-how-often"' in page  # On this page
    assert 'href="/docs/everyday-use/#add-an-expense"' in page
    assert 'rel="noopener noreferrer" target="_blank"' in page or (
        'target="_blank"' in page and "noopener" in page
    )
    assert 'src="/docs/images/home.png"' in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert reverse("docs") in page  # the Help menu link


@pytest.mark.django_db
def test_search_index_sections(client, chapters):
    client.force_login(_user())
    index = client.get(reverse("docs_search_index")).json()["sections"]
    mine = [s for s in index if s["chapter"] == "getting-started"]
    assert [s["heading"] for s in mine] == [
        "Getting started",
        "Add a recurring bill",
        "Choose how often",
    ]
    intro, bill, often = mine
    assert intro["slug"] is None and intro["text"] == "Intro for getting-started."
    assert bill["slug"] == "add-a-recurring-bill"
    # A line wrap in the source reads as a space, so phrases still match.
    assert "Safe to spend." in bill["text"]
    assert often["parent"] == "Add a recurring bill"
    assert bill["url"] == "/docs/getting-started/"


def test_slugs_match_github_style():
    assert guide.slugify("Add a bill: now!") == "add-a-bill-now"
    assert guide.slugify("Back up now (Admin)") == "back-up-now-admin"
    assert guide.slugify("What “Safe to spend” means") == "what-safe-to-spend-means"


def test_page_can_be_renamed():
    from budget.forms import PAGE_CHOICES, PAGE_LABEL_DEFAULTS, PAGE_TITLE_DEFAULTS

    assert ("docs", "Help") in PAGE_CHOICES
    assert PAGE_LABEL_DEFAULTS["docs"] == "Help"
    assert "docs" in PAGE_TITLE_DEFAULTS


# ---- The real chapter files ------------------------------------------------------------


ROOT = Path(settings.BASE_DIR)


@pytest.mark.parametrize("chapter", guide.CHAPTERS, ids=lambda c: c.key)
def test_real_chapter_exists_with_one_title(chapter):
    text = (ROOT / chapter.path).read_text(encoding="utf-8")
    titles = [line for line in _outside_code(text) if re.match(r"#\s", line)]
    assert len(titles) == 1, f"{chapter.path} should have exactly one # title"


@pytest.mark.parametrize("chapter", guide.CHAPTERS, ids=lambda c: c.key)
def test_real_chapter_links_and_images_resolve(chapter):
    guide._cache.clear()
    rendered = guide.render(chapter)
    anchors = {c.key: {e.slug for e in guide.render(c).toc} for c in guide.CHAPTERS}
    for href in re.findall(r'href="(/docs/[^"]+)"', rendered.html):
        match = re.fullmatch(r"/docs/([a-z-]+)/(?:#(.+))?", href)
        assert match, f"{chapter.path}: bad link {href}"
        key, anchor = match.groups()
        assert key in anchors, f"{chapter.path}: link to unknown chapter {href}"
        if anchor:
            assert anchor in anchors[key], f"{chapter.path}: link to missing heading {href}"
    for href in re.findall(r'href="#([^"]+)"', rendered.html):
        assert href in anchors[chapter.key], f"{chapter.path}: link to missing heading #{href}"
    for src in re.findall(r'src="/docs/images/([^"]+)"', rendered.html):
        assert guide.image_path(src), f"{chapter.path}: missing image {src}"
    # Links to other repository markdown files that are not chapters would be dead in the app.
    assert not re.search(r'href="[^"]*\.md(#[^"]*)?"', rendered.html), (
        f"{chapter.path}: links to a markdown file that is not a chapter"
    )


@pytest.mark.parametrize(
    "chapter", [c for c in guide.CHAPTERS if not c.admin_only], ids=lambda c: c.key
)
def test_real_member_chapters_hold_no_server_commands(chapter):
    text = (ROOT / chapter.path).read_text(encoding="utf-8")
    for word in ("docker ", "sudo ", "make backup", "/home/", "manage.py"):
        assert word not in text, f"{chapter.path} mentions {word.strip()}"


def _outside_code(text):
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield line
