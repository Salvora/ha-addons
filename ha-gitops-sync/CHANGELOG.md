# Changelog

## 1.0.1

- Runs on Python 3.15: Home Assistant's base-python 3.15 image.

## 1.0.0

- First release: deploys checked commits of `main` to `/config`, commits edits made in Home Assistant, and runs
  check, reload and restart from the GitOps panel or from Gitea Actions through the signed API.
