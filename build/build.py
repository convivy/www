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


AUTHORSHIP_LABELS = {"human": "Human", "collab": "Collab", "llm": "LLM"}


def prepare_post(post: dict) -> dict:
    authorship = post["authorship"]
    return {
        **post,
        "date_display": display_date(post["date"]),
        "updated_display": display_date(post["updated"]) if post.get("updated") else None,
        "authorship_label": AUTHORSHIP_LABELS.get(authorship, authorship),
        "body_html": render_markdown(post["body"]),
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

    index_tmpl = env.get_template("fieldnotes_index.html")
    (fieldnotes_dir / "index.html").write_text(
        index_tmpl.render(root="/", year=year, posts=posts),
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
