import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


def normalize_email(raw: str) -> str:
    return str(raw or "").strip().lower()


def email_domain(email_addr: str) -> str:
    email_addr = normalize_email(email_addr)
    if "@" not in email_addr:
        return ""
    return email_addr.split("@", 1)[1]


class ManagedAccountStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _append_event(self, row: dict[str, Any]) -> None:
        os.makedirs(self.path.parent or ".", exist_ok=True)
        with open(self.lock_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False))
                    f.write("\n")
                try:
                    os.chmod(self.path, 0o600)
                except Exception:
                    pass
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.lock_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(row, dict):
                            rows.append(row)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return rows

    def latest_accounts(self) -> dict[str, dict[str, Any]]:
        accounts: dict[str, dict[str, Any]] = {}
        for row in self._load_rows():
            if str(row.get("kind") or "") != "managed_account":
                continue
            email_addr = normalize_email(str(row.get("email") or ""))
            if not email_addr:
                continue
            current = dict(accounts.get(email_addr) or {})
            for key, value in row.items():
                if key == "password" and not value and current.get("password"):
                    continue
                current[key] = value
            accounts[email_addr] = current
        return accounts

    def get(self, email_addr: str) -> dict[str, Any]:
        return dict(self.latest_accounts().get(normalize_email(email_addr)) or {})

    def record_domain_success(self, *, email_addr: str, domain: str, password: str, account_id: int) -> None:
        self._append_event(
            {
                "kind": "managed_account",
                "at": time.time(),
                "source": "domain",
                "email": normalize_email(email_addr),
                "domain": (domain or email_domain(email_addr)).strip().lower(),
                "password": password,
                "last_account_id": int(account_id or 0),
            }
        )

    def record_tempmail_success(self, *, email_addr: str, account_id: int) -> None:
        self._append_event(
            {
                "kind": "managed_account",
                "at": time.time(),
                "source": "tempmail",
                "email": normalize_email(email_addr),
                "domain": email_domain(email_addr),
                "last_account_id": int(account_id or 0),
            }
        )

    def record_duck_success(self, *, email_addr: str, password: str, account_id: int) -> None:
        self._append_event(
            {
                "kind": "managed_account",
                "at": time.time(),
                "source": "duck",
                "email": normalize_email(email_addr),
                "domain": email_domain(email_addr),
                "password": password,
                "last_account_id": int(account_id or 0),
            }
        )
