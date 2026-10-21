from prometheus_client import Counter, Gauge, Histogram

ALERTS_RECEIVED = Counter(
    "kubesentinel_alerts_received_total",
    "Alert entries received from Alertmanager, repeats included",
    ["status"],
)
INCIDENT_EVENTS = Counter(
    "kubesentinel_incident_events_total",
    "Incident lifecycle changes",
    ["event"],
)
INCIDENTS = Gauge(
    "kubesentinel_incidents",
    "Incidents currently stored, by status",
    ["status"],
)
DIAGNOSES = Counter(
    "kubesentinel_diagnoses_total",
    "Diagnoses produced, by who produced them (llm or matcher fallback) and root cause",
    ["source", "root_cause"],
)
ACTIONS = Counter(
    "kubesentinel_actions_total",
    "Remediation actions by type and outcome",
    ["type", "outcome"],
)
INVESTIGATION_SECONDS = Histogram(
    "kubesentinel_investigation_seconds",
    "Time from starting evidence collection to a stored diagnosis",
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
)
