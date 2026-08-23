# convivy.com — site source

This branch (`site-src`) is the source for convivy.com. It is not yet `main` — Jay promotes it by
hand (see **Activation checklist** below). Once promoted, this branch **is** `main`: a small
Python static-site builder plus templates, CSS, and a GitHub Actions workflow that builds and
deploys via Pages-from-Actions. No blog content lives in this repo.

## Why this repo holds no content

Field Notes used to live as files in the `convivy-lab` repo, built by a private-repo Action that
force-pushed HTML to two public repos' `gh-pages` branches. That repo is retiring entirely.
Orient — Convivy's internal knowledge store — becomes the store of truth for the blog, and every
public surface (starting with `convivy.com/fieldnotes`) becomes a renderer over it. Concretely:

- **Orient serves the post corpus on a pull endpoint.** This site's build pulls the current
  corpus at build time — full corpus, every build, which keeps the build idempotent.
- **Orient fires a rebuild trigger on publish** — a `repository_dispatch` (event type
  `fieldnotes-publish`) to this repo when a post is published or updated. A daily scheduled
  build is the fallback if a dispatch is ever missed.
- **The build renders and deploys via Pages-from-Actions** (`actions/upload-pages-artifact` +
  `actions/deploy-pages`) — no `gh-pages` branch, no force-push. That old pattern is what fights
  branch protection; this pipeline doesn't need branch protection worked around because nothing
  but this repo's own sources ever needs to land on `main`.
- **The human publish gate lives upstream, in the Writing Desk.** Jay's publish action there,
  attested via Cloudflare Access, is the moment of human decision. Everything downstream of that
  — the dispatch, the pull, the build, the deploy — is mechanics.
- **Styling boundary:** content is the only thing that crosses the seam. This repo owns every
  template and every line of CSS `/fieldnotes` renders with; Orient sends markdown and metadata,
  never markup.

**A post never needs a PR merge.** Publishing a post is an action in the Writing Desk, not a
commit here. The only things that land as PRs against this repo are changes to the site's own
build, templates, or styling.

**The accepted coupling:** the site build depends on the Orient endpoint being reachable at
build time. Pages output is static, so an Orient outage means new posts can't be published until
it's back — never that the site itself goes down. The build is written to fail loudly rather than
paper over that outage; see the corpus contract below.

## Content contract — what Orient's endpoint actually serves

Orient is the store of truth for Field Notes; this section describes the shape of its deployed
`/api/blog/corpus` response, which this build validates against. `ORIENT_FIELDNOTES_URL` must
return a JSON object of this shape:

```json
{
  "posts": [
    {
      "slug": "example-post",
      "title": "Example Post",
      "date": "2026-08-01",
      "updated": "2026-08-05",
      "author": "Jay Porter",
      "authorship": "human",
      "body": "# Example Post\n\nBody text in markdown..."
    }
  ],
  "count": 1
}
```

- `date` and `updated` are ISO 8601 (`YYYY-MM-DD`, or a full timestamp — only the date portion is
  used for display). `updated` is optional; when present it's shown as "updated \<date\>" next to
  the post's byline.
- Posts are sorted newest-first by `date` at build time — the endpoint does not need to
  pre-sort, but should not assume the build will use its ordering either.
- `authorship` is server-attested and is one of `"human"`, `"collab"`, or `"llm"` — rendered as a
  small byline mark (Human / Collab / LLM) next to the date. Any other value fails the build (see
  below) rather than rendering silently wrong.
- `body` is the post's markdown source. (Not `body_markdown` — Orient's deployed response uses the
  shorter field name.)
- `count`, when present, must equal `len(posts)`. A mismatch fails the build — this is the guard
  against a response silently truncated somewhere upstream.
- `slug` becomes the URL: `/fieldnotes/<slug>/`.

This is the interface Orient's pull endpoint builds against. Treat a change to this shape as a
breaking change to both sides.

### Build behavior against the corpus

- **`ORIENT_FIELDNOTES_URL` unset** — the build succeeds and renders an empty-state Field Notes
  index ("Field Notes is moving in — posts will appear here."). This is the expected state before
  Orient's endpoint and the pipeline tokens exist.
- **`ORIENT_FIELDNOTES_URL` set, and the fetch fails, times out, or returns anything that doesn't
  match the contract above** — the build **exits non-zero** with a specific error naming what was
  wrong. It never falls back to an empty index in this case. A configured endpoint that silently
  yields an empty blog is the failure mode this pipeline is built to refuse — it would look like a
  successful deploy of nothing, and nobody would notice until a reader did.
- **Auth headers.** The deployed endpoint sits behind two walls: a Cloudflare Access service token
  at the edge, and an Orient-issued bearer token at the origin. When `ORIENT_FIELDNOTES_URL` is
  set, all three credentials below are **required** — the build exits 1, naming exactly which is
  missing, if any is absent:
  - `CF_ACCESS_CLIENT_ID` → header `CF-Access-Client-Id`
  - `CF_ACCESS_CLIENT_SECRET` → header `CF-Access-Client-Secret`
  - `ORIENT_BLOG_TOKEN` → header `Authorization: Bearer <token>`

## Repo layout

```
build/build.py            the entire builder — reads templates/ + content/ + the corpus, writes _site/
content/home.md            the home page's markdown source (Jay's copy, edited via PR)
templates/                 base.html, home.html, fieldnotes_index.html, post.html — Jinja2
static/style.css           all styling; no build step, no framework, no JS
.github/workflows/build-deploy.yml   the Actions pipeline
requirements.txt           markdown, jinja2, requests — pinned, nothing else
```

### Previewing a PR

Every PR against `main` builds the site (no deploy, no Orient secrets needed) and uploads the
result as an artifact. To view it: open the PR's checks, find the `preview` job, and download the
`site-preview` artifact. Unzip it, then **serve the folder rather than double-clicking
`index.html`** — the site's links are root-relative, so opening the file directly (a `file://`
URL) breaks `/fieldnotes/` links. From the unzipped directory:

```
python3 -m http.server
```

then open `http://localhost:8000/` in a browser.

Run it locally with:

```
pip install -r requirements.txt
python build/build.py
```

Output goes to `_site/` (gitignored — recreated by every build; add a `.gitignore` if one isn't
present when this lands on `main`).

## Activation checklist — what Jay does to turn this on

This branch is source, not yet live. To activate:

1. **Promote `site-src` to `main`.** Rename or merge this branch to `main`, and set `main` as the
   repo's default branch.
2. **Set Pages source to "GitHub Actions"** in repo Settings → Pages (currently building from
   `gh-pages` via the legacy workflow — leave `gh-pages` alone until this repo's new pipeline is
   verified live, then it can be deleted).
3. **Confirm the custom domain.** The current `gh-pages` tree carries a `CNAME` for `convivy.com`;
   this build also writes that `CNAME` into `_site/` on every build, but Settings → Pages →
   Custom domain should be (re)confirmed once Pages source changes.
4. **Mint and add the pipeline credentials**, per the runbook at
   `company/runbooks/mint-fieldnotes-pipeline-tokens` in the knowledge repo:
   - Actions **variable** `ORIENT_FIELDNOTES_URL` — the corpus pull endpoint. The runbook notes
     the exact path is still being confirmed with the Orient co (candidate:
     `team.convivy.com/api/fieldnotes/*`).
   - Actions **secrets** `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` — the Cloudflare
     Access service token scoped to that endpoint only.
   - Actions **secret** `ORIENT_BLOG_TOKEN` — the Orient-issued bearer token the origin checks
     behind Cloudflare Access. All three of these credentials are required together; the build
     exits 1, naming exactly which is missing, if `ORIENT_FIELDNOTES_URL` is set without all
     three.
   - The runbook flags that the GitHub-side dispatch token (Orient → this repo's
     `repository_dispatch`) has an open pre-check against an uncommitted platform decision about
     routing all `gh` calls through the Bosun. Until that resolves, the daily scheduled build in
     this workflow is the fallback path — new posts go live on the next scheduled build rather
     than instantly on publish. Nothing here needs to change when that resolves; it only affects
     whether Orient's dispatch token gets minted.
5. **Ingest the Field Notes corpus into Orient's store.** As of this scaffold, Orient does not
   yet hold the posts — they're still files in the (retiring) `convivy-lab` repo. This build will
   correctly show the empty state until that ingest happens and the endpoint above is live; it
   will hard-fail instead of silently showing nothing if the endpoint is configured before the
   ingest is done and the endpoint returns something malformed, so ingest and endpoint-configuration
   should land together.

Nothing else is left undone in the scaffold itself — the four templates, the corpus
fetch/validate/error path, and the workflow's triggers (`push` to `main`,
`repository_dispatch` for `fieldnotes-publish`, `workflow_dispatch`, and the daily schedule) are
all in place and exercised locally (see the PR / commit message for the local verification
output).
