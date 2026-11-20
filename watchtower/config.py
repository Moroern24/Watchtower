"""Settings read from environment variables (see README)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))
    model: str = field(default_factory=lambda: os.environ.get("WATCHTOWER_MODEL", "claude-opus-5-5"))
    # USD per million tokens for cost accounting (defaults: Claude Opus 5.5 list price)
    price_in: float = field(default_factory=lambda: _float("MODEL_PRICE_INPUT_PER_MTOK", 4.0))
    price_out: float = field(default_factory=lambda: _float("MODEL_PRICE_OUTPUT_PER_MTOK", 20.0))
    read_interval_min: int = field(default_factory=lambda: _int("READ_INTERVAL_MIN", 30))
    repair_cooldown_min: int = field(default_factory=lambda: _int("REPAIR_COOLDOWN_MIN", 120))
    min_request_interval: float = field(default_factory=lambda: _float("MIN_REQUEST_INTERVAL_SEC", 1.05))
    max_concurrent_runs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_RUNS", 8))
    max_tenders: int = field(default_factory=lambda: _int("MAX_TENDERS_PER_PORTAL", 500))
    max_file_bytes: int = field(default_factory=lambda: _int("MAX_FILE_BYTES", 250 * 1024 * 1024))
    max_docs_per_tender: int = field(default_factory=lambda: _int("MAX_DOCS_PER_TENDER", 40))
    http_timeout: float = field(default_factory=lambda: _float("HTTP_TIMEOUT_SEC", 60.0))
    block_private_networks: bool = field(
        default_factory=lambda: os.environ.get("BLOCK_PRIVATE_NETWORKS", "0") == "1")
    use_browser: bool = field(default_factory=lambda: os.environ.get("USE_BROWSER", "1") == "1")
    scheduler_tick_sec: float = field(default_factory=lambda: _float("SCHEDULER_TICK_SEC", 20.0))
    user_agent: str = field(default_factory=lambda: os.environ.get(
        "USER_AGENT", "Mozilla/5.0 (compatible; WatchtowerBot/0.1; tender monitoring)"))

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "watchtower.db"


settings = Settings()
