"""Session stores (Phase 5A, D-5A-6; AC-16 / SR-13)."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

import pytest

from bullhorn_mcp.identity.session_store import (
    EncryptedFileSessionStore,
    MemorySessionStore,
    SessionKeys,
    SessionRecord,
    SessionStoreError,
    new_id,
)

from ._identity_helpers import ALICE, BOB, TK1, TK2, pk

SENTINEL = "SENTINEL-BH-REST-TOKEN-5a-fe3d"
REFRESH = "SENTINEL-REFRESH-TOKEN-5a-77aa"


def keys(**k):
    return SessionKeys(k or {"k1": os.urandom(32)}, list(k or {"k1": 0})[-1])


def rec(tk=TK1, who=ALICE, token=SENTINEL):
    now = time.time()
    return SessionRecord(tk, pk(who), "https://rest99.bullhornstaffing.com/rest-services/abc123", token, now + 600,
                         "SENTINEL-ACCESS", now + 600, REFRESH, "https://auth.bullhornstaffing.com", None, now, now)


@pytest.fixture
def k1():
    return os.urandom(32)


@pytest.fixture
def store(tmp_path, k1):
    return EncryptedFileSessionStore(tmp_path, SessionKeys({"k1": k1}, "k1"))


def all_bytes(root):
    out = b""
    for dirpath, _d, files in os.walk(root):
        for f in files:
            with open(os.path.join(dirpath, f), "rb") as fh:
                out += fh.read()
    return out


class TestEncryptedStore:
    def test_roundtrip_and_no_plaintext(self, store, tmp_path):
        store.put(rec())
        got = store.get(TK1, pk(ALICE))
        assert got is not None and got.bh_rest_token == SENTINEL and got.refresh_token == REFRESH
        raw = all_bytes(tmp_path)
        assert SENTINEL.encode() not in raw and REFRESH.encode() not in raw and b"SENTINEL" not in raw
        assert pk(ALICE).encode() not in raw  # file names are hashes, contents are sealed

    def test_repr_redacted(self, store):
        r = rec()
        assert SENTINEL not in repr(r) and REFRESH not in repr(r) and "SENTINEL" not in repr(r)
        assert "<redacted>" in repr(store) and "k1" in repr(store.keys)

    def test_swapped_file_fails_closed(self, store):
        store.put(rec(who=ALICE))
        shutil.copyfile(store._session_path(TK1, pk(ALICE)), store._session_path(TK1, pk(BOB)))
        assert store.get(TK1, pk(BOB)) is None  # AAD binds the file to (TK1, ALICE)
        assert store.get(TK1, pk(ALICE)) is not None

    def test_cross_tenant_copy_fails_closed(self, store):
        store.put(rec(tk=TK1))
        shutil.copyfile(store._session_path(TK1, pk(ALICE)), store._session_path(TK2, pk(ALICE)))
        assert store.get(TK2, pk(ALICE)) is None

    def test_tampered_byte_fails_closed(self, store):
        store.put(rec())
        path = store._session_path(TK1, pk(ALICE))
        data = bytearray(path.read_bytes())
        data[-5] ^= 0x01
        path.write_bytes(bytes(data))
        assert store.get(TK1, pk(ALICE)) is None

    def test_key_rotation(self, tmp_path, k1):
        old = EncryptedFileSessionStore(tmp_path, SessionKeys({"k1": k1}, "k1"))
        old.put(rec())
        k2 = os.urandom(32)
        rotated = EncryptedFileSessionStore(tmp_path, SessionKeys({"k1": k1, "k2": k2}, "k2"))
        assert rotated.get(TK1, pk(ALICE)) is not None  # old files still decrypt
        rotated.put(rec(who=BOB))
        assert rotated._session_path(TK1, pk(BOB)).read_bytes()[5:7] == b"k2"
        removed = EncryptedFileSessionStore(tmp_path, SessionKeys({"k2": k2}, "k2"))
        assert removed.get(TK1, pk(ALICE)) is None  # removed key: fails closed
        assert removed.get(TK1, pk(BOB)) is not None

    def test_wrong_key_fails_closed(self, tmp_path, store):
        store.put(rec())
        other = EncryptedFileSessionStore(tmp_path, SessionKeys({"k1": os.urandom(32)}, "k1"))
        assert other.get(TK1, pk(ALICE)) is None

    def test_delete_only_that_owner(self, store):
        store.put(rec(who=ALICE))
        store.put(rec(who=BOB))
        store.put(rec(tk=TK2, who=ALICE))
        assert store.delete(TK1, pk(ALICE)) is True
        assert store.get(TK1, pk(ALICE)) is None
        assert store.get(TK1, pk(BOB)) is not None and store.get(TK2, pk(ALICE)) is not None
        assert store.delete(TK1, pk(ALICE)) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_owner_only_mode(self, store):
        store.put(rec())
        assert (store._session_path(TK1, pk(ALICE)).stat().st_mode & 0o777) == 0o600

    def test_invalid_owner_keys_refused(self, store):
        with pytest.raises(SessionStoreError):
            store.get("tenant", pk(ALICE))
        with pytest.raises(SessionStoreError):
            store.get(TK1, "../../etc")

    def test_pending_login_is_single_use_under_concurrency(self, store):
        login_id = new_id()
        store.put_login(login_id, {"state": "s"})
        assert store.get_login(login_id) == {"state": "s"}
        results = []
        barrier = threading.Barrier(8)

        def take():
            barrier.wait()
            results.append(store.take_login(login_id))

        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(r is not None for r in results) == 1
        assert store.take_login(login_id) is None

    def test_pending_file_swap_fails_closed(self, store):
        a, b = new_id(), new_id()
        store.put_login(a, {"state": "for-a"})
        shutil.copyfile(store._pending_path("login", a), store._pending_path("login", b))
        assert store.take_login(b) is None

    def test_link_kinds_are_separate(self, store):
        ident = new_id()
        store.put_link(ident, {"x": 1})
        assert store.take_login(ident) is None
        assert store.take_link(ident) == {"x": 1}

    def test_bad_identifiers_refused(self, store):
        with pytest.raises(SessionStoreError):
            store.put_login("short", {})


class TestKeys:
    def test_from_text(self):
        import base64
        import json

        text = json.dumps({"active": "a", "keys": {"a": base64.b64encode(b"\x02" * 32).decode()}})
        assert SessionKeys.from_text(text).active == "a"

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            '{"active": "a"}',
            '{"active": "b", "keys": {"a": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="}}',
            '{"active": "a", "keys": {"a": "AgIC"}}',
            '{"active": "a", "keys": {"a": "!!!"}}',
            '{"active": "a", "keys": {"a": 5}}',
        ],
    )
    def test_invalid_refused(self, text):
        with pytest.raises(SessionStoreError):
            SessionKeys.from_text(text)


class TestMemoryStore:
    def test_isolation(self):
        m = MemorySessionStore()
        m.put(rec(who=ALICE))
        assert m.get(TK1, pk(BOB)) is None and m.get(TK2, pk(ALICE)) is None
        assert m.get(TK1, pk(ALICE)).bh_rest_token == SENTINEL
        assert m.delete(TK1, pk(ALICE)) and m.get(TK1, pk(ALICE)) is None

    def test_record_validation(self):
        assert SessionRecord.from_dict({"tenant_key": TK1}) is None
        bad = rec().to_dict()
        bad["bh_expires_at"] = True
        assert SessionRecord.from_dict(bad) is None
