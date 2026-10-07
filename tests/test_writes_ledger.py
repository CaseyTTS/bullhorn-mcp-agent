"""Phase 4B D-4B-6: the persistent idempotency ledger (exclusive create, windows, in-doubt)."""

import datetime as dt
import threading

from bullhorn_mcp.writes.ledger import IdempotencyKey, Ledger

from ._tenant_helpers import NOW, make_store


def _key(kind="derived", value="k" * 64, payload="p" * 64):
    return IdempotencyKey(kind, value, payload)


class TestLedger:
    def test_lifecycle(self, tmp_path):
        ledger = Ledger(make_store(tmp_path))
        key = _key()
        assert ledger.lookup(key, NOW).status == "new"
        assert ledger.begin(key, NOW, operation_id="o", correlation_id="c").status == "new"
        assert ledger.lookup(key, NOW).status == "in_doubt"
        assert ledger.begin(key, NOW, operation_id="o", correlation_id="c").status == "in_doubt"
        ledger.finish(key, 1, NOW, "committed", 9)
        assert ledger.lookup(key, NOW).to_dict() == {"verdict": "duplicate", "record_id": 9}

    def test_windows(self, tmp_path):
        ledger = Ledger(make_store(tmp_path))
        derived, caller = _key(), _key("caller", "my-key")
        for key in (derived, caller):
            ledger.begin(key, NOW, operation_id="o", correlation_id="c")
            ledger.finish(key, 1, NOW, "committed", 1)
        later = NOW + dt.timedelta(hours=24)
        assert ledger.lookup(derived, later).status == "new"
        assert ledger.lookup(caller, later).status == "duplicate"
        assert ledger.lookup(caller, NOW + dt.timedelta(days=7)).status == "new"
        assert ledger.begin(derived, later, operation_id="o2", correlation_id="c2").status == "new"

    def test_pending_never_expires(self, tmp_path):
        ledger = Ledger(make_store(tmp_path))
        ledger.begin(_key(), NOW, operation_id="o", correlation_id="c")
        assert ledger.lookup(_key(), NOW + dt.timedelta(days=365)).status == "in_doubt"

    def test_failed_is_reusable(self, tmp_path):
        ledger = Ledger(make_store(tmp_path))
        ledger.begin(_key(), NOW, operation_id="o", correlation_id="c")
        ledger.finish(_key(), 1, NOW, "failed")
        assert ledger.lookup(_key(), NOW).status == "new"
        assert ledger.begin(_key(), NOW, operation_id="o", correlation_id="c").status == "new"

    def test_caller_key_payload_conflict(self, tmp_path):
        ledger = Ledger(make_store(tmp_path))
        ledger.begin(_key("caller", "x"), NOW, operation_id="o", correlation_id="c")
        ledger.finish(_key("caller", "x"), 1, NOW, "committed", 1)
        assert ledger.lookup(_key("caller", "x", payload="q" * 64), NOW).status == "conflict"

    def test_corrupt_entry_is_in_doubt(self, tmp_path):
        store = make_store(tmp_path)
        ledger = Ledger(store)
        ledger.begin(_key(), NOW, operation_id="o", correlation_id="c")
        path = next((store.root / "writes" / "ledger").glob("*.json"))
        path.write_text("{not json", encoding="utf-8")
        assert ledger.lookup(_key(), NOW).status == "in_doubt"

    def test_concurrent_begin_one_winner(self, tmp_path):
        store = make_store(tmp_path)
        results = []
        barrier = threading.Barrier(8)

        def run():
            barrier.wait()
            results.append(Ledger(store).begin(_key(), NOW, operation_id="o", correlation_id="c").status)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("new") == 1

    def test_key_file_names_are_hashes(self, tmp_path):
        store = make_store(tmp_path)
        Ledger(store).begin(_key("caller", "../../evil"), NOW, operation_id="o", correlation_id="c")
        names = [p.name for p in (store.root / "writes" / "ledger").iterdir()]
        assert names == [f"{_key('caller', '../../evil').file_id}.g1.json"]
