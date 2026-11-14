"""Project-relative path resolution for the host runtime."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project_path(value: str | Path, root: Path = ROOT) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()
