#!/usr/bin/env python3
"""Static site builder for convivy.com.

Renders `_site/` from `templates/` + `content/`, plus (optionally) the
Field Notes corpus pulled from an Orient endpoint at build time. Content
never lives in this repo — see README.md for the corpus contract this
script builds against.

Usage:
    python build/build.py

Environment:
    ORIENT_FIELDNOTES_URL       Corpus endpoint. Unset -> build with an
                                 empty-state Field Notes index. Set but the
                                 fetch fails or the payload is malformed ->
                                 this script exits non-zero. A configured
                                 endpoint that silently yields an empty blog
                                 is exactly the failure mode this build
                                 refuses to produce.

                                 When set, the endpoint sits behind two
                                 walls: a Cloudflare Access service token at
                                 the edge, and an Orient-issued bearer token
                                 at the origin. All three of the following
                                 are then REQUIRED — missing any one exits 1
                                 naming exactly which:
    CF_ACCESS_CLIENT_ID         Sent as header CF-Access-Client-Id.
    CF_ACCESS_CLIENT_SECRET     Sent as header CF-Access-Client-Secret.
    ORIENT_BLOG_TOKEN           Sent as header Authorization: Bearer <token>.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

import markdown
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
CONTENT_DIR = ROOT / "content"
STATIC_DIR = ROOT / "static"
OUT_DIR = ROOT / "_site"

MD = markdown.Markdown(extensions=["extra", "smarty"])

# How many of the newest posts the Field Notes front page renders in full
# (the river). Posts beyond the cap appear in the year-grouped "Older posts"
# archive list below the river, linking to their permalink pages, so the
# front page stays a readable length however large the corpus grows.
RIVER_CAP = 5


class CorpusError(RuntimeError):
    """Raised when ORIENT_FIELDNOTES_URL is set but the corpus can't be used."""


def render_markdown(text: str) -> str:
    MD.reset()
    return MD.convert(text)


def fetch_corpus(url: str, creds: dict[str, str]) -> list[dict]:
    """Fetch and validate the Field Notes corpus from `url`.

    `creds` must already contain non-empty CF_ACCESS_CLIENT_ID,
    CF_ACCESS_CLIENT_SECRET, and ORIENT_BLOG_TOKEN — the endpoint sits
    behind both a Cloudflare Access service token (edge) and an
    Orient-issued bearer token (origin), and answers 401 without both.

    Raises CorpusError on any failure — network, HTTP status, JSON parse,
    or shape mismatch against the documented contract. Callers must not
    swallow this: a configured endpoint that fails must fail the build.
    """
    headers = {
        "CF-Access-Client-Id": creds["CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": creds["CF_ACCESS_CLIENT_SECRET"],
        "Authorization": f"Bearer {creds['ORIENT_BLOG_TOKEN']}",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CorpusError(f"failed to fetch Field Notes corpus from {url}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CorpusError(f"Field Notes corpus at {url} did not return valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "posts" not in payload:
        raise CorpusError(
            f"Field Notes corpus at {url} is malformed: expected a JSON object with a "
            f"'posts' key, got: {type(payload).__name__}"
        )

    posts = payload["posts"]
    if not isinstance(posts, list):
        raise CorpusError(
            f"Field Notes corpus at {url} is malformed: 'posts' must be a list, "
            f"got: {type(posts).__name__}"
        )

    # Silent-truncation guard: when the payload declares a count, it must
    # match the number of posts actually returned. A mismatch means the
    # response was cut off somewhere upstream and must fail loudly rather
    # than quietly publish a partial corpus.
    if "count" in payload and payload["count"] != len(posts):
        raise CorpusError(
            f"Field Notes corpus at {url} is malformed: 'count' says "
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


def display_date(iso_date: str) -> str:
    """Render an ISO 8601 date/datetime string as a human-readable date."""
    d = datetime.date.fromisoformat(iso_date[:10])
    return d.strftime("%B %-d, %Y") if os.name != "nt" else d.strftime("%B %d, %Y")


def short_date(iso_date: str) -> str:
    """Render an ISO 8601 date/datetime as a month-and-day date ("Aug 4").

    Used in the archive list, where rows sit under a year heading and
    repeating the year on every row would be noise.
    """
    d = datetime.date.fromisoformat(iso_date[:10])
    return d.strftime("%b %-d") if os.name != "nt" else d.strftime("%b %d")


AUTHORSHIP_LABELS = {"human": "Human", "collab": "Collab", "llm": "LLM"}

_HEADING_TAG_RE = re.compile(r"(</?h)([1-6])\b", flags=re.IGNORECASE)

_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*(?:\n|$)")
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+) *$")


def _normalize_heading_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def strip_leading_title_heading(body: str, title: str) -> str:
    """Strip the post body's leading heading if its text matches `title`.

    Orient's corpus bodies commonly begin with the post title restated as a
    heading (the templates already render the title from the `title` field,
    so left alone this duplicates it). Strips that leading heading, in
    either ATX (`# Title`) or setext (`Title` underlined with `===`/`---`)
    form, comparing case-insensitively and whitespace-normalized, and
    tolerating leading blank lines before the heading.

    Leaves `body` byte-for-byte untouched if the leading heading's text
    doesn't match `title`, or if the body has no leading heading at all —
    this never strips content that isn't the duplicated title.
    """
    normalized_title = _normalize_heading_text(title)
    if not normalized_title:
        return body

    leading_stripped = body.lstrip("\n\r\t ")

    atx_match = _ATX_HEADING_RE.match(leading_stripped)
    if atx_match:
        if _normalize_heading_text(atx_match.group(2)) == normalized_title:
            return leading_stripped[atx_match.end():].lstrip("\n")
        return body

    first_line, sep, remainder = leading_stripped.partition("\n")
    if sep and first_line.strip():
        second_line, sep2, rest = remainder.partition("\n")
        if _SETEXT_UNDERLINE_RE.match(second_line):
            if _normalize_heading_text(first_line) == normalized_title:
                return rest.lstrip("\n") if sep2 else ""
        return body

    return body


def shift_headings(html: str, by: int = 1) -> str:
    """Demote every <h1>–<h6> in `html` by `by` levels, capping at <h6>.

    The Field Notes front page renders full post bodies inline under the
    page's single <h1> and each post's <h2> title, so body headings
    (authored as h2/h3 in markdown for the permalink page, where the title
    is the h1) must drop one level to keep the document outline valid.
    """
    return _HEADING_TAG_RE.sub(
        lambda m: f"{m.group(1)}{min(int(m.group(2)) + by, 6)}", html
    )


def prepare_post(post: dict) -> dict:
    authorship = post["authorship"]
    body = strip_leading_title_heading(post["body"], post["title"])
    body_html = render_markdown(body)
    return {
        **post,
        "date_display": display_date(post["date"]),
        "date_short": short_date(post["date"]),
        "year": post["date"][:4],
        "updated_display": display_date(post["updated"]) if post.get("updated") else None,
        "authorship_label": AUTHORSHIP_LABELS.get(authorship, authorship),
        "body_html": body_html,
        # The same body, demoted one heading level, for inline rendering on
        # the Field Notes front page (the river).
        "body_html_river": shift_headings(body_html),
    }


def load_corpus() -> list[dict]:
    """Return the Field Notes post list, or [] for the empty state.

    Exits the process (non-zero) if ORIENT_FIELDNOTES_URL is set but the
    corpus can't be fetched or is malformed — this must never degrade to
    an empty index silently. Also exits non-zero, naming exactly which
    credential(s) are absent, if the URL is set but any of the two
    Cloudflare Access creds or the Orient bearer token is missing.
    """
    url = os.environ.get("ORIENT_FIELDNOTES_URL")
    if not url:
        return []

    creds = {
        "CF_ACCESS_CLIENT_ID": os.environ.get("CF_ACCESS_CLIENT_ID"),
        "CF_ACCESS_CLIENT_SECRET": os.environ.get("CF_ACCESS_CLIENT_SECRET"),
        "ORIENT_BLOG_TOKEN": os.environ.get("ORIENT_BLOG_TOKEN"),
    }
    missing = [name for name, value in creds.items() if not value]
    if missing:
        print(
            "ERROR: ORIENT_FIELDNOTES_URL is set but missing required "
            f"credential(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        posts = fetch_corpus(url, creds)
    except CorpusError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    posts.sort(key=lambda p: p["date"], reverse=True)
    return [prepare_post(p) for p in posts]


def group_by_year(posts: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group already-sorted (newest-first) posts into (year, posts) runs.

    Preserves the incoming order both across and within groups, so the
    archive lists years newest-first and posts newest-first inside each.
    """
    groups: list[tuple[str, list[dict]]] = []
    for post in posts:
        if not groups or groups[-1][0] != post["year"]:
            groups.append((post["year"], []))
        groups[-1][1].append(post)
    return groups


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

    # Field Notes.
    posts = load_corpus()
    fieldnotes_dir = OUT_DIR / "fieldnotes"
    fieldnotes_dir.mkdir(parents=True)

    river_posts = posts[:RIVER_CAP]
    older_posts = posts[RIVER_CAP:]
    index_tmpl = env.get_template("fieldnotes_index.html")
    (fieldnotes_dir / "index.html").write_text(
        index_tmpl.render(
            root="/",
            year=year,
            posts=posts,
            river_posts=river_posts,
            older_by_year=group_by_year(older_posts),
            older_count=len(older_posts),
        ),
        encoding="utf-8",
    )

    post_tmpl = env.get_template("post.html")
    for post in posts:
        post_dir = fieldnotes_dir / post["slug"]
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "index.html").write_text(
            post_tmpl.render(root="/", year=year, post=post),
            encoding="utf-8",
        )

    # Static assets.
    shutil.copytree(STATIC_DIR, OUT_DIR / "static")

    # Custom domain + disable Jekyll processing on Pages.
    (OUT_DIR / "CNAME").write_text("convivy.com\n", encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    note = "empty state" if not posts else f"{len(posts)} post(s)"
    print(f"Built _site/ — home page + fieldnotes ({note}).")


if __name__ == "__main__":
    build()
