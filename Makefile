# uv is installed in ~/.local/bin, which make's non-login shell may not have on PATH.
export PATH := $(HOME)/.local/bin:$(PATH)

CLUSTER_NAME := kubesentinel
KIND_CONFIG  := infra/kind-config.yaml
GITOPS_DIR   ?= ../kubesentinel-gitops
IMAGE        := orders-api:dev

.PHONY: cluster down status test image app

cluster:
	kind create cluster --config $(KIND_CONFIG)

down:
	kind delete cluster --name $(CLUSTER_NAME)

status:
	kubectl get nodes
	kubectl get pods -A

# Run the orders-api unit tests (no cluster needed).
test:
	cd apps/orders-api && uv run pytest -q

# Build the orders-api image and load it into the kind node.
image:
	docker build -t $(IMAGE) apps/orders-api
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)

# Phase 1 deploy: apply the GitOps repo's manifests directly.
# Phase 2 replaces this with Argo CD syncing the same repo.
app: image
	kubectl apply -k $(GITOPS_DIR)
	kubectl -n demo rollout status deployment/redis --timeout=120s
	kubectl -n demo rollout status deployment/orders-api --timeout=180s
