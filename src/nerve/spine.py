from __future__ import annotations

import logging
from collections.abc import Callable

from .lab.arms import ControlArm, LabRunner
from .lab.models import MeasurementEvent, MeasurementEventKind, ObservationSnapshot
from .lab.outcomes import pending_outcomes
from .models import Impulse, NodeType, PortfolioContext, Verdict
from .protocol import NerveNode
from .reflexes import Reflex, check_reflexes, default_reflexes
from .store import NerveStore

logger = logging.getLogger(__name__)


class Spine:
    """Conducts one impulse through a route and records every transition."""

    def __init__(
        self,
        nodes: list[NerveNode],
        store: NerveStore,
        context_fn: Callable[[], PortfolioContext],
        reflexes: tuple[Reflex, ...] | None = None,
        lab_runner: LabRunner | None = None,
    ) -> None:
        self.nodes = {node.node_type: node for node in nodes}
        # SENTINEL is free and deterministic, so it runs before the paid model
        # call and long before any signer.
        self.route = [NodeType.SCANNER, NodeType.SENTINEL, NodeType.ANALYST, NodeType.RISK, NodeType.EXECUTOR]
        self.store = store
        self.context_fn = context_fn
        self.reflexes = reflexes or default_reflexes()
        self.lab_runner = lab_runner or LabRunner(store, (ControlArm(),))

    def add_node(self, node: NerveNode, after: NodeType | None = None) -> None:
        self.nodes[node.node_type] = node
        if node.node_type in self.route:
            return
        if after is not None and after in self.route:
            self.route.insert(self.route.index(after) + 1, node.node_type)
        else:
            self.route.append(node.node_type)

    def conduct(self, impulse: Impulse) -> Impulse:
        context = self.context_fn()
        impulse.metadata.setdefault("portfolio", context.model_dump(mode="json"))
        # Create the parent row before transition inserts (foreign key guard).
        self.store.save(impulse)
        for index, node_type in enumerate(self.route):
            if impulse.verdict is Verdict.REJECT:
                break
            # Scanner must first populate metrics; reflexes guard all expensive
            # and side-effecting nodes after that point.
            if index > 0:
                impulse = check_reflexes(impulse, self.context_fn(), self.reflexes, before=node_type)
                self.store.save(impulse)
                self.store.log_transition(impulse)
                if impulse.verdict is Verdict.REJECT:
                    break
            node = self.nodes.get(node_type)
            if node is None:
                continue
            impulse = node.process(impulse)
            # SCANNER assigns the stable pool id; persist before the FK-backed
            # transition row is written.
            self.store.save(impulse)
            self.store.log_transition(impulse)
            if node_type is NodeType.SENTINEL:
                self._capture_measurement(impulse)
        self.store.save(impulse)
        return impulse

    def _capture_measurement(self, impulse: Impulse) -> None:
        """Observe the protocol without changing its verdict or execution path."""
        snapshot: ObservationSnapshot | None = None
        try:
            snapshot = ObservationSnapshot.capture(impulse)
            self.store.record_observation_snapshot(snapshot)
        except Exception as exc:
            self._record_measurement_event(
                MeasurementEventKind.CAPTURE_FAILED,
                "post_sentinel_capture",
                impulse,
                snapshot,
                exc,
            )
            return

        self._record_measurement_event(
            MeasurementEventKind.CAPTURE_OK,
            "post_sentinel_capture",
            impulse,
            snapshot,
            None,
            message="post-SENTINEL snapshot persisted",
        )
        try:
            outcomes = pending_outcomes(snapshot)
            for outcome in outcomes:
                self.store.record_forward_outcome(outcome)
        except Exception as exc:
            self._record_measurement_event(
                MeasurementEventKind.OUTCOME_FAILED,
                "pending_outcomes",
                impulse,
                snapshot,
                exc,
            )
        self.lab_runner.evaluate(snapshot)

    def _record_measurement_event(
        self,
        kind: MeasurementEventKind,
        stage: str,
        impulse: Impulse,
        snapshot: ObservationSnapshot | None,
        error: Exception | None,
        *,
        message: str | None = None,
    ) -> None:
        event = MeasurementEvent(
            kind=kind,
            stage=stage,
            impulse_id=impulse.id,
            snapshot_id=snapshot.snapshot_id if snapshot else None,
            error_type=type(error).__name__ if error else None,
            message=(message or str(error) or kind.value)[:1000],
            details={"verdict": impulse.verdict.value},
        )
        try:
            self.store.record_measurement_event(event)
        except Exception:
            # Core execution remains unchanged, but the final fallback is never silent.
            logger.exception(
                "failed to persist measurement health event kind=%s impulse_id=%s",
                kind.value,
                impulse.id,
            )
