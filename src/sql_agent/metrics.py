from __future__ import annotations

import re
import threading
from collections import Counter


JOB_PATH = re.compile(r"^/v1/jobs/[^/]+(?:/(events|review|replay))?$")
STATIC_PATHS = {'/', '/health', '/benchmark', '/metrics', '/v1/tasks', '/v1/jobs',
                '/v1/capabilities', '/v1/operations', '/docs', '/redoc', '/openapi.json'}


def normalized_path(path: str) -> str:
    match = JOB_PATH.fullmatch(path)
    if match:
        return '/v1/jobs/{job_id}' + ('/' + match[1] if match[1] else '')
    if re.fullmatch(r'/v1/traces/[^/]+', path):
        return '/v1/traces/{trace_id}'
    return path if path in STATIC_PATHS else '/unmatched'


class HttpMetrics:
    BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = Counter()
        self._durations = {}

    def observe(self, method: str, path: str, status: int, seconds: float) -> None:
        method = method if method in {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'} else 'OTHER'
        key = (method, normalized_path(path), str(status) if 100 <= status <= 599 else 'other')
        with self._lock:
            self._requests[key] += 1
            histogram = self._durations.setdefault(key, {'buckets': [0] * len(self.BUCKETS), 'count': 0, 'sum': 0.0})
            histogram['count'] += 1
            histogram['sum'] += seconds
            for i, bound in enumerate(self.BUCKETS):
                histogram['buckets'][i] += seconds <= bound

    @staticmethod
    def _labels(key) -> str:
        method, path, status = key
        return f'method="{method}",path="{path}",status="{status}"'

    def render(self, job_counts: dict[str, int]) -> str:
        lines = [
            "# HELP sql_agent_http_requests_total HTTP requests by method, normalized path, and status.",
            "# TYPE sql_agent_http_requests_total counter",
        ]
        with self._lock:
            for key, count in sorted(self._requests.items()):
                labels = self._labels(key)
                lines.append(f"sql_agent_http_requests_total{{{labels}}} {count}")
            lines += [
                "# HELP sql_agent_http_request_duration_seconds HTTP request latency.",
                "# TYPE sql_agent_http_request_duration_seconds histogram",
            ]
            for key, values in sorted(self._durations.items()):
                labels = self._labels(key)
                for bucket, count in zip(self.BUCKETS, values['buckets']):
                    lines.append(f'sql_agent_http_request_duration_seconds_bucket{{{labels},le="{bucket}"}} {count}')
                lines.append(f'sql_agent_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {values["count"]}')
                lines.append(f"sql_agent_http_request_duration_seconds_sum{{{labels}}} {values['sum']}")
                lines.append(f"sql_agent_http_request_duration_seconds_count{{{labels}}} {values['count']}")
        lines += ["# HELP sql_agent_jobs Jobs by current status.", "# TYPE sql_agent_jobs gauge"]
        for status, count in sorted(job_counts.items()):
            lines.append(f'sql_agent_jobs{{status="{status}"}} {count}')
        return "\n".join(lines) + "\n"
