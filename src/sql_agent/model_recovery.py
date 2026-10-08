"""Bounded retries for transient model transport failures and invalid JSON only.

Explicit STOP, schema validation errors and SQL policy failures are not retried.
Each attempt still uses the provider adapter's own finite request timeout.
"""
import json
import socket
import time
import urllib.error

MAX_MODEL_ATTEMPTS = 3
MAX_RETRY_DELAY = 2.0


def retry_delay(exc, attempt):
    delay = 0.25 * 2 ** (attempt - 1)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code not in {429, 502, 503, 504}:
            return None
        value = (exc.headers or {}).get('Retry-After')
        if value is not None:
            try:
                requested = float(value)
            except (ValueError, TypeError):
                # A date or invalid header must not cause an immediate retry.
                return None
            if not 0 <= requested <= MAX_RETRY_DELAY:
                return None
            delay = max(delay, requested)
    elif isinstance(exc, urllib.error.URLError):
        if not isinstance(exc.reason, (TimeoutError, ConnectionError, socket.timeout)):
            return None
    elif not isinstance(exc, (TimeoutError, ConnectionError, json.JSONDecodeError)):
        return None
    return min(delay, MAX_RETRY_DELAY)


def complete_json(model, prompt, events):
    """Retry the identical prompt, never feed a candidate or oracle to the checker."""
    invocation = sum(e['event'] == 'start' for e in events) + 1
    events.append({'event': 'start', 'invocation': invocation, 'attempt_limit': MAX_MODEL_ATTEMPTS})
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        started = time.perf_counter()
        metadata = {}
        try:
            if callable(getattr(model, 'complete_with_metadata', None)):
                raw, metadata = model.complete_with_metadata(prompt)
            else:
                raw = model.complete(prompt)
            result = json.loads(raw)
        except Exception as exc:
            delay = retry_delay(exc, attempt)
            retry = delay is not None and attempt < MAX_MODEL_ATTEMPTS
            events.append({'event': 'failure', 'invocation': invocation, 'attempt': attempt,
                           **call_observation(model, started, metadata),
                           'error_type': type(exc).__name__, 'retry': retry,
                           'delay_seconds': delay if retry else 0})
            if not retry:
                raise
            time.sleep(delay)
        else:
            events.append({'event': 'success', 'invocation': invocation, 'attempt': attempt,
                           **call_observation(model, started, metadata)})
            return result


def call_observation(model, started, metadata):
    """Keep usage and timing, never prompts, credentials, URLs or response text."""
    def count(name):
        value = metadata.get(name) if isinstance(metadata, dict) else None
        return value if type(value) is int and value >= 0 else None
    return {'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
            'adapter': type(model).__name__,
            'model': str(getattr(model, 'model', 'unspecified'))[:128],
            'prompt_tokens': count('prompt_tokens'),
            'completion_tokens': count('completion_tokens'),
            **{target: (count(source)/1_000_000 if count(source) is not None else None)
               for source, target in [('load_duration_ns','model_load_ms'),
                   ('prompt_eval_duration_ns','prompt_eval_ms'), ('eval_duration_ns','generation_ms')]}}
