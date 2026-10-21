from prometheus_client import Counter, Gauge

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
