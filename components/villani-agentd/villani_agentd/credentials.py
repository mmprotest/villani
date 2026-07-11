from __future__ import annotations


from .config import AgentdPaths


class InstallationCredentialStore:
    SERVICE = "villani-agentd"

    def __init__(self, paths: AgentdPaths) -> None:
        self.paths = paths

    def set(self, installation_id: str, credential: str) -> str:
        try:
            import keyring

            keyring.set_password(self.SERVICE, installation_id, credential)
            if keyring.get_password(self.SERVICE, installation_id) == credential:
                self.paths.credential_fallback.unlink(missing_ok=True)
                return "os_keyring"
        except Exception:
            pass
        from .lifecycle import write_token

        write_token(self.paths.credential_fallback, credential)
        return "protected_file"

    def get(self, installation_id: str) -> str:
        try:
            import keyring

            value = keyring.get_password(self.SERVICE, installation_id)
            if value:
                return value
        except Exception:
            pass
        value = self.paths.credential_fallback.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("installation credential is not available")
        return value

    def delete(self, installation_id: str) -> None:
        try:
            import keyring

            keyring.delete_password(self.SERVICE, installation_id)
        except Exception:
            pass
        self.paths.credential_fallback.unlink(missing_ok=True)
