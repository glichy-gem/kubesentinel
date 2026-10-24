#!/usr/bin/env bash
# Design doc section 8.6: prove what the agent's ServiceAccount can and cannot
# do, against the live cluster. Every "no" is a claim in docs/threat-model.md.
set -uo pipefail

SA="system:serviceaccount:kubesentinel:kubesentinel-agent"
failures=0

check() { # check <yes|no> <kubectl auth can-i arguments...>
  local want=$1
  shift
  local got
  got=$(kubectl auth can-i "$@" --as="$SA" 2>/dev/null)
  if [ "$got" = "$want" ]; then
    printf '  ok    %-3s %s\n' "$got" "$*"
  else
    printf '  FAIL  %-3s %s (expected %s)\n' "${got:-?}" "$*" "$want"
    failures=$((failures + 1))
  fi
}

echo "What the agent needs:"
check yes get pods -n demo
check yes delete pods -n demo
check yes get events -n demo
check yes get deployments -n demo
check yes get applications.argoproj.io -n argocd
check yes patch applications.argoproj.io/orders-api -n argocd
check yes patch applications.argoproj.io/redis -n argocd

echo "What it must never do:"
check no delete namespaces
check no delete namespace/demo
check no create roles -n demo
check no patch rolebindings -n kubesentinel
check no create clusterrolebindings
check no get secrets -n demo
check no get secrets -n kubesentinel
check no patch deployments -n demo
check no delete deployments -n demo
check no create pods -n demo
check no create pods/exec -n demo
check no delete pods -n kube-system
check no patch applications.argoproj.io/kubesentinel-agent -n argocd
check no delete applications.argoproj.io/orders-api -n argocd
check no create applications.argoproj.io -n argocd

if [ "$failures" -gt 0 ]; then
  echo "$failures RBAC check(s) failed"
  exit 1
fi
echo "all RBAC checks passed"
