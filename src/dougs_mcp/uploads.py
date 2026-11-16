"""Local files an upload tool is allowed to read."""

from pathlib import Path

from .client import session_cache_dir
from .config import Settings


def require_upload_root(settings: Settings) -> Path:
    """The configured receipts directory, or an error the agent can act on."""
    configured = settings.dougs_upload_root
    if configured is None:
        raise ValueError("set DOUGS_UPLOAD_ROOT to the directory that holds receipts and invoices")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"DOUGS_UPLOAD_ROOT is not a directory: {root}")
    return root


def resolve_upload(file_path: str, root: Path) -> Path:
    """Resolve `file_path` inside `root`. Relative paths are relative to that directory.

    Symlinks are followed and then checked: a link that lands outside the root, on `.env`,
    or in the session cache is refused.
    """
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"no file at {file_path}") from exc
    if not resolved.is_file():
        raise ValueError(f"no file at {file_path}")
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"{file_path} is outside DOUGS_UPLOAD_ROOT ({root})") from None
    if resolved.name == ".env" or ".env" in resolved.parts:
        raise ValueError("refusing to upload .env")
    cache = session_cache_dir().resolve()
    try:
        resolved.relative_to(cache)
    except ValueError:
        return resolved
    raise ValueError("refusing to upload a session-cache file")
