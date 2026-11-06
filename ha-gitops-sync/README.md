# HA GitOps Sync

Keeps Home Assistant's `/config` and its Gitea repository in step, both ways.

- Deploys a commit of `main` once Gitea Actions has checked it, runs the config check on the placed files, and puts
  them back if it fails.
- Commits edits made in Home Assistant and pushes them to the repository.
- Reloads or restarts only when asked, and only after the config check passes.
- A GitOps panel in the sidebar shows what is live and what is waiting, with two switches and the buttons.

The Documentation tab explains the setup and the options.
