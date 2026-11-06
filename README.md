# ha-addons

Home Assistant add-ons.

| Add-on | What it does |
| --- | --- |
| ha-gitops-sync (first release in progress) | Keeps Home Assistant's `/config` and a Git repository in step, both ways: deploys merged changes once CI has checked them, commits edits made in Home Assistant, and reloads or restarts only when asked, after a config check |

## Installing

In Home Assistant, open the add-on store, then **⋮ → Repositories**, and add `https://github.com/Salvora/ha-addons`. The add-ons then appear in the store; each one's Documentation tab explains its options.

## This repository is a mirror

The add-ons are developed, checked and built in a private Gitea instance. This GitHub repository is a read-only push mirror of it, and every sync overwrites it, so pull requests and issues opened here are not seen.
