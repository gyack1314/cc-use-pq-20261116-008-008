from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from typing import Any
import json

from .contracts import HarvestError, canonical
from .ledger import Ledger
from .export import Delivery


class Lineage:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def record(self, source: str, record_id: str) -> dict:
        rows = [row for row in self.ledger.observations(source=source)
                if row["record_id"] == record_id]
        if not rows:
            raise HarvestError("record_unknown", f"{source}/{record_id}")
        entries = []
        previous = None
        for row in rows:
            fields = self._field_changes(previous["record"] if previous else None, row["record"])
            entries.append({"observation_id": row["id"], "batch": row["batch"],
                            "page": row["ordinal"], "version": row["version"],
                            "captured_at": row["captured_at"], "observed_at": row["observed_at"],
                            "identity": row["identity"], "status": row["status"],
                            "reasons": row["reasons"], "raw_digest": row["raw_digest"],
                            "changed_fields": fields, "record": row["record"]})
            previous = row
        source_row = self.ledger.db.execute("SELECT withdrawn_at FROM sources WHERE key=?", (source,)).fetchone()
        current = Delivery(self.ledger).current(include_quarantine=True)
        identities = sorted({row["identity"] for row in rows})
        visible = [entity for entity in current["accepted"]
                   if entity["identity"] in identities and entity["selected"]["source"] == source
                   and entity["selected"]["source_record_id"] == record_id]
        return {"source": source, "record_id": record_id, "versions": entries,
                "withdrawn_at": source_row[0], "selected_current": visible,
                "latest_status": rows[-1]["status"]}

    @staticmethod
    def _field_changes(previous: dict | None, current: dict) -> list[str]:
        if previous is None:
            return sorted(current)
        return sorted(key for key in current if previous.get(key) != current.get(key))

    def batch(self, source: str, batch: str) -> dict:
        state = self.ledger.batch(source, batch)
        observations = [row for row in self.ledger.observations(source=source)
                        if row["batch"] == batch]
        by_page: dict[int, list[dict]] = defaultdict(list)
        for row in observations:
            by_page[row["ordinal"]].append(row)
        pages = []
        for page in state["pages"]:
            records = by_page[page["ordinal"]]
            pages.append({"ordinal": page["ordinal"], "path": page["path"],
                          "sha256": page["digest"], "state": page["state"],
                          "attempts": page["attempts"],
                          "rules_basis": json.loads(page["rules_basis"] or "{}"),
                          "observations": [{"id": row["id"], "record_id": row["record_id"],
                                            "identity": row["identity"], "version": row["version"],
                                            "status": row["status"], "raw_digest": row["raw_digest"]}
                                           for row in records]})
        events = [event for event in self.ledger.events(source=source) if event["batch"] == batch]
        return {"source": source, "batch": batch, "manifest_hash": state["manifest_hash"],
                "captured_at": state["captured_at"], "state": state["state"],
                "pages": pages, "events": events}

    def evidence_digest(self, source: str, batch: str) -> dict:
        evidence = self.batch(source, batch)
        data = canonical(evidence).encode("utf-8")
        return {"source": source, "batch": batch, "sha256": sha256(data).hexdigest(),
                "pages": len(evidence["pages"]),
                "observations": sum(len(page["observations"]) for page in evidence["pages"])}

    def source_timeline(self, source: str) -> dict:
        row = self.ledger.db.execute("SELECT key,name,revision,withdrawn_at FROM sources WHERE key=?",
                                     (source,)).fetchone()
        if row is None:
            raise HarvestError("source_unknown", source)
        batches = self.ledger.db.execute("SELECT batch,captured_at,state,started_at,completed_at FROM batches WHERE source=? ORDER BY captured_at,batch",
                                         (source,)).fetchall()
        events = self.ledger.events(source=source)
        observations = self.ledger.observations(source=source)
        by_batch: dict[str, list[dict]] = defaultdict(list)
        for item in observations:
            by_batch[item["batch"]].append(item)
        timeline = []
        for batch in batches:
            items = by_batch[batch["batch"]]
            timeline.append({**dict(batch), "observations": len(items),
                             "accepted": sum(item["status"] == "accepted" for item in items),
                             "quarantined": sum(item["status"] == "quarantined" for item in items)})
        return {"source": dict(row), "batches": timeline, "events": events}
