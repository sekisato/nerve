from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from nerve.cli import main
from nerve.lab.arms import ControlArm, LabRunner
from nerve.lab.models import (
    ExecutionObservation,
    ForwardOutcome,
    MeasurementEvent,
    MeasurementEventKind,
    ObservationSnapshot,
    OutcomeStatus,
)
from nerve.models import ChainName, PoolObservation
from nerve.observatory.read_store import ObservatoryReadStore
from nerve.observatory.server import make_server
from nerve.store import NerveStore

STATIC_DIR = Path(__file__).parents[1] / "src" / "nerve" / "observatory" / "static"


def impulse() -> Any:
    return PoolObservation(
        chain=ChainName.ROBINHOOD,
        token="0xtoken",
        pool="0xpool",
        liquidity_usd=Decimal("600000"),
        volume_1h_usd=Decimal("400000"),
        volume_24h_usd=Decimal("1000000"),
        top10_pct=Decimal("35"),
        slippage_bps=50,
        mint_renounced=True,
        lp_locked=True,
        contract_verified=True,
        buy_route=True,
        sell_route=True,
    ).to_impulse()


def populated_db(path: Path) -> str:
    store = NerveStore(path)
    item = impulse()
    store.save(item)
    captured = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ObservationSnapshot.capture(item, captured_at=captured)
    store.record_observation_snapshot(snapshot)
    LabRunner(store, (ControlArm(),)).evaluate(
        snapshot, deadline_at=datetime.now(UTC) + timedelta(seconds=30)
    )
    pending = ForwardOutcome(
        recorded_at=captured,
        snapshot_id=snapshot.snapshot_id,
        horizon_seconds=60,
        target_at=captured + timedelta(seconds=60),
        source="fixture",
        status=OutcomeStatus.PENDING,
    )
    resolved = ForwardOutcome(
        recorded_at=captured + timedelta(seconds=70),
        snapshot_id=snapshot.snapshot_id,
        horizon_seconds=60,
        target_at=captured + timedelta(seconds=60),
        resolved_at=captured + timedelta(seconds=70),
        reference_price=Decimal("10"),
        outcome_price=Decimal("11"),
        return_pct=Decimal("10"),
        realized_label=True,
        source="fixture",
        status=OutcomeStatus.RESOLVED,
    )
    store.record_forward_outcome(pending)
    store.record_forward_outcome(resolved)
    store.record_execution_observation(
        ExecutionObservation(
            snapshot_id=snapshot.snapshot_id,
            arm_id="CONTROL",
            source="not_quoted",
            status="unavailable",
        )
    )
    store.record_measurement_event(
        MeasurementEvent(
            recorded_at=captured + timedelta(seconds=2),
            kind=MeasurementEventKind.CAPTURE_OK,
            stage="post_sentinel_capture",
            impulse_id=item.id,
            snapshot_id=snapshot.snapshot_id,
            message="captured",
        )
    )
    store.record_measurement_event(
        MeasurementEvent(
            recorded_at=captured + timedelta(seconds=3),
            kind=MeasurementEventKind.ARM_FAILED,
            stage="arm:test",
            impulse_id=item.id,
            snapshot_id=snapshot.snapshot_id,
            error_type="RuntimeError",
            message="fixture arm failure",
        )
    )
    store.close()
    return snapshot.snapshot_id


def database_state(path: Path) -> tuple[list[tuple[Any, ...]], dict[str, int]]:
    conn = sqlite3.connect(path)
    schema = conn.execute(
        """SELECT type,name,sql FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
    ).fetchall()
    tables = [str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    counts = {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }
    conn.close()
    return schema, counts


def get_json(url: str) -> Any:
    with urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def test_observatory_cli_defaults_to_loopback_port_3000(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, Any] = {}

    def fake_serve(db_path: Path, host: str, port: int) -> None:
        called.update({"db_path": db_path, "host": host, "port": port})

    monkeypatch.setenv("DB_PATH", str(tmp_path / "truth.db"))
    monkeypatch.setattr("nerve.observatory.server.serve", fake_serve)
    assert main(["observatory"]) == 0
    assert called == {
        "db_path": tmp_path / "truth.db",
        "host": "127.0.0.1",
        "port": 3000,
    }


def test_reader_is_true_read_only_and_preserves_database(tmp_path: Path) -> None:
    path = tmp_path / "truth.db"
    populated_db(path)
    before = database_state(path)
    with ObservatoryReadStore(path) as reader:
        assert reader.summary()["snapshot_count"] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.conn.execute(
                """INSERT INTO measurement_events(
                       event_id,recorded_at,kind,stage,impulse_id,message,details_json
                   ) VALUES('nope','2026-01-01','capture_ok','test','i','no','{}')"""
            )
    assert database_state(path) == before


def test_snapshot_detail_keeps_joins_nulls_history_and_latest_state(tmp_path: Path) -> None:
    path = tmp_path / "truth.db"
    snapshot_id = populated_db(path)
    with ObservatoryReadStore(path) as reader:
        detail = reader.snapshot_detail(snapshot_id)
        assert detail is not None
        assert detail["snapshot"]["snapshot_id"] == snapshot_id
        assert [row["display_arm"] for row in detail["decisions"]] == ["CONTROL · ABSTAIN"]
        assert detail["decisions"][0]["strategy_id"] == "control-abstain-v1"
        assert len(detail["forward_outcome_events"]) == 2
        assert [row["status"] for row in detail["latest_forward_outcomes"]] == ["resolved"]
        execution = detail["execution_observations"][0]
        assert execution["quote_price"] is None
        assert execution["obtainable_quantity"] is None
        arms = reader.arms()
        assert arms[0]["baseline_kind"] == "abstention"
        assert arms[0]["performance_inference_allowed"] is False


def test_measurement_failures_are_visible_in_health(tmp_path: Path) -> None:
    path = tmp_path / "truth.db"
    populated_db(path)
    with ObservatoryReadStore(path) as reader:
        health = reader.health()
        assert health["capture_success_count"] == 1
        assert health["arm_failure_count"] == 1
        assert health["failure_count"] == 1
        assert health["latest_failure"]["message"] == "fixture arm failure"


def test_server_get_api_and_mutation_rejection_do_not_change_db(tmp_path: Path) -> None:
    path = tmp_path / "truth.db"
    snapshot_id = populated_db(path)
    before = database_state(path)
    server = make_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/", timeout=3) as response:
            assert response.status == 200
            assert b"NERVE Observatory" in response.read()
        assert get_json(base + "/api/observatory/summary")["snapshot_count"] == 1
        assert get_json(base + "/api/observatory/snapshots")[0]["snapshot_id"] == snapshot_id
        detail = get_json(base + f"/api/observatory/snapshot/{snapshot_id}")
        assert detail["decisions"][0]["display_arm"] == "CONTROL · ABSTAIN"
        assert get_json(base + "/api/observatory/arms")[0]["baseline_kind"] == "abstention"
        assert get_json(base + "/api/observatory/calibration") == []
        assert get_json(base + "/api/observatory/health")["arm_failure_count"] == 1
        assert get_json(base + "/api/observatory/memecoin-state")["observation_count"] == 0
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + "/api/observatory/summary", method=method), timeout=3)
            assert error.value.code == 405
            assert error.value.headers["Allow"] == "GET"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert database_state(path) == before


def test_frontend_has_no_external_data_brain_and_renders_null_as_unavailable() -> None:
    source = "\n".join(path.read_text() for path in sorted(STATIC_DIR.iterdir()))
    forbidden = (
        "api.jup.ag",
        "DexScreener",
        "CoinGecko",
        "Helius",
        "TypeSafe",
        "wallet provider",
        "connectWallet",
        "setup_quality",
    )
    assert not any(value.lower() in source.lower() for value in forbidden)
    assert "innerHTML" not in source
    assert 'if (value === null || value === undefined || value === "") return "Unavailable"' in source
    assert "fetch(`/api/observatory${path}`" in source


def test_forward_outcome_migration_is_backward_compatible_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "phase0.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE observation_snapshots (
            snapshot_id TEXT PRIMARY KEY, impulse_id TEXT NOT NULL, chain TEXT NOT NULL,
            token TEXT NOT NULL, pool TEXT NOT NULL, observed_at TEXT NOT NULL,
            captured_at TEXT NOT NULL, stage TEXT NOT NULL, input_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE forward_outcomes (
            outcome_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
            horizon_seconds INTEGER NOT NULL, target_at TEXT NOT NULL,
            resolved_at TEXT, reference_price TEXT, outcome_price TEXT,
            return_pct TEXT, realized_label INTEGER, source TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id),
            UNIQUE(snapshot_id, horizon_seconds, status)
        );
        INSERT INTO observation_snapshots VALUES(
            'snap','impulse','robinhood','token','pool',
            '2026-01-01T00:00:00+00:00','2026-01-01T00:00:01+00:00',
            'post_sentinel','hash','{}'
        );
        INSERT INTO forward_outcomes VALUES(
            'event-1','snap',60,'2026-01-01T00:01:00+00:00',NULL,NULL,NULL,NULL,NULL,
            'unconfigured','pending'
        );
        """
    )
    conn.commit()
    conn.close()

    store = NerveStore(path)
    schema = str(store.conn.execute(
        "SELECT sql FROM sqlite_schema WHERE name='forward_outcomes'"
    ).fetchone()["sql"])
    columns = {str(row["name"]) for row in store.conn.execute(
        "PRAGMA table_info(forward_outcomes)"
    ).fetchall()}
    assert "recorded_at" in columns
    assert "UNIQUE(snapshot_id, horizon_seconds, status)" not in schema
    assert len(store.list_forward_outcome_events("snap")) == 1
    store.record_forward_outcome(
        ForwardOutcome(
            recorded_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
            snapshot_id="snap",
            horizon_seconds=60,
            target_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            source="retry",
            status=OutcomeStatus.PENDING,
        )
    )
    assert len(store.list_forward_outcome_events("snap")) == 2
    assert store.list_forward_outcomes("snap")[0].source == "retry"
    store.close()

    reopened = NerveStore(path)
    assert len(reopened.list_forward_outcome_events("snap")) == 2
    reopened.close()
