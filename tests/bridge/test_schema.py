"""schema 校验：每个产出的快照都必须通过 protocol/state.schema.json。"""

import json

from bridge.sources import mock, replay
from bridge.state.engine import StateEngine

import bridge.events as ev
from conftest import REPO_ROOT, assert_invariants, encode


def test_every_mock_snapshot_validates(validator, lifecycle_fixtures):
    for name, snaps in lifecycle_fixtures.items():
        for snap in snaps:
            errors = list(validator.iter_errors(snap))
            assert not errors, f"scenario={name} seq={snap['seq']}: {errors[0].message}"
            assert_invariants(snap)


def test_seq_strictly_increasing_within_epoch(lifecycle_fixtures):
    for name, snaps in lifecycle_fixtures.items():
        seqs = [s["seq"] for s in snaps]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert seqs[0] >= 1
        epochs = {s["bridge_epoch"] for s in snaps}
        assert epochs == {snaps[0]["bridge_epoch"]}


def test_replay_edge_fixture_validates(validator):
    fixture = REPO_ROOT / "tests" / "fixtures" / "bridge" / "edge_events.jsonl"
    snaps = replay.run_file(str(fixture), epoch="replay-edge", utc_anchor_ms=1789002000000)
    assert snaps
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, errors[0].message
        assert_invariants(snap)
    last = snaps[-1]
    assert last["source"]["stale"] is True and last["source"]["connected"] is False
    assert last["selected_thread_id"] == "thread-edge-1"  # fixture 中的本地选中
    plan = last["threads"][0]["plan"]
    assert plan["total"] == 12 and len(plan["steps"]) == 8 and plan["truncated"] is True
    usage = last["usage"]
    assert usage["windows_total"] == 6 and len(usage["windows"]) == 4
    assert usage["windows_truncated"] is True
    assert all(len(w["label"].encode("utf-8")) <= 48 for w in usage["windows"])


def test_snapshot_bytes_within_16kib(validator, lifecycle_fixtures):
    for snaps in lifecycle_fixtures.values():
        for snap in snaps:
            assert len(encode(snap)) <= 16384


def test_anchor_mapping_deterministic(validator):
    engine = StateEngine("epoch-anch", source_kind="mock", utc_anchor_ms=1789002000000)
    snap = engine.apply(ev.thread_started("t1", "p", at_ms=7), 7)
    assert snap["generated_at_ms"] == 1789002000007
    assert snap["source"]["last_event_at_ms"] == 1789002000007
    assert snap["threads"][0]["updated_at_ms"] == 1789002000007
    assert not list(validator.iter_errors(snap))


def test_json_roundtrip_of_snapshot_is_stable():
    import bridge.state.render as render_mod

    snaps = mock.run("usage_100")
    raw = encode(snaps[-1])
    assert json.loads(raw.decode("utf-8")) == snaps[-1]
    assert render_mod.MAX_MESSAGE_BYTES == 16384
