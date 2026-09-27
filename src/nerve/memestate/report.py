from __future__ import annotations

from typing import Any


def collection_result(state: Any) -> dict[str, Any]:
    return {
        "state_id": state.state_id,
        "snapshot_id": state.snapshot_id,
        "status": state.status.value,
        "state_hash": state.state_hash,
        "fact_count": len(state.facts),
    }
