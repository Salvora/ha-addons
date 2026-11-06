# ha-addons

A Home Assistant add-on (app) repository.

| Add-on | What it does |
| --- | --- |
| [HA GitOps Sync](ha-gitops-sync) | Keeps Home Assistant's `/config` and a Git repository in step, both ways: deploys merged changes once CI has checked them, commits edits made in Home Assistant, and reloads or restarts only when asked, after a config check |

## Installing

In Home Assistant, go to **Settings → Apps → Install app**, then **⋮ → Repositories**, and add
`https://github.com/Salvora/ha-addons`. The add-ons then appear in the store; each one's Documentation tab explains
its options.

This GitHub repository is a read-only push mirror of a private Gitea repository, where the add-ons are developed,
checked and built. Every sync overwrites it, so pull requests and issues opened here are not seen.

## Development

- **Checks**, on every pull request (`.gitea/workflows/check.yml`): ruff and pytest in the add-on's base image,
  hadolint, actionlint, shellcheck, Home Assistant's add-on linter, and `python3 -m addons check`:
  - a pull request that changes an add-on folder raises `version` in its `config.yaml` and adds a `## <version>`
    section to its `CHANGELOG.md`;
  - `translations/en.yaml` names and describes every option and port, and nothing else;
  - `image` is a lower-case `ghcr.io` repository without a tag, and `version` is `MAJOR.MINOR.PATCH`.

  Locally, with git and openssl: `pip install -r ci/requirements.txt -r ha-gitops-sync/requirements.txt`, then
  `ruff check .`, `ruff format --check .`, `python3 -m pytest` and
  `PYTHONPATH=ci python3 -m addons check --base origin/main`.
- **Release**, on a merge that changes what the image is built from or `config.yaml` (`.gitea/workflows/image.yml`,
  or Actions → image → Run workflow): kaniko builds the image and pushes `<image>:<version>` to ghcr.io, then the
  Gitea release `<add-on>-v<version>` carries the version's changelog section. A version already on ghcr.io is
  refused. Home Assistant offers the update once the mirror has synced.

## Settings

| Setting | Kind | What it is |
| --- | --- | --- |
| `GHCR_PUSH_USER` | variable | The GitHub account that owns the images |
| `GHCR_PUSH_TOKEN` | secret | Its classic token with only `write:packages` (ghcr.io takes no fine-grained token) |
| `GHCR_MIRROR`, `DOCKERHUB_MIRROR` | organisation variables | Optional pull-through mirrors, bare hosts, for the base images |
| Push mirror | Settings → Mirror settings | To `github.com/Salvora/ha-addons`, with a fine-grained GitHub token that has Contents read and write on that one repository |
| Topic | repository settings | `ci-lint-markdown` |
| Branch protection on `main` | Settings → Branches | No force push, no deletion; the four jobs of check.yml (`python`, `dockerfile`, `workflows`, `addons`) required |

## Runbooks

- image.yml refuses a version already on ghcr.io: raise the version in `config.yaml`, with its changelog section.
- The release step fails: create the release by hand, as its error says.
- The first push of a new image: the package is private on ghcr.io; make it public once, so Home Assistant can pull it.
