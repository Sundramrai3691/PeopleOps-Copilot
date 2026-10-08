from sql_agent.metrics import HttpMetrics, normalized_path


def test_metrics_normalize_job_ids_and_render_prometheus():
    assert normalized_path("/v1/jobs/123") == "/v1/jobs/{job_id}"
    assert normalized_path("/v1/jobs/123/events") == "/v1/jobs/{job_id}/events"
    metrics = HttpMetrics()
    metrics.observe("GET", "/v1/jobs/secret-id", 200, 0.012)
    output = metrics.render({"queued": 2})
    assert "secret-id" not in output
    assert 'path="/v1/jobs/{job_id}"' in output
    assert 'sql_agent_jobs{status="queued"} 2' in output


def test_metrics_storage_bounded_across_unique_jobs_and_unknown_urls():
    metrics = HttpMetrics()
    for i in range(10000):
        for suffix in ('events', 'review', 'replay'):
            metrics.observe('POST', f'/v1/jobs/private-{i}/{suffix}', 200, .01)
        metrics.observe('GET', f'/v1/traces/private-{i}', 200, .01)
        metrics.observe(f'CUSTOM-{i}', f'/random/private-{i}', 404, .01)
    assert len(metrics._durations) == 5
    assert all(len(v['buckets']) == len(metrics.BUCKETS) and v['count'] == 10000
               for v in metrics._durations.values())
    output = metrics.render({})
    assert 'private-' not in output and 'CUSTOM-' not in output
    assert 'path="/unmatched"' in output


def test_histogram_boundaries_counts_and_sum():
    m = HttpMetrics()
    for value in (.005, .012, 7):
        m.observe('GET', '/health', 200, value)
    text = m.render({})
    prefix = 'sql_agent_http_request_duration_seconds'
    labels = 'method="GET",path="/health",status="200"'
    assert f'{prefix}_bucket{{{labels},le="0.005"}} 1' in text
    assert f'{prefix}_bucket{{{labels},le="0.025"}} 2' in text
    assert f'{prefix}_bucket{{{labels},le="+Inf"}} 3' in text
    assert f'{prefix}_count{{{labels}}} 3' in text
    assert f'{prefix}_sum{{{labels}}} 7.017' in text
