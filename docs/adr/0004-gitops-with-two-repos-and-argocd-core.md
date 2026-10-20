# 4. GitOps with two repositories and Argo CD core

Date: 2026-10-20
Status: Accepted

## Context

The agent needs a trustworthy history of what changed before an incident, and rollbacks must be safe and auditable. CI must be able to publish a new version without holding credentials to the cluster. The laptop has very little spare memory.

## Decision

- Keep **application code** in `kubesentinel` and **Kubernetes manifests** in `kubesentinel-gitops`.
- CI builds, scans and pushes an image tagged `sha-<short commit>` to GHCR, then commits that tag to the gitops repo. The commit message records the source commit, the person who triggered it and the run URL.
- **Argo CD** (core install, no UI or API server) watches the gitops repo and applies it, with auto-sync, prune and self-heal.
- CI authenticates to the gitops repo with a **deploy key** limited to that one repository, stored as a GitHub secret.

## Alternatives considered

- **One repository**: simpler, but deploy commits mix with code commits and CI's own commit would re-trigger CI.
- **CI runs `kubectl apply`**: needs cluster credentials in GitHub and leaves no Git record of what is running.
- **Personal access token for CI**: works, but carries the permissions of the whole account.
- **Full Argo CD install**: adds a UI and API server that cost memory we do not have.

## Consequences

- Every deploy is a commit, so "what changed before the incident" is a `git log`, and a rollback is a `git revert`.
- Manual cluster edits are reverted by Argo CD within seconds (verified by scaling a deployment by hand).
- The core install has no API server, so it does not create the `default` Argo project; it is defined in `argocd/project-default.yaml`.
- Anyone who can merge to `main` can trigger a deploy. Acceptable for a solo project, to be tightened for a team.
- Actions are pinned by version tag, not commit hash.
