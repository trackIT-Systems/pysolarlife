"""What differs between the operating systems: default paths and the way CLI and daemon talk."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TCP_PORT = 47821


@dataclass(frozen=True)
class Defaults:
    config: Path
    state_dir: Path
    socket: Path | str  # a path (Unix socket) or "tcp://127.0.0.1:port"
    data_dir: Path


def default_paths(platform: str | None = None, env: dict[str, str] | None = None, home: Path | None = None) -> Defaults:
    """Linux (tsOS): /etc/solarlife, /var/lib, /run. macOS: ~/Library/Application Support. Windows: %PROGRAMDATA%."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform.startswith("win"):
        base = Path(env.get("PROGRAMDATA") or (home or Path.home()) / "AppData" / "Local") / "solarlife"
        return Defaults(base / "solarlife.yml", base, f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}", base / "data")
    if platform == "darwin":
        base = (home or Path.home()) / "Library" / "Application Support" / "solarlife"
        return Defaults(base / "solarlife.yml", base, base / "solarlife.sock", base / "data")
    return Defaults(
        Path("/etc/solarlife/solarlife.yml"), Path("/var/lib/solarlife"), Path("/run/solarlife/solarlife.sock"), Path("/data")
    )


DEFAULTS = default_paths()
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform.startswith("win")
