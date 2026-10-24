# uv is installed in ~/.local/bin, which make's non-login shell may not have on PATH.
export PATH := $(HOME)/.local/bin:$(PATH)

CLUSTER_NAME   := kubesentinel
KIND_CONFIG    := infra/kind-config.yaml
GITOPS_DIR     ?= ../kubesentinel-gitops
GITOPS_REPO    ?= glichy-gem/kubesentinel-gitops
ARGOCD_VERSION ?= v3.5.5

# Observability stack, pinned so a rebuild gives the same cluster.
KPS_VERSION    ?= 92.10.1
LOKI_VERSION   ?= 7.3.0
ALLOY_VERSION  ?= 1.14.0

# Tools run as containers so nothing else needs installing (CI uses the same targets).
PROM_IMAGE     ?= prom/prometheus:v3.5.0
YQ_IMAGE       ?= mikefarah/yq:4.47.1
SHELLCHECK_IMAGE ?= koalaman/shellcheck:v0.10.0
RULES_TMP      := $(CURDIR)/.tmp/rules

.PHONY: cluster down status test agent-test agent-secret eval-mode-on eval-mode-off agent-git-key argocd argocd-status stack stack-template stack-status grafana-password rules-test chaos chaos-test

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

# Run the agent unit tests (no cluster needed).
agent-test:
	cd agent && uv run pytest -q

# Copy the keys from .env into the agent's Secret (never into Git), then restart
# the agent so it picks them up. The values never appear on a command line: they
# go through a private temp file, and server-side apply keeps them out of the
# last-applied annotation.
agent-secret:
	@test -f .env || (echo "create .env from .env.example first"; exit 2)
	@kubectl create namespace kubesentinel --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@umask 077; mkdir -p .tmp; \
	sed -n -e 's/^GEMINI_API_KEY=/KS_GEMINI_API_KEY=/p' \
	       -e 's/^TELEGRAM_BOT_TOKEN=/KS_TELEGRAM_BOT_TOKEN=/p' \
	       -e 's/^TELEGRAM_CHAT_ID=/KS_TELEGRAM_CHAT_ID=/p' .env > .tmp/agent.env; \
	kubectl -n kubesentinel create secret generic kubesentinel-agent-secrets \
	  --from-env-file=.tmp/agent.env --dry-run=client -o yaml \
	  | kubectl apply --server-side --field-manager=make-agent-secret -f -; \
	rc=$$?; rm -f .tmp/agent.env; exit $$rc
	@# Deleting the pod (not `rollout restart`) avoids a template change that Argo CD would see as drift.
	-kubectl -n kubesentinel delete pod -l app=kubesentinel-agent --wait=false

# Eval mode for Phase 6: the agent still investigates and records what it would
# do, but sends no Telegram cards and changes nothing (KS_DRY_RUN), and closes
# quiet incidents after 2 minutes so back-to-back runs do not merge.
eval-mode-on:
	kubectl -n kubesentinel create configmap kubesentinel-agent-eval \
	  --from-literal=KS_DRY_RUN=true --from-literal=KS_QUIET_MINUTES_TO_CLOSE=2 \
	  --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n kubesentinel delete pod -l app=kubesentinel-agent
	kubectl -n kubesentinel rollout status deployment/kubesentinel-agent --timeout=120s

eval-mode-off:
	-kubectl -n kubesentinel delete configmap kubesentinel-agent-eval
	kubectl -n kubesentinel delete pod -l app=kubesentinel-agent
	kubectl -n kubesentinel rollout status deployment/kubesentinel-agent --timeout=120s

# Give the agent write access to the GitOps repo, and nothing else: a fresh SSH
# deploy key for that one repo (any older agent key is removed, so running this
# again rotates it). The private key goes straight into a Secret and is shredded
# locally. GitHub's host keys come from its API over TLS, not from ssh-keyscan.
agent-git-key:
	@set -e; umask 077; rm -rf .tmp/gitkey; mkdir -p .tmp/gitkey; \
	trap 'shred -u .tmp/gitkey/id_ed25519 2>/dev/null; rm -rf .tmp/gitkey' EXIT; \
	ssh-keygen -q -t ed25519 -N '' -C kubesentinel-agent -f .tmp/gitkey/id_ed25519; \
	curl -fsS https://api.github.com/meta \
	  | python3 -c 'import json,sys; [print("github.com", k) for k in json.load(sys.stdin)["ssh_keys"]]' \
	  > .tmp/gitkey/known_hosts; \
	for id in $$(gh repo deploy-key list -R $(GITOPS_REPO) --json id,title \
	    --jq '.[] | select(.title == "kubesentinel-agent") | .id'); do \
	  gh repo deploy-key delete "$$id" -R $(GITOPS_REPO); \
	done; \
	gh repo deploy-key add .tmp/gitkey/id_ed25519.pub -R $(GITOPS_REPO) \
	  --title kubesentinel-agent --allow-write; \
	kubectl create namespace kubesentinel --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	kubectl -n kubesentinel create secret generic kubesentinel-agent-git \
	  --from-file=id_ed25519=.tmp/gitkey/id_ed25519 \
	  --from-file=known_hosts=.tmp/gitkey/known_hosts --dry-run=client -o yaml \
	  | kubectl apply --server-side --field-manager=make-agent-git-key -f -

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

# Install the observability stack: Prometheus, Alertmanager, Grafana, Loki, Alloy,
# the dummy webhook receiver, alert rules and dashboards. Needs ~1 GB of free RAM.
# The Grafana admin password is generated once and kept in a Secret, never in Git.
stack:
	kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1 || \
		kubectl -n monitoring create secret generic grafana-admin \
			--from-literal=admin-user=admin \
			--from-literal=admin-password="$$(openssl rand -base64 18 | tr -d '/+=')"
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
	helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
	helm repo update >/dev/null
	helm upgrade --install kps prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) \
		-n monitoring -f infra/helm-values/kube-prometheus-stack.yaml --wait --timeout 15m
	helm upgrade --install loki grafana/loki --version $(LOKI_VERSION) \
		-n monitoring -f infra/helm-values/loki.yaml --wait --timeout 10m
	helm upgrade --install alloy grafana/alloy --version $(ALLOY_VERSION) \
		-n monitoring -f infra/helm-values/alloy.yaml --wait --timeout 5m
	kubectl apply -f infra/monitoring/
	kubectl apply -f infra/alert-rules/kubesentinel-rules.yaml
	kubectl apply --server-side --force-conflicts -k infra/dashboards

# Render every chart with our values and pinned versions, without a cluster.
# Catches typos in values files; CI runs it on every infra change.
stack-template:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
	helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
	helm repo update >/dev/null
	helm template kps prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) -n monitoring -f infra/helm-values/kube-prometheus-stack.yaml >/dev/null
	helm template loki grafana/loki --version $(LOKI_VERSION) -n monitoring -f infra/helm-values/loki.yaml >/dev/null
	helm template alloy grafana/alloy --version $(ALLOY_VERSION) -n monitoring -f infra/helm-values/alloy.yaml >/dev/null
	@echo "all charts render"

stack-status:
	kubectl -n monitoring get pods
	kubectl -n monitoring get prometheusrules,podmonitors

grafana-password:
	@kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Run one failure scenario end to end: make chaos S=oom
# Scenarios live in chaos/scenarios/; results land in chaos/results/.
chaos:
	@test -n "$(S)" || (echo "usage: make chaos S=<scenario>   (scenarios: $$(ls chaos/scenarios | tr '\n' ' '))"; exit 2)
	uv run chaos/run_scenario.py $(S)

# Unit tests for the chaos runner and a consistency check of every scenario folder,
# then shellcheck on the scenario scripts.
chaos-test:
	uv run --with pytest --with pyyaml pytest -q chaos/tests
	docker run --rm -v $(CURDIR):/w -w /w $(SHELLCHECK_IMAGE) --severity=warning chaos/lib.sh $(wildcard chaos/scenarios/*/*.sh)

# Validate the alert rules and run their unit tests with promtool.
rules-test:
	mkdir -p $(RULES_TMP)
	docker run --rm -v $(CURDIR)/infra/alert-rules:/w $(YQ_IMAGE) '.spec' /w/kubesentinel-rules.yaml > $(RULES_TMP)/rules.yaml
	cp infra/alert-rules/tests/rules_test.yaml $(RULES_TMP)/rules_test.yaml
	docker run --rm -v $(RULES_TMP):/w --entrypoint promtool $(PROM_IMAGE) check rules /w/rules.yaml
	docker run --rm -v $(RULES_TMP):/w --entrypoint promtool $(PROM_IMAGE) test rules /w/rules_test.yaml
