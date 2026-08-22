# see.io site instructions

This repository IS the website at https://bold-acorn-68.s3.seeiousercontent.com, hosted by see.io.
A commit on `main` is the deploy: pushing to the see.io remote builds the
site and puts it live. Pushing is the only call you make. see.io reviews
each pushed commit before it deploys, and that review usually changes
nothing; when a commit would not deploy, see.io commits the smallest fix
that makes it deployable.

see.io writes this file and refreshes it at the start of every see.io build
session, so treat it as read only. Local edits to it are replaced. Everything
else in this repository is yours.

## The site contract

Every see.io site repository MUST satisfy this contract:

1. A `Dockerfile` at the repository root that builds an image serving plain
   HTTP on port 8080 (no TLS: the platform terminates it).
2. A `seeio.json` at the repository root:
       {"name": "<human site name>", "healthcheck": "/<path>"}
   The healthcheck path must answer HTTP 200 as soon as the container is up.
3. Persistence: at runtime the container gets a volume mounted at `/data`,
   the ONLY path that survives deploys and rollbacks. Anything stateful
   (databases, uploads, caches worth keeping) must live under /data, e.g.
   SQLite at /data/app.db. Builds and build sessions see /data empty or
   absent, so the app must initialize it on first boot; never assume it is
   pre-populated and never store state anywhere else.

## Shipping a change

    git pull --rebase
    git add -A && git commit -m "<what changed>"
    git push origin main

Only `main` deploys. Other branches are accepted and never deployed, so use
them freely for work in progress. Force pushes to `main` are rejected: push a
revert commit instead of rewriting history. A push is refused while a see.io
build session is running for this site, and the review of your own previous
push is one of those sessions, so a refused push usually just means the last
one is still being reviewed. Wait for it to finish, pull, and push again.

## Checking deploy status

A push that succeeded is not yet a live site: the image build and the deploy
run afterwards. Poll the status endpoint every 5 seconds until the state
settles (a first deploy can take up to 3 minutes while the site's server is
provisioned):

    curl -sS https://see.io/api/v1/agent/status \
      -H "Authorization: Bearer $SEEIO_TOKEN"

The token is scoped to this one site and is minted in the see.io dashboard,
on the site's Connect tab. It is a secret: keep it in your shell environment,
never in this repository.

Act on `state` in the response:

  - "live": done. The site is serving at the returned `site_url`.
  - "verifying": see.io is reading your commit before it deploys. Keep
    polling, and do not push again until this clears.
  - "deploy_failed": read `deploy.failure_code` and `deploy.failure_detail`
    (and the full log at `deploy.log_url`), fix the project so it satisfies
    the contract above, then commit and push again.
  - any other state: keep polling.

## House rules

- Copy style: never use the em dash character in anything a visitor can read,
  including headlines, body text, buttons, alt text and metadata. Use a
  comma, a colon, parentheses, or two sentences instead.
- Changing a CSS or JS file that pages link by a fixed name may not reach
  visitors who already loaded the site, because browsers reuse those files
  across refreshes. Change the URL in the same commit, either by bumping a
  version query (style.css?v=5) or by renaming the file with a content marker
  (app.4f2a1c.css), and update every page that links it.
- `/data` is the only path that survives a deploy or a rollback. Everything
  else in the running container is rebuilt from this repository every time.
- Keep a short `.seeio/NOTES.md`: confirmed facts and decisions (for example
  "hours are Tue-Sun 8am-4pm, owner confirmed") plus open questions. Read it
  before you start and update it whenever something is decided. It can be
  publicly reachable on the deployed site, so business facts only, nothing
  sensitive.
- Do not commit secrets. Everything pushed here becomes part of the site's
  permanent history, and files at the repository root can be served by the
  site's own Dockerfile.
