# HA GitOps Sync

Keeps `/config` and its Gitea repository in step, both ways. The branch `main` holds the config you want; `live` is
what Home Assistant runs, and only the add-on writes it. A deploy places a checked commit of `main` in `/config`, an
edit made in Home Assistant is committed and pushed, and reload and restart happen only when asked.

## Setup

1. Put `/config` on the repository's `main`, with `.gitops/repository-only` listing the paths that stay in the
   repository, one per line (such as `.gitea/` and `ci/`). Remove any nested repository from `/config`, such as
   `esphome/.git`: the add-on does not start while one exists.
2. Give the add-on its own Gitea account with write access to the repository, and create a token for it.
3. Fill in the options and start the add-on. The first start connects `/config` to the repository: it adds the
   repository's `.gitignore`, `.gitattributes` and `.gitops/` when `/config` lacks them, and changes no other file.
   Both switches start paused. On the add-on's Info tab, switch on **Show in sidebar** for the GitOps panel, or use
   **Open Web UI**.
4. In the panel, switch **Capture** on: edits made since the upload arrive as its first commit. Then switch
   **Auto-deploy** on.
5. For Gitea Actions: store `deploy_key` and `control_key` as the repository's secrets `HA_GITOPS_DEPLOY_KEY` and
   `HA_GITOPS_CONTROL_KEY`, put the runner's address in `allowed_sources`, and let it reach port 8765. The API serves
   HTTPS with `/ssl/fullchain.pem` and `/ssl/privkey.pem`, and the add-on does not start without them.

## Options

| Option | Meaning |
| --- | --- |
| `gitea_url` | Your Gitea, such as `https://git.example.org` |
| `repository` | The config repository, as `owner/name` |
| `gitea_token` | The add-on account's token. It is passed to git per command, never stored in `/config/.git` |
| `deploy_key` | Signs deploy requests. At least 32 characters: `openssl rand -hex 32` |
| `control_key` | Signs check, reload and restart requests. At least 32 characters, and not the deploy key |
| `allowed_sources` | Addresses or CIDR networks that may call the API. Empty allows no caller |
| `log_level` | `debug`, `info`, `warning` or `error` |

Fixed: the branches `main` and `live`; commits by `Home Assistant`; a capture after 60 seconds without edits; new
files over 20 MB are not committed; a drift check at start and daily; a deploy needs the commit status `ha/checks`
to be `success`.

## The panel

**GitOps** in the sidebar, for administrators. It shows what is live, what `main` has waiting with its checks and
what it needs, edits not pushed yet, files left out, and the `live → main` pull request. `main` is fetched each time
the page opens. While an action runs, the page refreshes itself until the result is in **Recent**, which keeps the last
50 with who asked and links to Gitea.

- **Auto-deploy** paused: merged pull requests wait; a manual deploy still runs.
- **Capture** paused: edits stay uncommitted, and deploys are refused until it is on again.
- **Deploy** only when the checks passed, **Check config**, **Reload** a target, and **Restart…** after a
  confirmation. Reload and restart run only when the config check passes.

## What a deploy does

1. Refuses a commit that is not on `main`, or whose checks have not passed.
2. Commits edits waiting in Home Assistant first; with capture paused, it refuses instead.
3. Places the files: a fast-forward, or a merge with Home Assistant's edits. A conflict is refused and `/config`
   stays untouched; resolve it as under **When something goes wrong**.
4. Runs the Supervisor's config check on the placed files. If it fails, or Home Assistant does not answer, the files
   are put back. If the add-on stops before the check ends, it puts them back when it starts again.
5. Pushes `live`, and `main` too when the deploy merged; sets the commit status `ha/deploy`; fires `gitops_deployed`.
   It never reloads or restarts.

Paths in `.gitops/repository-only` are never written to `/config`.

## What a capture does

An edit in `/config`, from the UI, Samba or an editor, is committed 60 seconds after the last change. When `main` is
where `live` was, the commit goes to both; otherwise it goes to `live` and the `live → main` pull request, which the
next deploy merges. While Gitea does not answer, commits wait and the push is retried. What `.gitignore` excludes,
new files over 20 MB, repository-only paths and nested repositories are left out. After a backup restore, the
restored files are committed on top of `live`.

## The API for Gitea Actions

HTTPS on port 8765, each request signed with the deploy or control key; the config repository's workflows call it. A
request that is too old, replayed, signed with the other key or from a source not in `allowed_sources` gets 401.

## Events

`gitops_deployed`, `gitops_deploy_failed`, `gitops_captured` and `gitops_conflict` fire on Home Assistant with
`commit`, `files`, `impact` and `by` where they apply.

## When something goes wrong

- `/config/.git is on …, not live`: another setup owns `/config/.git`; remove it, then start the add-on.
- `nested repositories in /config`: delete them, such as `esphome/.git`.
- `cannot fetch the repository`: check `gitea_url`, `repository` and the token.
- `the repository has no main branch`: upload `/config` to it first.
- A deploy refused as `dirty`: switch **Capture** on, then deploy again.
- A deploy refused as `conflict`: on a branch from `live`, merge `main` and resolve the conflict, then merge that
  branch into `main` with a merge commit, not squash or rebase; that merge deploys.
- A merge deploy answered `paused`: switch **Auto-deploy** on, or deploy it by hand in the panel.
