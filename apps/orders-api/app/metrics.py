from prometheus_client import Counter, Histogram

# Labels use the route template (e.g. /orders/{order_id}), never the raw URL,
# so the number of time series stays bounded.
REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
)
ERRORS = Counter(
    "http_request_errors_total",
    "HTTP requests that ended in a 5xx response.",
    ["method", "path"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
