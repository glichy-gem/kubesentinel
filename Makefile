CLUSTER_NAME := kubesentinel
KIND_CONFIG  := infra/kind-config.yaml

.PHONY: cluster down status

cluster:
	kind create cluster --config $(KIND_CONFIG)

down:
	kind delete cluster --name $(CLUSTER_NAME)

status:
	kubectl get nodes
	kubectl get pods -A
