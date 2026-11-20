"""Configuração carregada de variáveis de ambiente (nada de constantes espalhadas pelo código)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_URGENT_WINDOW = 30.0  # minutos até o prazo para um pedido ser "urgente"
_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Configurações da aplicação.

    Variáveis de ambiente: TASKMIND_DB_PATH, TASKMIND_DATA_DIR, TASKMIND_LOG_LEVEL,
    TASKMIND_LOG_JSON (0/1) e TASKMIND_URGENT_WINDOW (minutos).
    """

    db_path: Path
    data_dir: Path
    log_level: str
    log_json: bool
    urgent_window_minutes: float

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """Lê as configurações do ambiente (ou de um mapeamento passado, útil em testes)."""
        env = os.environ if env is None else env
        return cls(
            db_path=Path(env.get("TASKMIND_DB_PATH", _ROOT / "taskmind.db")),
            data_dir=Path(env.get("TASKMIND_DATA_DIR", _ROOT / "data")),
            log_level=env.get("TASKMIND_LOG_LEVEL", "INFO").upper(),
            log_json=env.get("TASKMIND_LOG_JSON", "1").lower() not in ("0", "false", "no"),
            urgent_window_minutes=float(env.get("TASKMIND_URGENT_WINDOW", DEFAULT_URGENT_WINDOW)),
        )
