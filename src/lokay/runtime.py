from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("HOME", Path.home()))
DEFAULT_HERMES = Path(
    os.environ.get("HERMES_HOME", DEFAULT_HOME / ".hermes")
)
DEFAULT_FALA_DB = Path(
    os.environ.get(
        "HERMES_LOKAY_FALA_DB",
        DEFAULT_HERMES / "lokay" / "fala" / "state.sqlite",
    )
)
DEFAULT_FALA_ARTIFACTS = Path(
    os.environ.get(
        "HERMES_LOKAY_FALA_ARTIFACTS",
        DEFAULT_HERMES / "lokay" / "fala" / "artifacts",
    )
)
DEFAULT_CONFIG = Path(
    os.environ.get(
        "HERMES_LOKAY_CONFIG",
        DEFAULT_HERMES / "lokay" / "config.toml",
    )
)


def ensure_fala_paths(
    db: Path | None = None,
    artifacts: Path | None = None,
) -> tuple[Path, Path]:
    db_path = (db or DEFAULT_FALA_DB).expanduser()
    art_path = (artifacts or DEFAULT_FALA_ARTIFACTS).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.mkdir(parents=True, exist_ok=True)
    return db_path, art_path
