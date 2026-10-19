# 2. Run Docker Engine inside WSL2 instead of Docker Desktop

Date: 2026-10-19
Status: Accepted

## Context

Development happens on Windows 11 with about 7.7 GB of RAM. The full stack needs roughly 3.4 GB inside the cluster, so every idle background service matters.

## Decision

Install **Docker Engine directly inside Ubuntu 24.04 on WSL2**, cap WSL2 at 4 GB through `.wslconfig`, and keep all code in the Linux filesystem.

## Alternatives considered

- **Docker Desktop with the WSL2 backend**: easier setup, but runs its own VM and uses extra memory.

## Consequences

- Lower memory use and the same environment as Linux CI.
- Docker is managed by systemd inside Ubuntu, not by a Windows application.
- Code must live under `~/projects` in Linux, not under `/mnt/c`, because cross-filesystem I/O is much slower.
