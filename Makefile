# uv is installed in ~/.local/bin, which make's non-login shell may not have on PATH.
export PATH := $(HOME)/.local/bin:$(PATH)

CLUSTER_NAME   := kubesentinel
KIND_CONFIG    := infra/kind-config.yaml
GITOPS_DIR     ?= ../kubesentinel-gitops
ARGOCD_VERSION ?= v3.5.5

.PHONY: cluster down status test argocd argocd-status

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

# Install Argo CD (core, no UI) and hand it the GitOps repo. After this, every
# deploy is a commit to kubesentinel-gitops; the demo app appears on its own.
# The applicationset controller is scaled to zero to save memory; we do not use it.
argocd:
	kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/$(ARGOCD_VERSION)/manifests/core-install.yaml
	kubectl wait --for=condition=established --timeout=60s crd/applications.argoproj.io crd/appprojects.argoproj.io
	kubectl -n argocd scale deployment/argocd-applicationset-controller --replicas=0
	kubectl apply -f $(GITOPS_DIR)/argocd/

argocd-status:
	kubectl -n argocd get applications
	kubectl -n demo get pods
