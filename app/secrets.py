"""Персистентные секреты СУБД: генерируются один раз и живут в /state."""
from __future__ import annotations

import json
import logging
import secrets as pysecrets
from dataclasses import asdict, dataclass

from .constants import STATE_DIR

logger = logging.getLogger("sydafa.secrets")

_SECRETS_FILE = "secrets.json"
_cached: "AppSecrets | None" = None


@dataclass(frozen=True)
class AppSecrets:
    sydafa_password: str
    mssql_sa_password: str
    oracle_sys_password: str


def _alnum_password(prefix: str, nbytes: int = 12) -> str:
    return f"{prefix}{pysecrets.token_hex(nbytes)}"


def _mssql_sa_password() -> str:
    return f"Sa1!{pysecrets.token_urlsafe(16)}"


def _generate() -> AppSecrets:
    return AppSecrets(
        sydafa_password=_alnum_password("Sy"),
        mssql_sa_password=_mssql_sa_password(),
        oracle_sys_password=_alnum_password("Or"),
    )


def get_secrets() -> AppSecrets:
    global _cached
    if _cached is not None:
        return _cached

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / _SECRETS_FILE
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        _cached = AppSecrets(
            sydafa_password=raw["sydafa_password"],
            mssql_sa_password=raw["mssql_sa_password"],
            oracle_sys_password=raw["oracle_sys_password"],
        )
        return _cached

    generated = _generate()
    path.write_text(json.dumps(asdict(generated), indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    logger.info("Сгенерированы секреты СУБД и сохранены в %s", path)
    _cached = generated
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None
