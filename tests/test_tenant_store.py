"""AC-9: store immutability, atomic active pointer, history, and bounded store errors."""

import json
import os
from pathlib import Path

import pytest

from bullhorn_mcp.schema.errors import SchemaError
from bullhorn_mcp.tenant.store import SetupStore, SetupStoreError, check_external_path, store_from_env

from ._tenant_helpers import init_tenant, make_store, propose_and_commit


class TestOpen:
    def test_missing_directory(self, tmp_path):
        with pytest.raises(SetupStoreError):
            SetupStore(tmp_path / "nope")

    def test_file_not_directory(self, tmp_path):
        f = tmp_path / "file"
        f.write_text("x")
        with pytest.raises(SetupStoreError) as info:
            SetupStore(f)
        assert isinstance(info.value, SchemaError)
        assert not isinstance(info.value, OSError)

    def test_unreadable_directory(self, tmp_path, monkeypatch):
        root = tmp_path / "store"
        root.mkdir()

        def boom(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "listdir", boom)
        with pytest.raises(SetupStoreError) as info:
            SetupStore(root)
        assert "Permission denied" in str(info.value) and len(str(info.value)) <= 500

    def test_store_from_env(self, tmp_path):
        assert store_from_env({}) is None
        assert store_from_env({"BULLHORN_SETUP_STORE": "  "}) is None
        (tmp_path / "s").mkdir()
        assert store_from_env({"BULLHORN_SETUP_STORE": str(tmp_path / "s")}).root == (tmp_path / "s").resolve()

    def test_versions_dir_is_a_file(self, tmp_path):
        store = make_store(tmp_path)
        (store.root / "versions").write_text("x")
        with pytest.raises(SetupStoreError):
            store.list_versions()


class TestVersions:
    def test_commit_writes_immutable_version(self, tmp_path):
        store = make_store(tmp_path)
        init_tenant(store)
        assert store.list_versions() == [1]
        profile = store.read_version(1)
        with pytest.raises(SetupStoreError) as info:
            store.write_version(profile)
        assert "immutable" in str(info.value)

    def test_version_mismatch_is_profile_error(self, tmp_path):
        store = make_store(tmp_path)
        init_tenant(store)
        text = store.version_path(1).read_text(encoding="utf-8")
        store.version_path(2).write_text(text, encoding="utf-8")
        with pytest.raises(SchemaError):
            store.read_version(2)

    @pytest.mark.parametrize("n", [0, -1, 1_000_000, True, "1", 1.0])
    def test_bad_version_numbers(self, tmp_path, n):
        with pytest.raises(SetupStoreError):
            make_store(tmp_path).version_path(n)

    def test_corrupt_utf8(self, tmp_path):
        store = make_store(tmp_path)
        store.version_path(1).write_bytes(b"\xff\xfe\x00")
        with pytest.raises(SetupStoreError):
            store.read_version(1)

    def test_duplicate_key_in_version_file(self, tmp_path):
        store = make_store(tmp_path)
        init_tenant(store)
        path = store.version_path(1)
        path.write_text(path.read_text(encoding="utf-8") + "format: tenant-profile/v2\n", encoding="utf-8")
        with pytest.raises(SchemaError) as info:
            store.read_version(1)
        assert "duplicate mapping key" in str(info.value)


class TestActiveAndHistory:
    def test_active_pointer_and_history(self, tmp_path):
        store = make_store(tmp_path)
        r1 = init_tenant(store)
        active = json.loads((store.root / "active.json").read_text())
        assert active["version"] == 1 and active["actor"] == "admin@example.com"
        assert active["correlation_id"] == r1["correlation_id"]
        lines = (store.root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        r2 = propose_and_commit(store, [{"op": "set_setting", "name": "reporting_timezone", "value": "UTC"},
                                        {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"}])
        lines = (store.root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 2
        entry = json.loads(lines[1])
        assert entry["correlation_id"] == r2["correlation_id"]
        assert entry["actor"] == "admin@example.com"
        assert entry["version"] == 2 and entry["prev_version"] == 1
        assert any(d["key"] == "field:job.priority" for d in entry["diff"])
        assert "validation_summary" in entry
        assert not list(store.root.glob(".active.json.*"))  # no temp files left behind

    def test_active_switch_uses_os_replace(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        calls = []
        real = os.replace

        def spy(src, dst):
            calls.append(Path(dst).name)
            return real(src, dst)

        monkeypatch.setattr(os, "replace", spy)
        init_tenant(store)
        assert "active.json" in calls

    def test_corrupt_active(self, tmp_path):
        store = make_store(tmp_path)
        (store.root / "active.json").write_text('{"version": "x"}')
        with pytest.raises(SetupStoreError):
            store.active_version()
        (store.root / "active.json").write_text("not json")
        with pytest.raises(SetupStoreError):
            store.active_version()

    def test_write_failure_wrapped(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(SetupStoreError) as info:
            store.set_active(1, "2026-10-06T00:00:00Z", "a", "c")
        assert "No space left" in str(info.value)
        assert not list(store.root.glob(".active.json.*"))

    def test_history_corrupt_line_tolerated(self, tmp_path):
        store = make_store(tmp_path)
        (store.root / "history.jsonl").write_text('{"a": 1}\nnot json\n[1]\n')
        assert store.read_history() == [{"a": 1}, {"corrupt_line": 2}, {"corrupt_line": 3}]


class TestProposalIds:
    @pytest.mark.parametrize("pid", ["../active", "..\\x", "A" * 32, "a" * 31, "", None, 5, "a" * 32 + "/"])
    def test_rejects_traversal(self, tmp_path, pid):
        with pytest.raises(SetupStoreError):
            make_store(tmp_path).load_proposal(pid)


class TestCommitLock:
    def test_lock_exclusive(self, tmp_path):
        store = make_store(tmp_path)
        lock = store.acquire_commit_lock()
        with pytest.raises(SetupStoreError):
            store.acquire_commit_lock()
        store.release_commit_lock(lock)
        store.release_commit_lock(store.acquire_commit_lock())


class TestExternalPaths:
    def test_import_must_exist(self, tmp_path):
        with pytest.raises(SetupStoreError):
            check_external_path(str(tmp_path / "missing.yaml"), None, for_write=False)

    @pytest.mark.parametrize("name", ["x.txt", "x", "x.yaml.exe"])
    def test_suffix(self, tmp_path, name):
        (tmp_path / name).write_text("a: 1")
        with pytest.raises(SetupStoreError):
            check_external_path(str(tmp_path / name), None, for_write=False)

    def test_inside_store_refused(self, tmp_path):
        store = make_store(tmp_path)
        (store.root / "x.yaml").write_text("a: 1")
        with pytest.raises(SetupStoreError):
            check_external_path(str(store.root / "x.yaml"), store.root, for_write=False)
        with pytest.raises(SetupStoreError):
            check_external_path(str(store.root / "new.yaml"), store.root, for_write=True)
        with pytest.raises(SetupStoreError):
            check_external_path(str(tmp_path / "elsewhere" / ".." / "store" / "y.yaml"), store.root, for_write=True)

    def test_symlink_into_store_refused(self, tmp_path):
        store = make_store(tmp_path)
        target = store.root / "secret.yaml"
        target.write_text("a: 1")
        link = tmp_path / "link.yaml"
        link_dir = tmp_path / "linkdir"
        try:
            link.symlink_to(target)
            link_dir.symlink_to(store.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this host")
        with pytest.raises(SetupStoreError):
            check_external_path(str(link), store.root, for_write=False)
        with pytest.raises(SetupStoreError):
            check_external_path(str(link_dir / "new.yaml"), store.root, for_write=True)
        with pytest.raises(SetupStoreError):
            check_external_path(str(link), store.root, for_write=True)

    def test_export_existing_refused(self, tmp_path):
        (tmp_path / "a.yaml").write_text("x")
        with pytest.raises(SetupStoreError):
            check_external_path(str(tmp_path / "a.yaml"), None, for_write=True)

    @pytest.mark.parametrize("raw", [None, "", " ", 5, "a\x00.yaml", "x" * 2000 + ".yaml"])
    def test_bad_paths(self, raw):
        with pytest.raises(SetupStoreError):
            check_external_path(raw, None, for_write=False)

    def test_oversized_import(self, tmp_path):
        big = tmp_path / "big.yaml"
        big.write_bytes(b"a" * 1_000_001)
        with pytest.raises(SetupStoreError):
            check_external_path(str(big), None, for_write=False)
