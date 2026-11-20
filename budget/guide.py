"""The in-app documentation (Help → /docs/): chapters, rendering and the search index.

The chapters are the markdown files in the repository (docs/guide/ and three technical
documents), so the app and the repository show the same text. Every signed-in user sees the
everyday chapters; administrators (staff) also see the administrator and technical chapters.

Rendering uses Python-Markdown with raw HTML disabled. Heading ids are GitHub-style slugs, so
links written for the repository (``everyday-use.md#add-an-expense``) work in the app too:
links to another chapter file become ``/docs/<key>/#anchor`` and ``images/x.png`` becomes
``/docs/images/x.png``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from xml.etree.ElementTree import Element

import markdown
from django.conf import settings
from django.urls import reverse
from markdown.extensions import Extension
from markdown.extensions.toc import TocExtension
from markdown.treeprocessors import Treeprocessor


@dataclass(frozen=True)
class Chapter:
    key: str
    title: str
    path: str  # relative to the repository root
    admin_only: bool
    summary: str


CHAPTERS: tuple[Chapter, ...] = (
    Chapter(
        "getting-started",
        "Getting started",
        "docs/guide/getting-started.md",
        False,
        "Set up a new profile step by step",
    ),
    Chapter(
        "everyday-use",
        "Everyday use",
        "docs/guide/everyday-use.md",
        False,
        "Add transactions, pay bills, check statements",
    ),
    Chapter(
        "understanding-the-numbers",
        "Understanding the numbers",
        "docs/guide/understanding-the-numbers.md",
        False,
        "What each figure and chart means",
    ),
    Chapter(
        "troubleshooting",
        "Troubleshooting",
        "docs/guide/troubleshooting.md",
        False,
        "Common budget and technical problems",
    ),
    Chapter(
        "administrator",
        "Administrator guide",
        "docs/guide/administrator.md",
        True,
        "Profiles, users, backups and server tasks",
    ),
    Chapter(
        "deployment",
        "Deployment and updates",
        "docs/deployment.md",
        True,
        "Install, update, roll back, HTTPS",
    ),
    Chapter(
        "backup-and-restore",
        "Backup and restore",
        "docs/backup-and-restore.md",
        True,
        "Nightly backups, restore and drills",
    ),
    Chapter(
        "calculation-rules",
        "Calculation rules",
        "docs/calculation-rules.md",
        True,
        "Exactly how every figure is worked out",
    ),
)
BY_KEY = {chapter.key: chapter for chapter in CHAPTERS}
IMAGE_DIR = "docs/guide/images"
IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.(png|jpg|webp)$")


def repo_root() -> Path:
    return Path(getattr(settings, "GUIDE_ROOT", settings.BASE_DIR))


def visible_chapters(user) -> list[Chapter]:
    staff = bool(getattr(user, "is_staff", False))
    return [chapter for chapter in CHAPTERS if staff or not chapter.admin_only]


def can_read(user, chapter: Chapter) -> bool:
    return not chapter.admin_only or bool(getattr(user, "is_staff", False))


def slugify(value: str, separator: str = "-") -> str:
    """GitHub-style heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    value = re.sub(r"[^\w\s-]", "", value.strip().lower())
    return re.sub(r"[\s]+", separator, value)


# ---- Rendering ---------------------------------------------------------------------------


class _Links(Treeprocessor):
    """Point repository links at the docs pages and open outside links in a new tab."""

    def __init__(self, md, chapter: Chapter):
        super().__init__(md)
        self.chapter = chapter
        self.files = {c.path: c for c in CHAPTERS}

    def _chapter_link(self, href: str) -> str | None:
        path, _, anchor = href.partition("#")
        if not path.endswith(".md"):
            return None
        base = posixpath.dirname(self.chapter.path)
        target = self.files.get(posixpath.normpath(posixpath.join(base, path)))
        if target is None:
            return None
        url = reverse("docs_chapter", args=[target.key])
        return f"{url}#{anchor}" if anchor else url

    def run(self, root: Element) -> None:
        for link in root.iter("a"):
            href = link.get("href", "")
            if href.startswith(("http://", "https://")):
                link.set("rel", "noopener noreferrer")
                link.set("target", "_blank")
            elif (url := self._chapter_link(href)) is not None:
                link.set("href", url)
        for image in root.iter("img"):
            name = posixpath.basename(image.get("src", ""))
            if IMAGE_NAME.match(name):
                image.set("src", reverse("docs_image", args=[name]))
                image.set("loading", "lazy")
        for table in root.iter("table"):
            # Wrapped later (see _wrap_tables) so wide tables scroll on their own.
            table.set("class", "doc-table")


class _GuideExtension(Extension):
    def __init__(self, chapter: Chapter):
        super().__init__()
        self.chapter = chapter

    def extendMarkdown(self, md):  # noqa: N802 (Markdown's API name)
        # The chapters are our own files, but raw HTML is still not passed through.
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.treeprocessors.register(_Links(md, self.chapter), "guide_links", 5)


def _wrap_tables(html: str) -> str:
    return re.sub(
        r'(<table class="doc-table">.*?</table>)',
        r'<div class="table-wrap doc-table-wrap">\1</div>',
        html,
        flags=re.S,
    )


@dataclass(frozen=True)
class TocEntry:
    level: int
    text: str
    slug: str


@dataclass(frozen=True)
class Section:
    heading: str
    parent: str | None
    slug: str | None
    text: str


@dataclass(frozen=True)
class Rendered:
    title: str
    html: str
    toc: tuple[TocEntry, ...]
    sections: tuple[Section, ...]


class _SectionText(HTMLParser):
    """Split rendered HTML into plain-text sections at each h2/h3 (the intro is the first)."""

    def __init__(self, title: str):
        super().__init__(convert_charrefs=True)
        self.title = title
        self.sections: list[dict] = [{"heading": title, "parent": None, "slug": None, "text": []}]
        self.in_heading: dict | None = None
        self.in_h1 = False
        self.last_h2: str | None = None
        self.in_pre = False

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3"):
            parent = self.last_h2 if tag == "h3" else None
            self.in_heading = {"tag": tag, "parent": parent, "slug": dict(attrs).get("id")}
            self.in_heading["words"] = []
        elif tag == "h1":
            self.in_h1 = True
        elif tag == "pre":
            self.in_pre = True
            self.sections[-1]["text"].append("\n")
        elif tag in ("p", "li", "tr", "br", "td", "th", "blockquote", "h4", "dt", "dd"):
            self.sections[-1]["text"].append("\n" if tag != "td" else " ")

    def handle_endtag(self, tag):
        if self.in_heading and tag == self.in_heading["tag"]:
            heading = " ".join("".join(self.in_heading["words"]).split())
            if self.in_heading["tag"] == "h2":
                self.last_h2 = heading
            self.sections.append(
                {
                    "heading": heading,
                    "parent": self.in_heading["parent"],
                    "slug": self.in_heading["slug"],
                    "text": [],
                }
            )
            self.in_heading = None
        elif tag == "h1":
            self.in_h1 = False
        elif tag == "pre":
            self.in_pre = False

    def handle_data(self, data):
        if self.in_heading is not None:
            self.in_heading["words"].append(data)
        elif not self.in_h1:
            # A line break inside a paragraph is only a wrap in the source: read it as a space.
            self.sections[-1]["text"].append(data if self.in_pre else data.replace("\n", " "))

    def result(self) -> list[Section]:
        out = []
        for section in self.sections:
            text = re.sub(r"[ \t]+", " ", "".join(section["text"]))
            text = re.sub(r"\s*\n\s*", "\n", text).strip()
            if section["slug"] is None and not text:
                continue
            out.append(Section(section["heading"], section["parent"], section["slug"], text))
        return out


def _flatten(tokens, out: list[TocEntry]) -> list[TocEntry]:
    for token in tokens:
        if token["level"] in (2, 3):
            out.append(TocEntry(token["level"], token["name"], token["id"]))
        _flatten(token["children"], out)
    return out


def render_text(chapter: Chapter, text: str) -> Rendered:
    md = markdown.Markdown(
        extensions=[
            "tables",
            "fenced_code",
            "sane_lists",
            TocExtension(slugify=slugify, toc_depth="2-3", permalink=False),
            _GuideExtension(chapter),
        ],
        output_format="html",
    )
    html = _wrap_tables(md.convert(text))
    tokens = getattr(md, "toc_tokens", [])
    title = next((t["name"] for t in tokens if t["level"] == 1), chapter.title)
    toc = tuple(_flatten(tokens, []))
    parser = _SectionText(title)
    parser.feed(html)
    return Rendered(title, html, toc, tuple(parser.result()))


_cache: dict[str, tuple[float, Rendered]] = {}
_cache_lock = Lock()


def render(chapter: Chapter) -> Rendered:
    """The chapter rendered, cached until its file changes."""
    path = repo_root() / chapter.path
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return Rendered(chapter.title, "", (), ())
    with _cache_lock:
        cached = _cache.get(chapter.key)
        if cached and cached[0] == mtime:
            return cached[1]
    rendered = render_text(chapter, path.read_text(encoding="utf-8"))
    with _cache_lock:
        _cache[chapter.key] = (mtime, rendered)
    return rendered


def search_index(user) -> list[dict]:
    """Every section of every chapter the user may read, for the search box."""
    entries = []
    for order, chapter in enumerate(visible_chapters(user)):
        for position, section in enumerate(render(chapter).sections):
            entries.append(
                {
                    "chapter": chapter.key,
                    "chapterTitle": chapter.title,
                    "chapterOrder": order,
                    "order": position,
                    "heading": section.heading,
                    "parent": section.parent,
                    "slug": section.slug,
                    "text": section.text,
                    "url": reverse("docs_chapter", args=[chapter.key]),
                }
            )
    return entries


def image_path(name: str) -> Path | None:
    if not IMAGE_NAME.match(name):
        return None
    path = repo_root() / IMAGE_DIR / name
    return path if path.is_file() else None


_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")


def _chapter_images(chapter: Chapter) -> frozenset[str]:
    """Image file names a chapter's text refers to."""
    try:
        text = (repo_root() / chapter.path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    return frozenset(posixpath.basename(ref) for ref in _IMAGE_REF.findall(text))


def can_read_image(user, name: str) -> bool:
    """Staff may read every image; others only those used by a chapter they may read."""
    if getattr(user, "is_staff", False):
        return True
    return any(name in _chapter_images(chapter) for chapter in visible_chapters(user))
