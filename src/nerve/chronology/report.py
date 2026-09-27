from __future__ import annotations

from typing import Any


def collection_result(item: Any) -> dict[str, Any]:
    return {
        "chronology_id": item.chronology_id,
        "snapshot_id": item.snapshot_id,
        "coverage_status": item.coverage_status.value,
        "chronology_hash": item.chronology_hash,
        "signature_count": item.signature_count,
        "transaction_fetch_count": item.transaction_fetch_count,
        "transaction_unavailable_count": item.transaction_unavailable_count,
        "unsupported_version_count": item.unsupported_version_count,
        "decode_failure_count": item.decode_failure_count,
        "decoded_event_count": item.decoded_event_count,
        "reached_creation": item.reached_creation,
        "history_truncated": item.history_truncated,
        "source_cutoff_at": item.source_cutoff_at.isoformat(),
        "ready_at": item.ready_at.isoformat(),
    }
