from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

from ..lab.metrics import (
    BinaryPrediction,
    binary_log_loss,
    brier_score,
    expected_calibration_error,
    reliability_bins,
)

DEFAULT_HORIZONS = (60, 300, 900, 1800)


class ObservatoryReadStore:
    """Read model backed by a SQLite connection that cannot write."""

    def __init__(self, db_path: Path) -> None:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ObservatoryReadStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _has_column(self, table: str, column: str) -> bool:
        if not self.has_table(table):
            return False
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row["name"]) == column for row in rows)

    def _latest_outcomes_sql(self, where: str = "") -> str:
        recorded = (
            "recorded_at"
            if self._has_column("forward_outcomes", "recorded_at")
            else "COALESCE(resolved_at,target_at)"
        )
        return f"""SELECT * FROM (
                       SELECT event.*,
                              {recorded} AS event_recorded_at,
                              ROW_NUMBER() OVER (
                                  PARTITION BY snapshot_id,horizon_seconds
                                  ORDER BY {recorded} DESC,rowid DESC
                              ) AS event_rank
                       FROM forward_outcomes event {where}
                   ) WHERE event_rank=1"""

    @staticmethod
    def _plain(row: sqlite3.Row) -> dict[str, Any]:
        return {str(key): row[key] for key in row.keys() if key not in {"event_rank"}}

    @staticmethod
    def _load_json(value: object) -> Any:
        if value is None:
            return None
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return None

    def summary(self) -> dict[str, Any]:
        snapshot_row = self.conn.execute(
            """SELECT COUNT(*) AS snapshot_count,
                      COUNT(DISTINCT token) AS unique_tokens,
                      COUNT(DISTINCT pool) AS unique_pools,
                      MAX(observed_at) AS latest_observation_at
               FROM observation_snapshots"""
        ).fetchone()
        decision_row = self.conn.execute(
            """SELECT COUNT(*) AS decision_count,
                      SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_count
               FROM experiment_decisions"""
        ).fetchone()
        latest = self.latest_forward_outcomes()
        unresolved = sum(row["status"] != "resolved" for row in latest)
        health = self.health()
        return {
            "truth_store": "READ ONLY",
            "snapshot_count": int(snapshot_row["snapshot_count"]),
            "unique_token_count": int(snapshot_row["unique_tokens"]),
            "unique_pool_count": int(snapshot_row["unique_pools"]),
            "decision_count": int(decision_row["decision_count"]),
            "expired_decision_count": int(decision_row["expired_count"] or 0),
            "unresolved_outcome_count": unresolved,
            "measurement_error_count": int(health["failure_count"]),
            "latest_observation_at": snapshot_row["latest_observation_at"],
            "arms": self.arm_comparison(),
            "forward_lab": self.forward_lab(latest),
            "memecoin_state": self.memecoin_state_summary(),
        }

    def recent_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        rows = self.conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(DISTINCT d.arm_id)
                       FROM experiment_decisions d
                       WHERE d.snapshot_id=s.snapshot_id) AS decision_arm_count
               FROM observation_snapshots s
               ORDER BY observed_at DESC,captured_at DESC
               LIMIT ?""",
            (safe_limit,),
        ).fetchall()
        latest_by_snapshot: dict[str, list[str]] = defaultdict(list)
        for outcome in self.latest_forward_outcomes():
            latest_by_snapshot[str(outcome["snapshot_id"])].append(str(outcome["status"]))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._plain(row)
            statuses = latest_by_snapshot.get(str(row["snapshot_id"]), [])
            item["outcome_state"] = self._outcome_state(statuses)
            item.pop("payload_json", None)
            result.append(item)
        return result

    @staticmethod
    def _outcome_state(statuses: list[str]) -> str:
        if not statuses:
            return "unavailable"
        if all(status == "resolved" for status in statuses):
            return "resolved"
        if any(status == "pending" for status in statuses):
            return "pending"
        if all(status == "unavailable" for status in statuses):
            return "unavailable"
        return "mixed"

    def snapshot_detail(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM observation_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        snapshot = self._plain(row)
        snapshot["payload"] = self._load_json(snapshot.pop("payload_json", None))
        return {
            "snapshot": snapshot,
            "decisions": self.decisions(snapshot_id),
            "forward_outcome_events": self.forward_outcome_events(snapshot_id),
            "latest_forward_outcomes": self.latest_forward_outcomes(snapshot_id),
            "execution_observations": self.execution_observations(snapshot_id),
            "memecoin_state": self.memecoin_state_detail(snapshot_id),
        }

    def memecoin_state_detail(self, snapshot_id: str) -> dict[str, Any]:
        if not self.has_table("memecoin_state_observations"):
            return {"history": [], "latest": None, "facts": []}
        rows = self.conn.execute(
            """SELECT * FROM memecoin_state_observations WHERE snapshot_id=?
               ORDER BY ready_at,rowid""",
            (snapshot_id,),
        ).fetchall()
        history = []
        for row in rows:
            item = self._plain(row)
            item["sources"] = self._load_json(item.pop("sources_json", None))
            item["payload"] = self._load_json(item.pop("payload_json", None))
            history.append(item)
        latest_row = self.conn.execute(
            """SELECT * FROM memecoin_state_observations
               WHERE snapshot_id=? AND status IN ('success','partial')
               ORDER BY ready_at DESC,rowid DESC LIMIT 1""",
            (snapshot_id,),
        ).fetchone()
        if latest_row is None:
            return {"history": history, "latest": None, "facts": []}
        latest = self._plain(latest_row)
        latest["sources"] = self._load_json(latest.pop("sources_json", None))
        latest["payload"] = self._load_json(latest.pop("payload_json", None))
        facts = []
        fact_rows = self.conn.execute(
            """SELECT * FROM memecoin_state_facts WHERE state_id=?
               ORDER BY field_name,rowid""",
            (latest["state_id"],),
        ).fetchall()
        for row in fact_rows:
            fact = self._plain(row)
            fact["details"] = self._load_json(fact.pop("details_json", None))
            kind = fact.get("value_kind")
            if kind == "num":
                fact["value"] = fact.get("value_num")
            elif kind == "int":
                fact["value"] = int(fact["value_int"]) if fact.get("value_int") is not None else None
            elif kind == "text":
                fact["value"] = fact.get("value_text")
            elif kind == "bool":
                fact["value"] = bool(fact["value_bool"]) if fact.get("value_bool") is not None else None
            else:
                fact["value"] = None
            facts.append(fact)
        return {"history": history, "latest": latest, "facts": facts}

    def memecoin_state_summary(self) -> dict[str, Any]:
        if not self.has_table("memecoin_state_observations"):
            return {
                "observation_count": 0,
                "successful_count": 0,
                "failure_count": 0,
                "median_latency_ms": None,
                "p95_latency_ms": None,
                "field_coverage": [],
                "lifecycle_counts": {},
            }
        rows = self.conn.execute(
            "SELECT status,latency_ms FROM memecoin_state_observations"
        ).fetchall()
        latencies = sorted(int(row["latency_ms"]) for row in rows)
        p95_index = max(0, int((len(latencies) - 1) * 0.95)) if latencies else 0
        coverage = [
            {
                "field_name": str(row["field_name"]),
                "observed_count": int(row["observed_count"] or 0),
                "unavailable_count": int(row["unavailable_count"] or 0),
                "stale_count": int(row["stale_count"] or 0),
                "invalid_count": int(row["invalid_count"] or 0),
                "conflict_count": int(row["conflict_count"] or 0),
            }
            for row in self.conn.execute(
                """SELECT field_name,
                          SUM(status='observed') AS observed_count,
                          SUM(status='unavailable') AS unavailable_count,
                          SUM(status='stale') AS stale_count,
                          SUM(status='invalid') AS invalid_count,
                          SUM(status='conflict') AS conflict_count
                   FROM memecoin_state_facts GROUP BY field_name ORDER BY field_name"""
            ).fetchall()
        ]
        lifecycle_counts = {
            str(row["value_text"]): int(row["n"])
            for row in self.conn.execute(
                """SELECT value_text,COUNT(*) AS n FROM memecoin_state_facts
                   WHERE field_name='pump_lifecycle_state' AND status='observed'
                   GROUP BY value_text"""
            ).fetchall()
        }
        event_failure_count = 0
        if self.has_table("measurement_events"):
            event_failure_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM measurement_events WHERE kind='memestate_failed'"
                ).fetchone()["n"]
            )
        return {
            "observation_count": len(rows),
            "successful_count": sum(row["status"] in {"success", "partial"} for row in rows),
            "failure_count": sum(row["status"] == "failed" for row in rows)
            + event_failure_count,
            "median_latency_ms": median(latencies) if latencies else None,
            "p95_latency_ms": latencies[p95_index] if latencies else None,
            "field_coverage": coverage,
            "lifecycle_counts": lifecycle_counts,
        }

    def decisions(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM experiment_decisions WHERE snapshot_id=?
               ORDER BY started_at,completed_at,decision_id""",
            (snapshot_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._plain(row)
            item["raw_answer"] = self._load_json(item.pop("raw_answer_json", None))
            item["flags"] = self._load_json(item.pop("flags_json", None))
            item["display_arm"] = self._display_arm(item)
            result.append(item)
        return result

    @staticmethod
    def _display_arm(decision: dict[str, Any]) -> str:
        if (
            decision.get("arm_id") == "CONTROL"
            and decision.get("strategy_id") == "control-abstain-v1"
        ):
            return "CONTROL · ABSTAIN"
        return str(decision.get("arm_id") or "Unavailable")

    def forward_outcome_events(self, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        recorded = (
            "recorded_at"
            if self._has_column("forward_outcomes", "recorded_at")
            else "COALESCE(resolved_at,target_at)"
        )
        if snapshot_id is None:
            rows = self.conn.execute(
                f"""SELECT *,{recorded} AS event_recorded_at FROM forward_outcomes
                    ORDER BY {recorded},rowid"""
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""SELECT *,{recorded} AS event_recorded_at FROM forward_outcomes
                    WHERE snapshot_id=? ORDER BY {recorded},rowid""",
                (snapshot_id,),
            ).fetchall()
        return [self._plain(row) for row in rows]

    def latest_forward_outcomes(self, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE snapshot_id=?" if snapshot_id is not None else ""
        rows = self.conn.execute(
            self._latest_outcomes_sql(where) + " ORDER BY target_at,horizon_seconds",
            (snapshot_id,) if snapshot_id is not None else (),
        ).fetchall()
        return [self._plain(row) for row in rows]

    def execution_observations(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM execution_observations WHERE snapshot_id=?
               ORDER BY quote_at,observation_id""",
            (snapshot_id,),
        ).fetchall()
        return [self._plain(row) for row in rows]

    def arms(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT arm_id,strategy_id,COUNT(*) AS decision_count,
                      SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_count,
                      SUM(CASE WHEN status='abstained' THEN 1 ELSE 0 END) AS abstained_count
               FROM experiment_decisions GROUP BY arm_id,strategy_id
               ORDER BY arm_id,strategy_id"""
        ).fetchall()
        execution_rows = {
            str(row["arm_id"]): row
            for row in self.conn.execute(
                """SELECT arm_id,COUNT(*) AS observation_count,
                          SUM(CASE WHEN quote_price IS NOT NULL THEN 1 ELSE 0 END) AS quoted_count,
                          SUM(CASE WHEN executable_entry IS NOT NULL THEN 1 ELSE 0 END) AS executable_count,
                          AVG(CASE WHEN net_return IS NOT NULL THEN CAST(net_return AS REAL) END)
                              AS mean_net_return
                   FROM execution_observations GROUP BY arm_id"""
            ).fetchall()
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._plain(row)
            is_control_abstain = (
                item["arm_id"] == "CONTROL" and item["strategy_id"] == "control-abstain-v1"
            )
            item["display_name"] = (
                "CONTROL · ABSTAIN" if is_control_abstain else str(item["arm_id"])
            )
            item["baseline_kind"] = "abstention" if is_control_abstain else None
            item["performance_inference_allowed"] = not is_control_abstain
            execution = execution_rows.get(str(item["arm_id"]))
            item["execution_observation_count"] = (
                int(execution["observation_count"]) if execution is not None else 0
            )
            item["quoted_count"] = int(execution["quoted_count"] or 0) if execution else 0
            item["executable_count"] = (
                int(execution["executable_count"] or 0) if execution else 0
            )
            item["mean_net_return"] = execution["mean_net_return"] if execution else None
            result.append(item)
        return result

    def arm_comparison(self) -> list[dict[str, Any]]:
        """Expose only recorded arms and preserve abstention-baseline semantics."""
        return self.arms()

    def forward_lab(self, latest: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        outcomes = latest if latest is not None else self.latest_forward_outcomes()
        result: list[dict[str, Any]] = []
        for horizon in DEFAULT_HORIZONS:
            rows = [row for row in outcomes if int(row["horizon_seconds"]) == horizon]
            returns = [float(row["return_pct"]) for row in rows if row["return_pct"] is not None]
            result.append(
                {
                    "horizon_seconds": horizon,
                    "resolved_count": len(returns),
                    "pending_count": sum(row["status"] == "pending" for row in rows),
                    "unavailable_count": sum(row["status"] == "unavailable" for row in rows),
                    "mean_return_pct": mean(returns) if returns else None,
                    "median_return_pct": median(returns) if returns else None,
                    "positive_return_rate": (
                        sum(value > 0 for value in returns) / len(returns) if returns else None
                    ),
                }
            )
        return result

    def calibration(self) -> list[dict[str, Any]]:
        latest_sql = self._latest_outcomes_sql()
        rows = self.conn.execute(
            f"""WITH latest AS ({latest_sql})
                SELECT d.requested_model_id,d.returned_model_id,d.question_version,
                       d.raw_answer_json,latest.horizon_seconds,latest.realized_label
                FROM experiment_decisions d
                JOIN latest ON latest.snapshot_id=d.snapshot_id
                WHERE latest.status='resolved' AND latest.realized_label IS NOT NULL
                  AND d.raw_answer_json IS NOT NULL"""
        ).fetchall()
        groups: dict[tuple[str, str, int], list[BinaryPrediction]] = defaultdict(list)
        for row in rows:
            raw = self._load_json(row["raw_answer_json"])
            probabilities = raw.get("probabilities") if isinstance(raw, dict) else None
            if not isinstance(probabilities, dict):
                continue
            probability = probabilities.get(str(row["horizon_seconds"]))
            if (
                not isinstance(probability, (int, float))
                or isinstance(probability, bool)
                or not 0 <= float(probability) <= 1
            ):
                continue
            model_id = row["returned_model_id"] or row["requested_model_id"]
            if not model_id:
                continue
            key = (str(model_id), str(row["question_version"]), int(row["horizon_seconds"]))
            groups[key].append(
                BinaryPrediction(float(probability), bool(row["realized_label"]))
            )
        return [
            {
                "model_id": key[0],
                "question_version": key[1],
                "horizon_seconds": key[2],
                "sample_count": len(samples),
                "brier": brier_score(samples),
                "log_loss": binary_log_loss(samples),
                "ece": expected_calibration_error(samples),
                "reliability_bins": reliability_bins(samples),
            }
            for key, samples in sorted(groups.items())
        ]

    def health(self, limit: int = 100) -> dict[str, Any]:
        if not self.has_table("measurement_events"):
            return {
                "available": False,
                "capture_success_count": 0,
                "capture_failure_count": 0,
                "outcome_failure_count": 0,
                "arm_failure_count": 0,
                "memestate_failure_count": 0,
                "failure_count": 0,
                "latest_failure": None,
                "recent_events": [],
            }
        rows = self.conn.execute(
            """SELECT * FROM measurement_events
               ORDER BY recorded_at DESC,rowid DESC LIMIT ?""",
            (max(1, min(limit, 500)),),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = self._plain(row)
            item["details"] = self._load_json(item.pop("details_json", None))
            events.append(item)
        failure_row = self.conn.execute(
            """SELECT * FROM measurement_events WHERE kind!='capture_ok'
               ORDER BY recorded_at DESC,rowid DESC LIMIT 1"""
        ).fetchone()
        latest_failure: dict[str, Any] | None = None
        if failure_row is not None:
            latest_failure = self._plain(failure_row)
            latest_failure["details"] = self._load_json(
                latest_failure.pop("details_json", None)
            )
        counts = {
            str(row["kind"]): int(row["n"])
            for row in self.conn.execute(
                "SELECT kind,COUNT(*) AS n FROM measurement_events GROUP BY kind"
            ).fetchall()
        }
        failure_count = sum(
            count for kind, count in counts.items() if kind != "capture_ok"
        )
        return {
            "available": True,
            "capture_success_count": counts.get("capture_ok", 0),
            "capture_failure_count": counts.get("capture_failed", 0),
            "outcome_failure_count": counts.get("outcome_failed", 0),
            "arm_failure_count": counts.get("arm_failed", 0),
            "memestate_failure_count": counts.get("memestate_failed", 0),
            "failure_count": failure_count,
            "latest_failure": latest_failure,
            "recent_events": events,
        }
