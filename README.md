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

- **Orient pushes the post corpus to this repo.** On every publish, Orient writes the full
  published corpus as `corpus.json` to the orphan branch `fieldnotes-corpus`, which holds that one
  file and nothing else, then fires a `repository_dispatch` (event type `fieldnotes-changed`)
  naming the commit it pushed. The build reads `corpus.json` from that branch: at the dispatched
  commit when it is on the branch, and at the branch head for every other build. The build takes
  the full corpus every time, which keeps it idempotent, and nothing outside needs a way into
  Orient. A daily scheduled build is the fallback if a dispatch is ever missed. This branch is the
  production build's only corpus source.
- **The build renders and deploys via Pages-from-Actions** (`actions/upload-pages-artifact` +
  `actions/deploy-pages`) — no `gh-pages` branch, no force-push. That old pattern is what fights
  branch protection; this pipeline doesn't need branch protection worked around because nothing
  but this repo's own sources ever needs to land on `main`.
- **The human publish gate lives upstream, in the Writing Desk.** Jay's publish action there,
  attested via Cloudflare Access, is the moment of human decision. Everything downstream of that
  — the dispatch, the push, the build, the deploy — is mechanics.
- **Styling boundary:** content is the only thing that crosses the seam. This repo owns every
  template and every line of CSS `/fieldnotes` renders with; Orient sends markdown and metadata,
  never markup.

**A post never needs a PR merge.** Publishing a post is an action in the Writing Desk, not a
commit here. The only things that land as PRs against this repo are changes to the site's own
build, templates, or styling.

**The build does not need Orient to be reachable at build time.** A build reads only this repo's
`fieldnotes-corpus` branch, so an Orient outage stops new posts being published, never a build or
the site.

## Content contract — what Orient publishes

Orient is the store of truth for Field Notes. This section describes the shape of `corpus.json` on
the `fieldnotes-corpus` branch, which the build validates against. It must be a JSON object of
this shape:

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
- Posts are sorted newest-first by `date` at build time — `corpus.json` does not need to
  pre-sort, but should not assume the build will use its ordering either.
- `authorship` is server-attested and is one of `"human"`, `"collab"`, or `"llm"` — rendered as a
  small byline mark (Human / Collab / LLM) next to the date. Any other value fails the build (see
  below) rather than rendering silently wrong.
- `body` is the post's markdown source. (Not `body_markdown` — Orient's `corpus.json` uses the
  shorter field name.) Bodies may begin with the title restated as a heading (as in the example
  above); the build strips a leading heading from `body` when its text matches `title`, so the
  templates' own title rendering (`<h1>`/`<h2>`) is never duplicated. A leading heading whose text
  doesn't match `title` is left in place.
- `count`, when present, must equal `len(posts)`. A mismatch fails the build — this is the guard
  against a corpus silently truncated somewhere upstream.
- `slug` becomes the URL: `/fieldnotes/<slug>/`.

This is the interface Orient's push builds against. Treat a change to this shape as a breaking
change to both Orient and this build.

### Build behavior against the corpus

- **`FIELDNOTES_CORPUS_FILE` set** (the workflow sets it from the `fieldnotes-corpus` branch) — it
  is the build's only corpus source. A missing, unreadable or off-contract file exits non-zero,
  naming what was wrong.
- **`FIELDNOTES_CORPUS_FILE` unset** — the build succeeds and renders an empty-state Field Notes
  index ("Field Notes is moving in — posts will appear here."). This is what the preview job
  builds, since it never sets `FIELDNOTES_CORPUS_FILE`.
- **A branch corpus with zero posts** — the build exits non-zero unless the repo variable
  `FIELDNOTES_ALLOW_EMPTY` is `"1"`, set only to unpublish everything on purpose. A branch that
  silently yields an empty blog is the failure mode this pipeline is built to refuse — it would
  look like a successful deploy of nothing, and nobody would notice until a reader did.
- **The `fieldnotes-corpus` branch itself is missing** — the workflow's "Fetch the Field Notes
  corpus branch" step fails the build job before `build.py` ever runs, naming the branch. There is
  no fallback source to build from instead.

## Repo layout

```
build/build.py            the entire builder — reads templates/ + content/ + the corpus, writes _site/
content/home.md            the home page's markdown source (Jay's copy, edited via PR)
templates/                 base.html, home.html, fieldnotes_index.html, post.html — Jinja2
static/style.css           all styling; no build step, no framework, no JS
.github/workflows/build-deploy.yml   the Actions pipeline
requirements.txt           markdown, jinja2 — pinned, nothing else
```

### Previewing a PR

Every PR against `main` builds the site (no deploy, no Orient secrets needed) and pushes the
result to a live URL: **https://preview.convivy.com**. Open the PR, wait for the `preview` job to
finish, then click the preview link from its comment on the PR.

This is one shared preview, not one per PR — whichever PR's `preview` job finishes last is what's
live. If you're looking at someone else's build, wait for yours to finish and refresh.

Since the build runs with no Orient secrets, the preview always renders the empty-state Field
Notes index — there's no corpus behind it.

Run it locally with:

```
pip install -r requirements.txt
python build/build.py
```

Output goes to `_site/` (gitignored — recreated by every build; add a `.gitignore` if one isn't
present when this lands on `main`).

## Credentials the pipeline depends on

The production build job holds no secret. Two fine-grained GitHub tokens make the pipeline run,
both minted and rotated per the runbook `company/runbooks/mint-fieldnotes-pipeline-tokens` in the
knowledge repo:

- **Orient's dispatch token**, held on Orient's side, with Contents read/write on this repo only.
  Orient uses it to push `corpus.json` to `fieldnotes-corpus` and to send the `fieldnotes-changed`
  dispatch. Ruleset 23102478 lets only an OrganizationAdmin create or update that branch, and the
  token's owner carries that exemption. Ruleset 23102477 blocks deleting or force-pushing it.
- **`PREVIEW_DEPLOY_TOKEN`**, a repository secret here, with Contents read/write on
  `convivy/www-preview` only. The preview job uses it to publish PR builds to preview.convivy.com.
