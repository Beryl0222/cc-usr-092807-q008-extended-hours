from __future__ import annotations

from collections.abc import Iterable

from .errors import PermissionDenied


DEFAULT_ROLES = {
    "administrator": {"*"},
    "auditor": {"*.read", "audit.read"},
    "operator": {"*.create", "*.read", "*.update", "*.transition"},
    "reviewer": {"*.read", "*.update", "*.transition"},
    "viewer": {"*.read"},
}


class PermissionMatrix:
    def __init__(self, grants: dict[str, set[str]] | None = None):
        self.grants = {role: set(values) for role, values in (grants or DEFAULT_ROLES).items()}

    def allow(self, role: str, actions: Iterable[str]) -> None:
        self.grants.setdefault(role, set()).update(actions)

    def permits(self, role: str, action: str) -> bool:
        grants = self.grants.get(role, set())
        if "*" in grants or action in grants:
            return True
        namespace, _, operation = action.partition(".")
        return f"*.{operation}" in grants or f"{namespace}.*" in grants

    def require(self, role: str, action: str) -> None:
        if not self.permits(role, action):
            raise PermissionDenied(f"角色{role}不能执行{action}")
