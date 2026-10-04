#!/usr/bin/env python3
"""Static site builder for convivy.com.

Renders `_site/` from `templates/` + `content/`, plus a forwarding page at
every old Field Notes address. Field Notes moved to convivybuilder.org as
Swabby's Journal, so each `/fieldnotes/<slug>/` page now sends the reader to
`https://convivybuilder.org/journal/<slug>/` and names it as canonical. The
slugs come from the `fieldnotes-corpus` branch's `corpus.json`, which Orient no
longer writes, so it holds exactly the posts convivy.com ever published. See
README.md for the corpus contract this script still validates.

Usage:
    python build/build.py

Environment:
    FIELDNOTES_CORPUS_FILE      Path to corpus.json, read from the
                                 fieldnotes-corpus branch. Unset -> build
                                 only the /fieldnotes/ index forward. Set
                                 but the file is missing, unreadable, or
                                 malformed -> this script exits non-zero. A
                                 file with no posts also exits non-zero
                                 unless FIELDNOTES_ALLOW_EMPTY is "1".
    FIELDNOTES_ALLOW_EMPTY      "1" lets the branch file hold zero posts,
                                 for a deliberate unpublish of everything.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
CONTENT_DIR = ROOT / "content"
STATIC_DIR = ROOT / "static"
OUT_DIR = ROOT / "_site"

MD = markdown.Markdown(extensions=["extra", "smarty"])

# Where Field Notes lives now. A post keeps its slug: /fieldnotes/<slug>/ on
# convivy.com forwards to JOURNAL_URL + "<slug>/".
JOURNAL_URL = "https://convivybuilder.org/journal/"


class CorpusError(RuntimeError):
    """Raised when a configured corpus source can't be used."""


def render_markdown(text: str) -> str:
    MD.reset()
    return MD.convert(text)


def read_corpus_file(path: Path) -> list[dict]:
    """Read and validate corpus.json from the fieldnotes-corpus branch.

    Raises CorpusError when the file is missing, unreadable, not JSON, or off
    the contract.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError(f"could not read Field Notes corpus file {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CorpusError(f"Field Notes corpus file {path} is not valid JSON: {exc}") from exc
    return validate_corpus(payload, source=str(path))


def validate_corpus(payload: object, *, source: str) -> list[dict]:
    """Check `payload` against the corpus contract and return its posts.

    Raises CorpusError naming `source` and exactly what is wrong.
    """
    if not isinstance(payload, dict) or "posts" not in payload:
        raise CorpusError(
            f"Field Notes corpus from {source} is malformed: expected a JSON object with a "
            f"'posts' key, got: {type(payload).__name__}"
        )

    posts = payload["posts"]
    if not isinstance(posts, list):
        raise CorpusError(
            f"Field Notes corpus from {source} is malformed: 'posts' must be a list, "
            f"got: {type(posts).__name__}"
        )

    # Silent-truncation guard: when the payload declares a count, it must
    # match the number of posts actually returned. A mismatch means the
    # response was cut off somewhere upstream and must fail loudly rather
    # than quietly publish a partial corpus.
    if "count" in payload and payload["count"] != len(posts):
        raise CorpusError(
            f"Field Notes corpus from {source} is malformed: 'count' says "
            f"{payload['count']!r} but 'posts' has {len(posts)} item(s)"
        )

    required_fields = ("slug", "title", "date", "author", "authorship", "body")
    for i, post in enumerate(posts):
        if not isinstance(post, dict):
            raise CorpusError(f"Field Notes corpus post #{i} is malformed: not a JSON object")
        missing = [f for f in required_fields if f not in post]
        if missing:
            raise CorpusError(
                f"Field Notes corpus post #{i} (slug={post.get('slug')!r}) is missing "
                f"required field(s): {', '.join(missing)}"
            )
        if post["authorship"] not in ("human", "collab", "llm"):
            raise CorpusError(
                f"Field Notes corpus post #{i} (slug={post['slug']!r}) has invalid "
                f"authorship {post['authorship']!r}: must be 'human', 'collab', or 'llm'"
            )
        try:
            datetime.date.fromisoformat(post["date"][:10])
        except (ValueError, TypeError) as exc:
            raise CorpusError(
                f"Field Notes corpus post #{i} (slug={post['slug']!r}) has an invalid "
                f"ISO 8601 date {post.get('date')!r}: {exc}"
            ) from exc

    return posts


def load_corpus() -> list[dict]:
    """Return the Field Notes post list, or [] for the empty state.

    Exits the process (non-zero) if FIELDNOTES_CORPUS_FILE is set but the
    corpus can't be read or is malformed — this must never degrade to an
    empty index silently.
    """
    corpus_file = os.environ.get("FIELDNOTES_CORPUS_FILE")
    if not corpus_file:
        return []

    try:
        posts = read_corpus_file(Path(corpus_file))
    except CorpusError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    # Refuse an empty branch corpus by default. On 2026-09-12 a development
    # run of Orient's push created this branch holding zero posts; a merge
    # ordered before Orient's real seed would otherwise have published an
    # empty Field Notes with a green run.
    if not posts and os.environ.get("FIELDNOTES_ALLOW_EMPTY") != "1":
        print(
            f"ERROR: Field Notes corpus from {corpus_file} has 0 posts. Refusing to "
            "publish an empty Field Notes; set FIELDNOTES_ALLOW_EMPTY=1 to do it "
            "deliberately.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Field Notes corpus: {len(posts)} post(s) from the fieldnotes-corpus branch")
    return posts


def build() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    year = datetime.date.today().year

    # Home page.
    home_md = (CONTENT_DIR / "home.md").read_text(encoding="utf-8")
    home_html = render_markdown(home_md)
    home_tmpl = env.get_template("home.html")
    (OUT_DIR / "index.html").write_text(
        home_tmpl.render(root="/", year=year, body_html=home_html),
        encoding="utf-8",
    )

    # People.
    people_md = (CONTENT_DIR / "people.md").read_text(encoding="utf-8")
    people_html = render_markdown(people_md)
    people_tmpl = env.get_template("people.html")
    people_dir = OUT_DIR / "people"
    people_dir.mkdir(parents=True)
    (people_dir / "index.html").write_text(
        people_tmpl.render(root="/", year=year, body_html=people_html),
        encoding="utf-8",
    )

    # Field Notes moved to Swabby's Journal. GitHub Pages can't send a 301, so
    # every old address gets a page that forwards to the same slug on the
    # journal and names it canonical. The /fieldnotes/ index forwards to the
    # journal's index.
    posts = load_corpus()
    forward_tmpl = env.get_template("forward.html")
    fieldnotes_dir = OUT_DIR / "fieldnotes"
    fieldnotes_dir.mkdir(parents=True)
    (fieldnotes_dir / "index.html").write_text(
        forward_tmpl.render(target=JOURNAL_URL, title=None),
        encoding="utf-8",
    )
    for post in posts:
        post_dir = fieldnotes_dir / post["slug"]
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "index.html").write_text(
            forward_tmpl.render(target=f"{JOURNAL_URL}{post['slug']}/", title=post["title"]),
            encoding="utf-8",
        )

    # Static assets.
    shutil.copytree(STATIC_DIR, OUT_DIR / "static")

    # Custom domain + disable Jekyll processing on Pages.
    (OUT_DIR / "CNAME").write_text("convivy.com\n", encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Built _site/ — home, people, and {len(posts)} Field Notes forward(s) plus the index.")


if __name__ == "__main__":
    build()
