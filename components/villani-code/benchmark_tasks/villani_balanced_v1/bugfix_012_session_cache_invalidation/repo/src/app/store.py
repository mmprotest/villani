from __future__ import annotations

class UserStore:
    def __init__(self):
        self._roles = {}
        self._permission_cache = {}

    def set_role(self, user_id: str, role: str) -> None:
        self._roles[user_id] = role
        self._permission_cache.setdefault(user_id, self._permissions_for(role))

    def permissions_for(self, user_id: str) -> list[str]:
        if user_id not in self._permission_cache:
            role = self._roles.get(user_id, 'guest')
            self._permission_cache[user_id] = self._permissions_for(role)
        return list(self._permission_cache[user_id])

    def _permissions_for(self, role: str) -> list[str]:
        mapping = {
            'guest': ['read'],
            'editor': ['read', 'write'],
            'admin': ['read', 'write', 'delete'],
        }
        return mapping[role]
