from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .chronology.models import (
    ChronologyCoverage,
    ChronologyEvent,
    ChronologyEventType,
    CreatorLaunchEvidence,
    DecodeStatus,
    FundingEdge,
    MemeChronologyObservation,
)
from .lab.models import (
    DecisionStatus,
    ExecutionObservation,
    ExperimentDecision,
    ForwardOutcome,
    MeasurementEvent,
    MeasurementEventKind,
    ObservationSnapshot,
    OutcomeStatus,
    canonical_json,
)
from .memestate.models import (
    FactStatus,
    MemecoinStateFact,
    MemecoinStateObservation,
    StateStatus,
)
from .models import Impulse


class NerveStore:
    """Durable audit trail. Nodes remain stateless between cycles."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS impulses (
                id TEXT PRIMARY KEY, chain TEXT, token TEXT, pool TEXT,
                score INTEGER, verdict TEXT, path TEXT, payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, impulse_id TEXT NOT NULL,
                node TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT, ts TEXT NOT NULL,
                FOREIGN KEY(impulse_id) REFERENCES impulses(id)
            );
            CREATE TABLE IF NOT EXISTS intents (
                client_id TEXT PRIMARY KEY, impulse_id TEXT NOT NULL,
                status TEXT NOT NULL, nonce INTEGER, tx_hash TEXT,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                token TEXT PRIMARY KEY, pool TEXT NOT NULL, size_usd TEXT NOT NULL,
                entry_price TEXT NOT NULL, tx_hash TEXT NOT NULL, opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_impulses_created ON impulses(created_at);
            CREATE INDEX IF NOT EXISTS idx_transitions_impulse ON transitions(impulse_id);

            CREATE TABLE IF NOT EXISTS observation_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                impulse_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                pool TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_decisions (
                decision_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                provider TEXT,
                requested_model_id TEXT,
                returned_model_id TEXT,
                question_version TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                applied_at TEXT,
                latency_ms INTEGER NOT NULL,
                raw_answer_json TEXT,
                reason TEXT NOT NULL,
                flags_json TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id),
                UNIQUE(snapshot_id, arm_id)
            );
            CREATE TABLE IF NOT EXISTS forward_outcomes (
                outcome_id TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                target_at TEXT NOT NULL,
                resolved_at TEXT,
                reference_price TEXT,
                outcome_price TEXT,
                return_pct TEXT,
                realized_label INTEGER,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS execution_observations (
                observation_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                signal_price TEXT,
                quote_at TEXT,
                quote_price TEXT,
                obtainable_quantity TEXT,
                fee TEXT,
                impact_bps INTEGER,
                executable_entry TEXT,
                executable_exit TEXT,
                net_return TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS measurement_events (
                event_id TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                stage TEXT NOT NULL,
                impulse_id TEXT NOT NULL,
                snapshot_id TEXT,
                error_type TEXT,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memecoin_state_observations (
                state_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                state_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS memecoin_state_facts (
                fact_id TEXT PRIMARY KEY,
                state_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                status TEXT NOT NULL,
                value_kind TEXT,
                value_num TEXT,
                value_int TEXT,
                value_text TEXT,
                value_bool INTEGER,
                unit TEXT,
                source TEXT,
                source_observed_at TEXT,
                fetched_at TEXT,
                age_ms INTEGER,
                reason TEXT,
                details_json TEXT NOT NULL,
                FOREIGN KEY(state_id) REFERENCES memecoin_state_observations(state_id)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_observed
                ON observation_snapshots(observed_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_impulse
                ON observation_snapshots(impulse_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_snapshot
                ON experiment_decisions(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_group
                ON experiment_decisions(returned_model_id, requested_model_id, question_version);
            CREATE INDEX IF NOT EXISTS idx_outcomes_target
                ON forward_outcomes(status, target_at);
            CREATE INDEX IF NOT EXISTS idx_execution_snapshot
                ON execution_observations(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_measurement_events_recorded
                ON measurement_events(recorded_at);
            CREATE INDEX IF NOT EXISTS idx_measurement_events_kind
                ON measurement_events(kind, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_memestate_snapshot
                ON memecoin_state_observations(snapshot_id, ready_at);
            CREATE INDEX IF NOT EXISTS idx_memestate_ready
                ON memecoin_state_observations(ready_at);
            CREATE INDEX IF NOT EXISTS idx_memestate_facts_field
                ON memecoin_state_facts(field_name, status);
            CREATE INDEX IF NOT EXISTS idx_memestate_facts_state
                ON memecoin_state_facts(state_id);

            CREATE TABLE IF NOT EXISTS meme_chronology_observations (
                chronology_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
                chronology_version TEXT NOT NULL, started_at TEXT NOT NULL,
                ready_at TEXT NOT NULL, latency_ms INTEGER NOT NULL,
                source_cutoff_at TEXT NOT NULL, coverage_status TEXT NOT NULL,
                history_truncated INTEGER NOT NULL, reached_creation INTEGER NOT NULL,
                signature_count INTEGER NOT NULL, transaction_fetch_count INTEGER NOT NULL,
                transaction_unavailable_count INTEGER NOT NULL,
                unsupported_version_count INTEGER NOT NULL, decode_failure_count INTEGER NOT NULL,
                decoded_event_count INTEGER NOT NULL, oldest_slot INTEGER, newest_slot INTEGER,
                oldest_block_time TEXT, newest_block_time TEXT,
                chronology_hash TEXT NOT NULL, sources_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS meme_chronology_events (
                event_id TEXT PRIMARY KEY, chronology_id TEXT NOT NULL, signature TEXT NOT NULL,
                slot INTEGER NOT NULL, block_time TEXT, instruction_path TEXT NOT NULL,
                program_id TEXT NOT NULL, venue TEXT NOT NULL, event_type TEXT NOT NULL,
                user TEXT, mint TEXT, pool TEXT, creator TEXT, creation_user TEXT,
                amount_token_raw TEXT, amount_quote_raw TEXT,
                instruction_discriminator TEXT NOT NULL, decode_status TEXT NOT NULL,
                details_json TEXT NOT NULL,
                FOREIGN KEY(chronology_id) REFERENCES meme_chronology_observations(chronology_id)
            );
            CREATE TABLE IF NOT EXISTS meme_chronology_facts (
                fact_id TEXT PRIMARY KEY, chronology_id TEXT NOT NULL, field_name TEXT NOT NULL,
                status TEXT NOT NULL, value_kind TEXT, value_num TEXT, value_int TEXT,
                value_text TEXT, value_bool INTEGER, unit TEXT, source TEXT,
                source_observed_at TEXT, fetched_at TEXT, age_ms INTEGER, reason TEXT,
                details_json TEXT NOT NULL,
                FOREIGN KEY(chronology_id) REFERENCES meme_chronology_observations(chronology_id)
            );
            CREATE TABLE IF NOT EXISTS meme_funding_edges (
                edge_id TEXT PRIMARY KEY, chronology_id TEXT NOT NULL, target_wallet TEXT NOT NULL,
                source_wallet TEXT NOT NULL, funding_signature TEXT NOT NULL, slot INTEGER NOT NULL,
                block_time TEXT, lamports TEXT NOT NULL, seconds_before_first_buy INTEGER NOT NULL,
                source_kind TEXT NOT NULL, relation_to_creation_user INTEGER,
                relation_to_creation_creator INTEGER, details_json TEXT NOT NULL,
                FOREIGN KEY(chronology_id) REFERENCES meme_chronology_observations(chronology_id)
            );
            CREATE TABLE IF NOT EXISTS creator_launch_evidence (
                evidence_id TEXT PRIMARY KEY, chronology_id TEXT NOT NULL, creator TEXT NOT NULL,
                mint TEXT NOT NULL, creation_signature TEXT NOT NULL, slot INTEGER NOT NULL,
                block_time TEXT, is_current_mint INTEGER NOT NULL, source TEXT NOT NULL,
                details_json TEXT NOT NULL,
                FOREIGN KEY(chronology_id) REFERENCES meme_chronology_observations(chronology_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chronology_snapshot
                ON meme_chronology_observations(snapshot_id,ready_at);
            CREATE INDEX IF NOT EXISTS idx_chronology_events
                ON meme_chronology_events(chronology_id,slot,instruction_path);
            CREATE INDEX IF NOT EXISTS idx_chronology_facts
                ON meme_chronology_facts(chronology_id,field_name,status);
            CREATE INDEX IF NOT EXISTS idx_funding_chronology ON meme_funding_edges(chronology_id);
            CREATE INDEX IF NOT EXISTS idx_creator_launch_chronology
                ON creator_launch_evidence(chronology_id);
            """
        )
        self._migrate_forward_outcomes()
        self.conn.commit()

    def _migrate_forward_outcomes(self) -> None:
        """Rebuild the Phase 0 table without its status uniqueness constraint."""
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(forward_outcomes)").fetchall()
        }
        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='forward_outcomes'"
        ).fetchone()
        schema = "" if schema_row is None else str(schema_row["sql"] or "")
        compact_schema = "".join(schema.lower().split())
        has_status_unique = "unique(snapshot_id,horizon_seconds,status)" in compact_schema
        if "recorded_at" in columns and not has_status_unique:
            return

        old_count = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM forward_outcomes").fetchone()["n"]
        )
        recorded_at_expr = (
            "l.recorded_at"
            if "recorded_at" in columns
            else "COALESCE(l.resolved_at,s.captured_at,l.target_at)"
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("ALTER TABLE forward_outcomes RENAME TO forward_outcomes_legacy")
            self.conn.execute(
                """CREATE TABLE forward_outcomes (
                       outcome_id TEXT PRIMARY KEY,
                       recorded_at TEXT NOT NULL,
                       snapshot_id TEXT NOT NULL,
                       horizon_seconds INTEGER NOT NULL,
                       target_at TEXT NOT NULL,
                       resolved_at TEXT,
                       reference_price TEXT,
                       outcome_price TEXT,
                       return_pct TEXT,
                       realized_label INTEGER,
                       source TEXT NOT NULL,
                       status TEXT NOT NULL,
                       FOREIGN KEY(snapshot_id) REFERENCES observation_snapshots(snapshot_id)
                   )"""
            )
            self.conn.execute(
                f"""INSERT INTO forward_outcomes(
                        outcome_id,recorded_at,snapshot_id,horizon_seconds,target_at,
                        resolved_at,reference_price,outcome_price,return_pct,
                        realized_label,source,status
                    )
                    SELECT l.outcome_id,{recorded_at_expr},l.snapshot_id,l.horizon_seconds,l.target_at,
                           l.resolved_at,l.reference_price,l.outcome_price,l.return_pct,
                           l.realized_label,l.source,l.status
                    FROM forward_outcomes_legacy l
                    LEFT JOIN observation_snapshots s ON s.snapshot_id=l.snapshot_id"""
            )
            new_count = int(
                self.conn.execute("SELECT COUNT(*) AS n FROM forward_outcomes").fetchone()["n"]
            )
            if new_count != old_count:
                raise RuntimeError("forward outcome migration row count mismatch")
            self.conn.execute("DROP TABLE forward_outcomes_legacy")
            self.conn.execute(
                "CREATE INDEX idx_outcomes_target ON forward_outcomes(status, target_at)"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def save(self, impulse: Impulse) -> None:
        self.conn.execute(
            """INSERT INTO impulses(id,chain,token,pool,score,verdict,path,payload,created_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               score=excluded.score, verdict=excluded.verdict, path=excluded.path, payload=excluded.payload""",
            (impulse.id, str(impulse.chain), impulse.token, impulse.pool, impulse.score,
             impulse.verdict.value, impulse.path, impulse.model_dump_json(), impulse.created_at.isoformat()),
        )
        self.conn.commit()

    def log_transition(self, impulse: Impulse) -> None:
        if not impulse.history:
            return
        transition = impulse.history[-1]
        exists = self.conn.execute(
            "SELECT 1 FROM transitions WHERE impulse_id=? AND node=? AND ts=?",
            (impulse.id, transition.node.value, transition.ts.isoformat()),
        ).fetchone()
        if exists is None:
            self.conn.execute(
                "INSERT INTO transitions(impulse_id,node,verdict,note,ts) VALUES(?,?,?,?,?)",
                (impulse.id, transition.node.value, transition.verdict.value,
                 transition.note, transition.ts.isoformat()),
            )
            self.conn.commit()

    def create_intent(self, client_id: str, impulse_id: str, nonce: int | None, payload: dict[str, Any]) -> bool:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO intents(client_id,impulse_id,status,nonce,payload,updated_at) VALUES(?,?,?,?,?,?)",
            (client_id, impulse_id, "prepared", nonce, json.dumps(payload, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def update_intent(self, client_id: str, status: str, **fields: Any) -> None:
        row = self.conn.execute("SELECT payload FROM intents WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise KeyError(client_id)
        payload = json.loads(row["payload"])
        payload.update(fields)
        self.conn.execute(
            "UPDATE intents SET status=?,tx_hash=COALESCE(?,tx_hash),nonce=COALESCE(?,nonce),payload=?,updated_at=? WHERE client_id=?",
            (status, fields.get("tx_hash"), fields.get("nonce"), json.dumps(payload, sort_keys=True),
             datetime.now(UTC).isoformat(), client_id),
        )
        self.conn.commit()

    def get_intent(self, client_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM intents WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def unknown_intents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM intents WHERE status IN ('prepared','unknown','submitted')").fetchall()
        return [dict(row) for row in rows]

    def intents_with_status(self, status: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM intents WHERE status=? ORDER BY updated_at", (status,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at").fetchall()
        return [dict(row) for row in rows]

    def record_position(self, impulse: Impulse) -> None:
        self.conn.execute(
            """INSERT INTO positions(token,pool,size_usd,entry_price,tx_hash,opened_at,status)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET
               size_usd=excluded.size_usd, entry_price=excluded.entry_price,
               tx_hash=excluded.tx_hash, status='open'""",
            (impulse.token, impulse.pool, str(impulse.size_usd), str(impulse.entry_price),
             impulse.tx_hash, datetime.now(UTC).isoformat(), "open"),
        )
        self.conn.commit()

    def held_tokens(self) -> set[str]:
        rows = self.conn.execute("SELECT token FROM positions WHERE status='open'").fetchall()
        return {str(row["token"]) for row in rows}

    def open_position_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM positions WHERE status='open'").fetchone()
        return int(row["n"])

    def funnel(self, hours: int = 24) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute("SELECT verdict,COUNT(*) AS n FROM impulses WHERE created_at>=? GROUP BY verdict", (cutoff,)).fetchall()
        return {str(row["verdict"]): int(row["n"]) for row in rows}

    # Measurement sidecar -------------------------------------------------

    def record_observation_snapshot(self, snapshot: ObservationSnapshot) -> None:
        self.conn.execute(
            """INSERT INTO observation_snapshots(
                   snapshot_id,impulse_id,chain,token,pool,observed_at,captured_at,
                   stage,input_hash,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.snapshot_id,
                snapshot.impulse_id,
                snapshot.chain,
                snapshot.token,
                snapshot.pool,
                snapshot.observed_at.isoformat(),
                snapshot.captured_at.isoformat(),
                snapshot.stage,
                snapshot.input_hash,
                snapshot.payload_json,
            ),
        )
        self.conn.commit()

    def record_memecoin_state(self, state: MemecoinStateObservation) -> None:
        snapshot = self.conn.execute(
            "SELECT 1 FROM observation_snapshots WHERE snapshot_id=?", (state.snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise ValueError("memecoin state references an unknown snapshot")
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """INSERT INTO memecoin_state_observations(
                       state_id,snapshot_id,state_version,started_at,ready_at,latency_ms,
                       state_hash,status,sources_json,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    state.state_id,
                    state.snapshot_id,
                    state.state_version,
                    state.started_at.isoformat(),
                    state.ready_at.isoformat(),
                    state.latency_ms,
                    state.state_hash,
                    state.status.value,
                    state.sources_json,
                    state.payload_json,
                ),
            )
            self.conn.executemany(
                """INSERT INTO memecoin_state_facts(
                       fact_id,state_id,field_name,status,value_kind,value_num,value_int,
                       value_text,value_bool,unit,source,source_observed_at,fetched_at,
                       age_ms,reason,details_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        fact.fact_id,
                        state.state_id,
                        fact.field_name,
                        fact.status.value,
                        fact.value_kind,
                        str(fact.value_num) if fact.value_num is not None else None,
                        str(fact.value_int) if fact.value_int is not None else None,
                        fact.value_text,
                        int(fact.value_bool) if fact.value_bool is not None else None,
                        fact.unit,
                        fact.source,
                        fact.source_observed_at.isoformat() if fact.source_observed_at else None,
                        fact.fetched_at.isoformat() if fact.fetched_at else None,
                        fact.age_ms,
                        fact.reason,
                        fact.details_json,
                    )
                    for fact in state.facts
                ],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def list_memecoin_states(
        self, snapshot_id: str | None = None
    ) -> list[MemecoinStateObservation]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM memecoin_state_observations ORDER BY ready_at,rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM memecoin_state_observations WHERE snapshot_id=?
                   ORDER BY ready_at,rowid""",
                (snapshot_id,),
            ).fetchall()
        return [self._memecoin_state_from_row(row) for row in rows]

    def latest_memecoin_state(self, snapshot_id: str) -> MemecoinStateObservation | None:
        row = self.conn.execute(
            """SELECT * FROM memecoin_state_observations
               WHERE snapshot_id=? AND status IN ('success','partial')
               ORDER BY ready_at DESC,rowid DESC LIMIT 1""",
            (snapshot_id,),
        ).fetchone()
        return self._memecoin_state_from_row(row) if row is not None else None

    def _memecoin_state_from_row(self, row: sqlite3.Row) -> MemecoinStateObservation:
        fact_rows = self.conn.execute(
            "SELECT * FROM memecoin_state_facts WHERE state_id=? ORDER BY rowid",
            (row["state_id"],),
        ).fetchall()
        facts = tuple(
            MemecoinStateFact(
                fact_id=str(fact["fact_id"]),
                field_name=str(fact["field_name"]),
                status=FactStatus(str(fact["status"])),
                value_num=(Decimal(str(fact["value_num"])) if fact["value_num"] is not None else None),
                value_int=(int(str(fact["value_int"])) if fact["value_int"] is not None else None),
                value_text=(str(fact["value_text"]) if fact["value_text"] is not None else None),
                value_bool=(bool(fact["value_bool"]) if fact["value_bool"] is not None else None),
                unit=(str(fact["unit"]) if fact["unit"] is not None else None),
                source=(str(fact["source"]) if fact["source"] is not None else None),
                source_observed_at=(
                    datetime.fromisoformat(str(fact["source_observed_at"]))
                    if fact["source_observed_at"] is not None
                    else None
                ),
                fetched_at=(
                    datetime.fromisoformat(str(fact["fetched_at"]))
                    if fact["fetched_at"] is not None
                    else None
                ),
                age_ms=(int(fact["age_ms"]) if fact["age_ms"] is not None else None),
                reason=(str(fact["reason"]) if fact["reason"] is not None else None),
                details_json=str(fact["details_json"]),
            )
            for fact in fact_rows
        )
        return MemecoinStateObservation(
            state_id=str(row["state_id"]),
            snapshot_id=str(row["snapshot_id"]),
            state_version=str(row["state_version"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            ready_at=datetime.fromisoformat(str(row["ready_at"])),
            latency_ms=int(row["latency_ms"]),
            state_hash=str(row["state_hash"]),
            status=StateStatus(str(row["status"])),
            sources_json=str(row["sources_json"]),
            payload_json=str(row["payload_json"]),
            facts=facts,
        )

    def record_meme_chronology(self, item: MemeChronologyObservation) -> None:
        if self.get_observation_snapshot(item.snapshot_id) is None:
            raise ValueError("chronology references an unknown snapshot")
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """INSERT INTO meme_chronology_observations VALUES(
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.chronology_id, item.snapshot_id, item.chronology_version,
                    item.started_at.isoformat(), item.ready_at.isoformat(), item.latency_ms,
                    item.source_cutoff_at.isoformat(), item.coverage_status.value,
                    int(item.history_truncated), int(item.reached_creation), item.signature_count,
                    item.transaction_fetch_count, item.transaction_unavailable_count,
                    item.unsupported_version_count, item.decode_failure_count,
                    item.decoded_event_count, item.oldest_slot, item.newest_slot,
                    item.oldest_block_time.isoformat() if item.oldest_block_time else None,
                    item.newest_block_time.isoformat() if item.newest_block_time else None,
                    item.chronology_hash, item.sources_json, item.payload_json,
                ),
            )
            self.conn.executemany(
                """INSERT INTO meme_chronology_events VALUES(
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        event.event_id, item.chronology_id, event.signature, event.slot,
                        event.block_time.isoformat() if event.block_time else None,
                        event.instruction_path, event.program_id, event.venue,
                        event.event_type.value, event.user, event.mint, event.pool, event.creator,
                        event.creation_user,
                        str(event.amount_token_raw) if event.amount_token_raw is not None else None,
                        str(event.amount_quote_raw) if event.amount_quote_raw is not None else None,
                        event.instruction_discriminator, event.decode_status.value, event.details_json,
                    )
                    for event in item.events
                ],
            )
            self.conn.executemany(
                """INSERT INTO meme_chronology_facts VALUES(
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        fact.fact_id, item.chronology_id, fact.field_name, fact.status.value,
                        fact.value_kind,
                        str(fact.value_num) if fact.value_num is not None else None,
                        str(fact.value_int) if fact.value_int is not None else None,
                        fact.value_text,
                        int(fact.value_bool) if fact.value_bool is not None else None,
                        fact.unit, fact.source,
                        fact.source_observed_at.isoformat() if fact.source_observed_at else None,
                        fact.fetched_at.isoformat() if fact.fetched_at else None,
                        fact.age_ms, fact.reason, fact.details_json,
                    )
                    for fact in item.facts
                ],
            )
            self.conn.executemany(
                """INSERT INTO meme_funding_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        edge.edge_id, item.chronology_id, edge.target_wallet, edge.source_wallet,
                        edge.funding_signature, edge.slot,
                        edge.block_time.isoformat() if edge.block_time else None,
                        str(edge.lamports), edge.seconds_before_first_buy, edge.source_kind,
                        _optional_bool(edge.relation_to_creation_user),
                        _optional_bool(edge.relation_to_creation_creator), edge.details_json,
                    )
                    for edge in item.funding_edges
                ],
            )
            self.conn.executemany(
                """INSERT INTO creator_launch_evidence VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        launch.evidence_id, item.chronology_id, launch.creator, launch.mint,
                        launch.creation_signature, launch.slot,
                        launch.block_time.isoformat() if launch.block_time else None,
                        int(launch.is_current_mint), launch.source, launch.details_json,
                    )
                    for launch in item.creator_launches
                ],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def list_meme_chronologies(
        self, snapshot_id: str | None = None
    ) -> list[MemeChronologyObservation]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM meme_chronology_observations ORDER BY ready_at,rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM meme_chronology_observations WHERE snapshot_id=?
                   ORDER BY ready_at,rowid""",
                (snapshot_id,),
            ).fetchall()
        return [self._meme_chronology_from_row(row) for row in rows]

    def latest_meme_chronology(self, snapshot_id: str) -> MemeChronologyObservation | None:
        row = self.conn.execute(
            """SELECT * FROM meme_chronology_observations WHERE snapshot_id=?
               AND coverage_status IN ('complete_since_creation','partial')
               ORDER BY ready_at DESC,rowid DESC LIMIT 1""",
            (snapshot_id,),
        ).fetchone()
        return self._meme_chronology_from_row(row) if row is not None else None

    def _meme_chronology_from_row(self, row: sqlite3.Row) -> MemeChronologyObservation:
        chronology_id = str(row["chronology_id"])
        events = tuple(
            ChronologyEvent(
                event_id=str(event["event_id"]), signature=str(event["signature"]),
                slot=int(event["slot"]),
                block_time=_optional_datetime(event["block_time"]),
                instruction_path=str(event["instruction_path"]),
                program_id=str(event["program_id"]), venue=str(event["venue"]),
                event_type=ChronologyEventType(str(event["event_type"])),
                user=_optional_str(event["user"]), mint=_optional_str(event["mint"]),
                pool=_optional_str(event["pool"]), creator=_optional_str(event["creator"]),
                creation_user=_optional_str(event["creation_user"]),
                amount_token_raw=_optional_int(event["amount_token_raw"]),
                amount_quote_raw=_optional_int(event["amount_quote_raw"]),
                instruction_discriminator=str(event["instruction_discriminator"]),
                decode_status=DecodeStatus(str(event["decode_status"])),
                details_json=str(event["details_json"]),
            )
            for event in self.conn.execute(
                "SELECT * FROM meme_chronology_events WHERE chronology_id=? ORDER BY slot,instruction_path,rowid",
                (chronology_id,),
            ).fetchall()
        )
        facts = tuple(
            _fact_from_row(fact)
            for fact in self.conn.execute(
                "SELECT * FROM meme_chronology_facts WHERE chronology_id=? ORDER BY rowid",
                (chronology_id,),
            ).fetchall()
        )
        edges = tuple(
            FundingEdge(
                edge_id=str(edge["edge_id"]), target_wallet=str(edge["target_wallet"]),
                source_wallet=str(edge["source_wallet"]),
                funding_signature=str(edge["funding_signature"]), slot=int(edge["slot"]),
                block_time=_optional_datetime(edge["block_time"]), lamports=int(edge["lamports"]),
                seconds_before_first_buy=int(edge["seconds_before_first_buy"]),
                source_kind=str(edge["source_kind"]),
                relation_to_creation_user=_db_bool(edge["relation_to_creation_user"]),
                relation_to_creation_creator=_db_bool(edge["relation_to_creation_creator"]),
                details_json=str(edge["details_json"]),
            )
            for edge in self.conn.execute(
                "SELECT * FROM meme_funding_edges WHERE chronology_id=? ORDER BY rowid",
                (chronology_id,),
            ).fetchall()
        )
        launches = tuple(
            CreatorLaunchEvidence(
                evidence_id=str(launch["evidence_id"]), creator=str(launch["creator"]),
                mint=str(launch["mint"]), creation_signature=str(launch["creation_signature"]),
                slot=int(launch["slot"]), block_time=_optional_datetime(launch["block_time"]),
                is_current_mint=bool(launch["is_current_mint"]), source=str(launch["source"]),
                details_json=str(launch["details_json"]),
            )
            for launch in self.conn.execute(
                "SELECT * FROM creator_launch_evidence WHERE chronology_id=? ORDER BY rowid",
                (chronology_id,),
            ).fetchall()
        )
        return MemeChronologyObservation(
            chronology_id=chronology_id, snapshot_id=str(row["snapshot_id"]),
            chronology_version=str(row["chronology_version"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            ready_at=datetime.fromisoformat(str(row["ready_at"])), latency_ms=int(row["latency_ms"]),
            source_cutoff_at=datetime.fromisoformat(str(row["source_cutoff_at"])),
            coverage_status=ChronologyCoverage(str(row["coverage_status"])),
            history_truncated=bool(row["history_truncated"]),
            reached_creation=bool(row["reached_creation"]), signature_count=int(row["signature_count"]),
            transaction_fetch_count=int(row["transaction_fetch_count"]),
            transaction_unavailable_count=int(row["transaction_unavailable_count"]),
            unsupported_version_count=int(row["unsupported_version_count"]),
            decode_failure_count=int(row["decode_failure_count"]),
            decoded_event_count=int(row["decoded_event_count"]),
            oldest_slot=_optional_int(row["oldest_slot"]), newest_slot=_optional_int(row["newest_slot"]),
            oldest_block_time=_optional_datetime(row["oldest_block_time"]),
            newest_block_time=_optional_datetime(row["newest_block_time"]),
            chronology_hash=str(row["chronology_hash"]), sources_json=str(row["sources_json"]),
            payload_json=str(row["payload_json"]), events=events, facts=facts,
            funding_edges=edges, creator_launches=launches,
        )

    def get_observation_snapshot(self, snapshot_id: str) -> ObservationSnapshot | None:
        row = self.conn.execute(
            "SELECT * FROM observation_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def list_observation_snapshots(self, limit: int | None = None) -> list[ObservationSnapshot]:
        sql = "SELECT * FROM observation_snapshots ORDER BY observed_at DESC"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [self._snapshot_from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> ObservationSnapshot:
        return ObservationSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            impulse_id=str(row["impulse_id"]),
            chain=str(row["chain"]),
            token=str(row["token"]),
            pool=str(row["pool"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            captured_at=datetime.fromisoformat(str(row["captured_at"])),
            stage=str(row["stage"]),
            input_hash=str(row["input_hash"]),
            payload_json=str(row["payload_json"]),
        )

    def record_experiment_decision(self, decision: ExperimentDecision) -> None:
        snapshot = self.conn.execute(
            "SELECT input_hash FROM observation_snapshots WHERE snapshot_id=?",
            (decision.snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise ValueError("experiment decision references an unknown snapshot")
        if str(snapshot["input_hash"]) != decision.input_hash:
            raise ValueError("experiment decision input_hash does not match its snapshot")
        self.conn.execute(
            """INSERT INTO experiment_decisions(
                   decision_id,snapshot_id,input_hash,arm_id,strategy_id,provider,
                   requested_model_id,returned_model_id,question_version,status,
                   started_at,completed_at,deadline_at,applied_at,latency_ms,
                   raw_answer_json,reason,flags_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision.decision_id,
                decision.snapshot_id,
                decision.input_hash,
                decision.arm_id,
                decision.strategy_id,
                decision.provider,
                decision.requested_model_id,
                decision.returned_model_id,
                decision.question_version,
                decision.status.value,
                decision.started_at.isoformat(),
                decision.completed_at.isoformat(),
                decision.deadline_at.isoformat(),
                decision.applied_at.isoformat() if decision.applied_at else None,
                decision.latency_ms,
                canonical_json(decision.raw_answer) if decision.raw_answer is not None else None,
                decision.reason,
                canonical_json(decision.flags),
            ),
        )
        self.conn.commit()

    def list_experiment_decisions(self, snapshot_id: str | None = None) -> list[ExperimentDecision]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM experiment_decisions ORDER BY started_at"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM experiment_decisions WHERE snapshot_id=? ORDER BY started_at",
                (snapshot_id,),
            ).fetchall()
        return [self._decision_from_row(row) for row in rows]

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> ExperimentDecision:
        return ExperimentDecision(
            decision_id=str(row["decision_id"]),
            snapshot_id=str(row["snapshot_id"]),
            input_hash=str(row["input_hash"]),
            arm_id=str(row["arm_id"]),
            strategy_id=str(row["strategy_id"]),
            provider=row["provider"],
            requested_model_id=row["requested_model_id"],
            returned_model_id=row["returned_model_id"],
            question_version=str(row["question_version"]),
            status=DecisionStatus(str(row["status"])),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            completed_at=datetime.fromisoformat(str(row["completed_at"])),
            deadline_at=datetime.fromisoformat(str(row["deadline_at"])),
            applied_at=(datetime.fromisoformat(str(row["applied_at"])) if row["applied_at"] else None),
            latency_ms=int(row["latency_ms"]),
            raw_answer=(json.loads(str(row["raw_answer_json"])) if row["raw_answer_json"] else None),
            reason=str(row["reason"]),
            flags=json.loads(str(row["flags_json"])),
        )

    def record_forward_outcome(self, outcome: ForwardOutcome) -> None:
        self.conn.execute(
            """INSERT INTO forward_outcomes(
                   outcome_id,recorded_at,snapshot_id,horizon_seconds,target_at,resolved_at,reference_price,
                   outcome_price,return_pct,realized_label,source,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                outcome.outcome_id,
                outcome.recorded_at.isoformat(),
                outcome.snapshot_id,
                outcome.horizon_seconds,
                outcome.target_at.isoformat(),
                outcome.resolved_at.isoformat() if outcome.resolved_at else None,
                str(outcome.reference_price) if outcome.reference_price is not None else None,
                str(outcome.outcome_price) if outcome.outcome_price is not None else None,
                str(outcome.return_pct) if outcome.return_pct is not None else None,
                int(outcome.realized_label) if outcome.realized_label is not None else None,
                outcome.source,
                outcome.status.value,
            ),
        )
        self.conn.commit()

    def list_forward_outcomes(self, snapshot_id: str | None = None) -> list[ForwardOutcome]:
        """Return the latest event for each snapshot/horizon pair."""
        if snapshot_id is None:
            rows = self.conn.execute(
                """SELECT * FROM (
                       SELECT event.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY snapshot_id,horizon_seconds
                                  ORDER BY recorded_at DESC,rowid DESC
                              ) AS event_rank
                       FROM forward_outcomes event
                   ) WHERE event_rank=1
                   ORDER BY target_at,horizon_seconds"""
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM (
                       SELECT event.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY snapshot_id,horizon_seconds
                                  ORDER BY recorded_at DESC,rowid DESC
                              ) AS event_rank
                       FROM forward_outcomes event WHERE snapshot_id=?
                   ) WHERE event_rank=1
                   ORDER BY horizon_seconds""",
                (snapshot_id,),
            ).fetchall()
        return [self._outcome_from_row(row) for row in rows]

    def list_forward_outcome_events(self, snapshot_id: str | None = None) -> list[ForwardOutcome]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM forward_outcomes ORDER BY recorded_at,rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM forward_outcomes WHERE snapshot_id=?
                   ORDER BY recorded_at,rowid""",
                (snapshot_id,),
            ).fetchall()
        return [self._outcome_from_row(row) for row in rows]

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> ForwardOutcome:
        return ForwardOutcome(
            outcome_id=str(row["outcome_id"]),
            recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
            snapshot_id=str(row["snapshot_id"]),
            horizon_seconds=int(row["horizon_seconds"]),
            target_at=datetime.fromisoformat(str(row["target_at"])),
            resolved_at=(datetime.fromisoformat(str(row["resolved_at"])) if row["resolved_at"] else None),
            reference_price=row["reference_price"],
            outcome_price=row["outcome_price"],
            return_pct=row["return_pct"],
            realized_label=(bool(row["realized_label"]) if row["realized_label"] is not None else None),
            source=str(row["source"]),
            status=OutcomeStatus(str(row["status"])),
        )

    def record_measurement_event(self, event: MeasurementEvent) -> None:
        self.conn.execute(
            """INSERT INTO measurement_events(
                   event_id,recorded_at,kind,stage,impulse_id,snapshot_id,
                   error_type,message,details_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.recorded_at.isoformat(),
                event.kind.value,
                event.stage,
                event.impulse_id,
                event.snapshot_id,
                event.error_type,
                event.message,
                canonical_json(event.details),
            ),
        )
        self.conn.commit()

    def list_measurement_events(
        self, kind: MeasurementEventKind | None = None
    ) -> list[MeasurementEvent]:
        if kind is None:
            rows = self.conn.execute(
                "SELECT * FROM measurement_events ORDER BY recorded_at,rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM measurement_events WHERE kind=?
                   ORDER BY recorded_at,rowid""",
                (kind.value,),
            ).fetchall()
        return [
            MeasurementEvent(
                event_id=str(row["event_id"]),
                recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
                kind=MeasurementEventKind(str(row["kind"])),
                stage=str(row["stage"]),
                impulse_id=str(row["impulse_id"]),
                snapshot_id=(str(row["snapshot_id"]) if row["snapshot_id"] else None),
                error_type=(str(row["error_type"]) if row["error_type"] else None),
                message=str(row["message"]),
                details=json.loads(str(row["details_json"])),
            )
            for row in rows
        ]

    def record_execution_observation(self, observation: ExecutionObservation) -> None:
        if observation.executable_entry is not None:
            decision = self.conn.execute(
                """SELECT status,completed_at FROM experiment_decisions
                   WHERE snapshot_id=? AND arm_id=?""",
                (observation.snapshot_id, observation.arm_id),
            ).fetchone()
            if decision is None:
                raise ValueError("an executable observation requires its audited experiment decision")
            if str(decision["status"]) == DecisionStatus.EXPIRED.value:
                raise ValueError("an expired experiment decision cannot become executable")
            completed_at = datetime.fromisoformat(str(decision["completed_at"]))
            if observation.quote_at is None or observation.quote_at < completed_at:
                raise ValueError("executable quote cannot precede decision completion")
        self.conn.execute(
            """INSERT INTO execution_observations(
                   observation_id,snapshot_id,arm_id,signal_price,quote_at,quote_price,
                   obtainable_quantity,fee,impact_bps,executable_entry,executable_exit,
                   net_return,source,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation.observation_id,
                observation.snapshot_id,
                observation.arm_id,
                str(observation.signal_price) if observation.signal_price is not None else None,
                observation.quote_at.isoformat() if observation.quote_at else None,
                str(observation.quote_price) if observation.quote_price is not None else None,
                (str(observation.obtainable_quantity) if observation.obtainable_quantity is not None else None),
                str(observation.fee) if observation.fee is not None else None,
                observation.impact_bps,
                (str(observation.executable_entry) if observation.executable_entry is not None else None),
                (str(observation.executable_exit) if observation.executable_exit is not None else None),
                str(observation.net_return) if observation.net_return is not None else None,
                observation.source,
                observation.status,
            ),
        )
        self.conn.commit()

    def list_execution_observations(self, snapshot_id: str | None = None) -> list[ExecutionObservation]:
        if snapshot_id is None:
            rows = self.conn.execute(
                "SELECT * FROM execution_observations ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM execution_observations WHERE snapshot_id=? ORDER BY rowid",
                (snapshot_id,),
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionObservation:
        return ExecutionObservation(
            observation_id=str(row["observation_id"]),
            snapshot_id=str(row["snapshot_id"]),
            arm_id=str(row["arm_id"]),
            signal_price=row["signal_price"],
            quote_at=(datetime.fromisoformat(str(row["quote_at"])) if row["quote_at"] else None),
            quote_price=row["quote_price"],
            obtainable_quantity=row["obtainable_quantity"],
            fee=row["fee"],
            impact_bps=row["impact_bps"],
            executable_entry=row["executable_entry"],
            executable_exit=row["executable_exit"],
            net_return=row["net_return"],
            source=str(row["source"]),
            status=str(row["status"]),
        )

    def arm_sample_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT arm_id,COUNT(*) AS n FROM experiment_decisions GROUP BY arm_id"
        ).fetchall()
        return {str(row["arm_id"]): int(row["n"]) for row in rows}

    def calibration_samples(self) -> dict[tuple[str, str, int], list[tuple[float, bool]]]:
        """Return strictly versioned probability/label pairs for reporting."""
        rows = self.conn.execute(
            """WITH latest_outcomes AS (
                   SELECT ranked.* FROM (
                       SELECT outcome.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY snapshot_id,horizon_seconds
                                  ORDER BY recorded_at DESC,rowid DESC
                              ) AS outcome_rank
                       FROM forward_outcomes outcome
                   ) ranked WHERE outcome_rank=1
               )
               SELECT d.requested_model_id,d.returned_model_id,d.question_version,
                      d.raw_answer_json,o.horizon_seconds,o.realized_label
               FROM experiment_decisions d
               JOIN latest_outcomes o ON o.snapshot_id=d.snapshot_id
               WHERE o.status='resolved' AND o.realized_label IS NOT NULL
                 AND d.raw_answer_json IS NOT NULL"""
        ).fetchall()
        result: dict[tuple[str, str, int], list[tuple[float, bool]]] = {}
        for row in rows:
            answer = json.loads(str(row["raw_answer_json"]))
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict):
                continue
            probability = probabilities.get(str(row["horizon_seconds"]))
            if not isinstance(probability, (int, float)) or isinstance(probability, bool):
                continue
            if not 0 <= float(probability) <= 1:
                continue
            model_id = row["returned_model_id"] or row["requested_model_id"]
            if not model_id:
                continue
            key = (str(model_id), str(row["question_version"]), int(row["horizon_seconds"]))
            result.setdefault(key, []).append((float(probability), bool(row["realized_label"])))
        return result


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    return int(str(value)) if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_bool(value: bool | None) -> int | None:
    return int(value) if value is not None else None


def _db_bool(value: object) -> bool | None:
    return bool(value) if value is not None else None


def _fact_from_row(fact: sqlite3.Row) -> MemecoinStateFact:
    return MemecoinStateFact(
        fact_id=str(fact["fact_id"]), field_name=str(fact["field_name"]),
        status=FactStatus(str(fact["status"])),
        value_num=Decimal(str(fact["value_num"])) if fact["value_num"] is not None else None,
        value_int=_optional_int(fact["value_int"]), value_text=_optional_str(fact["value_text"]),
        value_bool=_db_bool(fact["value_bool"]), unit=_optional_str(fact["unit"]),
        source=_optional_str(fact["source"]),
        source_observed_at=_optional_datetime(fact["source_observed_at"]),
        fetched_at=_optional_datetime(fact["fetched_at"]), age_ms=_optional_int(fact["age_ms"]),
        reason=_optional_str(fact["reason"]), details_json=str(fact["details_json"]),
    )
