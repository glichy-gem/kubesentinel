# 1. Use kind for the local cluster

Date: 2026-10-19
Status: Accepted

## Context

The project needs a real Kubernetes cluster that runs on a laptop with 8 GB of RAM and no GPU, that can be destroyed and rebuilt quickly, and that failures can be injected into repeatedly.

## Decision

Use **kind** (Kubernetes in Docker) with a single control-plane node, configured by `infra/kind-config.yaml`.

## Alternatives considered

- **minikube**: heavier by default and more features than needed.
- **k3d / k3s**: lighter, but kind tracks upstream Kubernetes more closely and is widely used in CI.
- **A cloud cluster**: realistic but costs money and slows the edit-test loop.

## Consequences

- One node means no scheduling across nodes, so multi-node failure modes are not covered.
- The node is a Docker container, so host ports must be published through `extraPortMappings` and services exposed as NodePorts.
- The cluster is cheap to delete and recreate, which suits repeatable chaos experiments.
