from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass


ROLE_LEVEL = {"viewer": 1, "employee": 1, "operator": 2, "manager": 2, "hr": 3, "admin": 4}


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


class ApiKeyAuthorizer:
    """Small API-key RBAC boundary; raw keys are never logged or persisted."""

    def __init__(self, principals: dict[str, Principal]) -> None:
        if not principals:
            raise ValueError("at least one API key is required")
        self._principals = principals

    @classmethod
    def from_env(cls) -> "ApiKeyAuthorizer":
        raw = os.environ.get("SQL_AGENT_API_KEYS")
        if not raw:
            raise RuntimeError(
                "SQL_AGENT_API_KEYS must be a JSON object mapping API keys to {name, role}"
            )
        rows = json.loads(raw)
        principals = {}
        for key, value in rows.items():
            role = value["role"]
            if role not in ROLE_LEVEL:
                raise ValueError(f"invalid role: {role}")
            principals[key] = Principal(value["name"], role)
        return cls(principals)

    def authenticate(self, api_key: str | None, minimum_role: str) -> Principal:
        if minimum_role not in ROLE_LEVEL:
            raise ValueError(f"invalid minimum role: {minimum_role}")
        matched = None
        if api_key:
            for candidate, principal in self._principals.items():
                if hmac.compare_digest(api_key, candidate):
                    matched = principal
                    break
        if matched is None:
            raise PermissionError("invalid or missing API key")
        if ROLE_LEVEL[matched.role] < ROLE_LEVEL[minimum_role]:
            raise PermissionError(f"{minimum_role} role required")
        return matched


class BrowserSessions:
    """Bounded, process-local browser sessions; restarting the API signs users out."""

    def __init__(self, ttl_seconds=28800, capacity=1024):
        import threading
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._entries = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token):
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, principal):
        import secrets
        import time
        with self._lock:
            now = time.monotonic()
            self._entries = {key: value for key, value in self._entries.items() if value[1] > now}
            if len(self._entries) >= self.capacity:
                raise RuntimeError('browser session capacity reached')
            token = secrets.token_urlsafe(32)
            self._entries[self._digest(token)] = (principal, now + self.ttl_seconds)
            return token

    def authenticate(self, token, minimum_role):
        import time
        with self._lock:
            key = self._digest(token or '')
            value = self._entries.get(key)
            if value is None or value[1] <= time.monotonic():
                self._entries.pop(key, None)
                raise PermissionError('invalid or missing browser session')
            principal = value[0]
            if ROLE_LEVEL[principal.role] < ROLE_LEVEL[minimum_role]:
                raise PermissionError(f'{minimum_role} role required')
            return principal

    def revoke(self, token):
        with self._lock:
            self._entries.pop(self._digest(token or ''), None)
