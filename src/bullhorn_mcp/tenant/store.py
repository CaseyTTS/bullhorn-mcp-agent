"""The tenant setup store (``BULLHORN_SETUP_STORE``, D-4A-7).

Layout::

    versions/v000001.yaml   immutable v2 documents (created with open(..., "x"))
    active.json             {"version", "activated_at", "actor", "correlation_id"} (atomic os.replace)
    proposals/<id>.json     proposals (see changes.py)
    history.jsonl           append-only commit history
    discovery/latest.json   last discovery snapshot + drift report + last validation

Every ``OSError`` (and every decoding problem) becomes a bounded
``SetupStoreError``; every YAML read goes through ``yaml_strict``.

Phase 5A (D-5A-13/14): in ``shared`` mode the store is the caller's tenant's
``setup_store`` from the admin config (``BULLHORN_SETUP_STORE`` is ignored), and
import/export paths must resolve inside that tenant's ``exchange_dir``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..schema.errors import ProfileError, SchemaError, cap_errors, truncate_text
from .profile_v2 import TenantProfileV2

STORE_ENV_VAR = "BULLHORN_SETUP_STORE"

PROPOSAL_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_VERSION_FILE_RE = re.compile(r"v(\d{6})\.yaml", re.ASCII)
MAX_VERSION = 999_999
MAX_JSON_BYTES = 20_000_000
MAX_HISTORY_LINES = 100_000


class SetupStoreError(SchemaError):
    """The setup store could not be read or written. The message is bounded."""

    def __init__(self, message: str) -> None:
        super().__init__(truncate_text(message, 500))


def _os_message(action: str, exc: BaseException) -> str:
    detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
    return f"setup store: could not {action}: {truncate_text(str(detail), 200)}"


def _shared_tenant() -> Any:
    """``None`` in local mode; the caller's ``TenantConfig`` in shared mode (fails closed)."""
    from ..identity import deploy

    if not deploy.is_shared():
        return None
    from ..identity.principal import IdentityRequired, current_identity

    try:
        tenant = current_identity().tenant
    except IdentityRequired:
        raise SetupStoreError("setup store: identity_required") from None
    if tenant is None:
        raise SetupStoreError("setup store: identity_required")
    return tenant


def store_from_env(env: Mapping[str, str] | None = None) -> SetupStore | None:
    """The configured store, or ``None`` when ``BULLHORN_SETUP_STORE`` is unset/blank."""
    tenant = _shared_tenant()
    if tenant is not None:
        return SetupStore(tenant.setup_store)
    env = os.environ if env is None else env
    raw = env.get(STORE_ENV_VAR, "")
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    return SetupStore(Path(value).expanduser())


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class SetupStore:
    """File-backed store. Construction checks that the root is an existing directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        try:
            resolved = Path(root).resolve(strict=True)
            if not resolved.is_dir():
                raise SetupStoreError("setup store: BULLHORN_SETUP_STORE is not a directory")
            os.listdir(resolved)
        except SetupStoreError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise SetupStoreError(_os_message("open the store directory", exc)) from exc
        self.root = resolved

    # ------------------------------------------------------------------ #
    # Low-level helpers (all OSError -> SetupStoreError)
    # ------------------------------------------------------------------ #

    def _dir(self, name: str) -> Path:
        path = self.root / name
        try:
            path.mkdir(exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise SetupStoreError(f"setup store: {name}/ is not a directory")
        except SetupStoreError:
            raise
        except OSError as exc:
            raise SetupStoreError(_os_message(f"create {name}/", exc)) from exc
        return path

    def _read_text(self, path: Path, what: str) -> str | None:
        try:
            if path.is_symlink():
                raise SetupStoreError(f"setup store: {what} is a symbolic link")
            if not path.exists():
                return None
            if path.stat().st_size > MAX_JSON_BYTES:
                raise SetupStoreError(f"setup store: {what} is too large")
            return path.read_text(encoding="utf-8")
        except SetupStoreError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise SetupStoreError(_os_message(f"read {what}", exc)) from exc

    def _read_json(self, path: Path, what: str) -> Any:
        text = self._read_text(path, what)
        if text is None:
            return None
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise SetupStoreError(f"setup store: {what} is not valid JSON") from exc

    def _atomic_write(self, path: Path, text: str, what: str) -> None:
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
        except OSError as exc:
            raise SetupStoreError(_os_message(f"write {what}", exc)) from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    @staticmethod
    def _dumps(data: Any) -> str:
        try:
            return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise SetupStoreError(f"setup store: data is not serializable ({type(exc).__name__})") from exc

    # ------------------------------------------------------------------ #
    # Versions
    # ------------------------------------------------------------------ #

    def version_path(self, number: int) -> Path:
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_VERSION:
            raise SetupStoreError("setup store: version must be an integer from 1 to 999999")
        return self._dir("versions") / f"v{number:06d}.yaml"

    def list_versions(self) -> list[int]:
        directory = self._dir("versions")
        try:
            names = os.listdir(directory)
        except OSError as exc:
            raise SetupStoreError(_os_message("list versions/", exc)) from exc
        out = []
        for name in names:
            m = _VERSION_FILE_RE.fullmatch(name)
            if m and int(m.group(1)) >= 1:
                out.append(int(m.group(1)))
        return sorted(out)

    def read_version(self, number: int) -> TenantProfileV2:
        path = self.version_path(number)
        text = self._read_text(path, f"version {number}")
        if text is None:
            raise SetupStoreError(f"setup store: version {number} does not exist")
        profile = TenantProfileV2.from_yaml_text(text, source=f"version {number}")
        if profile.profile_version != number:
            raise ProfileError([f"profile_version {profile.profile_version} does not match file version {number}"], f"version {number}")
        return profile

    def write_version(self, profile: TenantProfileV2) -> Path:
        """Create the version file exclusively. An existing file is never overwritten."""
        path = self.version_path(profile.profile_version)
        text = profile.to_yaml_text()
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError as exc:
            raise SetupStoreError(f"setup store: version {profile.profile_version} already exists; versions are immutable") from exc
        except OSError as exc:
            raise SetupStoreError(_os_message(f"write version {profile.profile_version}", exc)) from exc
        return path

    # ------------------------------------------------------------------ #
    # Active pointer
    # ------------------------------------------------------------------ #

    def read_active(self) -> dict[str, Any] | None:
        data = self._read_json(self.root / "active.json", "active.json")
        if data is None:
            return None
        version = data.get("version") if isinstance(data, dict) else None
        if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
            raise SetupStoreError("setup store: active.json is corrupt")
        return data

    def active_version(self) -> int | None:
        active = self.read_active()
        return None if active is None else int(active["version"])

    def set_active(self, version: int, activated_at: str, actor: str, correlation_id: str) -> None:
        self.version_path(version)  # range check
        record = {"version": version, "activated_at": activated_at, "actor": actor, "correlation_id": correlation_id}
        self._atomic_write(self.root / "active.json", self._dumps(record), "active.json")

    # ------------------------------------------------------------------ #
    # Proposals
    # ------------------------------------------------------------------ #

    def proposal_path(self, proposal_id: object) -> Path:
        if not isinstance(proposal_id, str) or not PROPOSAL_ID_RE.fullmatch(proposal_id):
            raise SetupStoreError("setup store: proposal_id must be 32 lowercase hex characters")
        return self._dir("proposals") / f"{proposal_id}.json"

    def save_proposal(self, proposal_id: str, data: dict[str, Any], *, create: bool = False) -> None:
        path = self.proposal_path(proposal_id)
        text = self._dumps(data)
        if create:
            try:
                with open(path, "x", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            except OSError as exc:
                raise SetupStoreError(_os_message("create the proposal", exc)) from exc
            return
        self._atomic_write(path, text, "the proposal")

    def load_proposal(self, proposal_id: object) -> dict[str, Any] | None:
        path = self.proposal_path(proposal_id)
        data = self._read_json(path, "the proposal")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise SetupStoreError("setup store: proposal file is corrupt")
        return data

    def list_proposals(self) -> list[dict[str, Any]]:
        directory = self._dir("proposals")
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise SetupStoreError(_os_message("list proposals/", exc)) from exc
        out = []
        for name in names:
            stem = name[:-5] if name.endswith(".json") else None
            if stem is None or not PROPOSAL_ID_RE.fullmatch(stem):
                continue
            data = self.load_proposal(stem)
            if data is not None:
                out.append(data)
        return out

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #

    def append_history(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if "\n" in line:  # pragma: no cover - json.dumps never emits raw newlines
            raise SetupStoreError("setup store: history entry is not a single line")
        try:
            with open(self.root / "history.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise SetupStoreError(_os_message("append to history.jsonl", exc)) from exc

    def read_history(self) -> list[dict[str, Any]]:
        text = self._read_text(self.root / "history.jsonl", "history.jsonl")
        if text is None:
            return []
        out: list[dict[str, Any]] = []
        for i, line in enumerate(text.splitlines()[:MAX_HISTORY_LINES]):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                entry = None
            out.append(entry if isinstance(entry, dict) else {"corrupt_line": i + 1})
        return out

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def acquire_commit_lock(self) -> Path:
        """Exclusive commit lock (``commit.lock``, created with O_EXCL). Refuses when another commit holds it."""
        path = self.root / "commit.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise SetupStoreError(
                "setup store: another commit is in progress (commit.lock exists; remove it only if no commit is running)"
            ) from exc
        except OSError as exc:
            raise SetupStoreError(_os_message("create commit.lock", exc)) from exc
        return path

    def release_commit_lock(self, path: Path) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def read_discovery(self) -> dict[str, Any] | None:
        directory = self._dir("discovery")
        data = self._read_json(directory / "latest.json", "discovery/latest.json")
        if data is not None and not isinstance(data, dict):
            raise SetupStoreError("setup store: discovery/latest.json is corrupt")
        return data

    def write_discovery(self, data: dict[str, Any]) -> None:
        directory = self._dir("discovery")
        self._atomic_write(directory / "latest.json", self._dumps(data), "discovery/latest.json")


def bounded_errors(errors: list[str] | tuple[str, ...]) -> list[str]:
    return [truncate_text(e, 500) for e in cap_errors(errors)]


EXTERNAL_SUFFIXES = (".yaml", ".yml")
MAX_IMPORT_BYTES = 1_000_000


def check_external_path(raw: object, store_root: Path | None, *, for_write: bool) -> Path:
    """Resolve an import/export path; refuse anything that is not a plain YAML file outside the store.

    The path is fully resolved (symlinks included) before the store check, so
    a symlink - of the file or of any parent directory - cannot reach into the
    store. For writes the target must not exist yet and its parent must be an
    existing directory; for reads it must be an existing regular file.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024 or "\x00" in raw:
        raise SetupStoreError("path must be a non-empty string of at most 1024 characters")
    candidate = Path(raw.strip()).expanduser()
    if candidate.suffix.lower() not in EXTERNAL_SUFFIXES:
        raise SetupStoreError("path must end in .yaml or .yml")
    tenant = _shared_tenant()
    if tenant is not None and not is_within(Path(os.path.abspath(candidate)), Path(os.path.abspath(tenant.exchange_dir))):
        # Lexical pre-check (shared mode): nothing outside the exchange directory is even probed.
        raise SetupStoreError("path must be inside the tenant exchange directory (shared mode)")
    try:
        if for_write:
            if candidate.is_symlink() or candidate.exists():
                raise SetupStoreError("export path already exists; choose a new file name")
            parent = candidate.parent.resolve(strict=True)
            if not parent.is_dir():
                raise SetupStoreError("export directory does not exist")
            resolved = parent / candidate.name
        else:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                raise SetupStoreError("import path is not a regular file")
            if resolved.stat().st_size > MAX_IMPORT_BYTES:
                raise SetupStoreError(f"import file is larger than {MAX_IMPORT_BYTES} bytes")
    except SetupStoreError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SetupStoreError(_os_message("resolve the path", exc)) from exc
    if store_root is not None and is_within(resolved, store_root):
        raise SetupStoreError("path must not be inside the setup store")
    if tenant is not None:
        try:
            exchange = Path(tenant.exchange_dir).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SetupStoreError("the tenant exchange directory is not available") from None
        if not is_within(resolved, exchange) or resolved == exchange:
            raise SetupStoreError("path must be inside the tenant exchange directory (shared mode)")
    return resolved


def read_external_text(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_IMPORT_BYTES + 1)
    except OSError as exc:
        raise SetupStoreError(_os_message("read the import file", exc)) from exc
    if len(data) > MAX_IMPORT_BYTES:
        raise SetupStoreError(f"import file is larger than {MAX_IMPORT_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetupStoreError("import file is not valid UTF-8") from exc


def write_external_text(path: Path, text: str) -> None:
    """Atomic export: temp file in the target directory, then ``os.replace`` onto a name that did not exist."""
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists() or path.is_symlink():
            raise SetupStoreError("export path already exists; choose a new file name")
        os.replace(tmp_name, path)
        tmp_name = None
    except SetupStoreError:
        raise
    except OSError as exc:
        raise SetupStoreError(_os_message("write the export file", exc)) from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
