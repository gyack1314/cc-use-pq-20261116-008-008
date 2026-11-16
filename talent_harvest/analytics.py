from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from .contracts import HarvestError
from .export import Delivery
from .ledger import Ledger


class Analytics:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.delivery = Delivery(ledger)

    def distribution(self) -> dict:
        current = self.delivery.current(include_quarantine=True)
        by_kind: dict[str, Counter[str]] = defaultdict(Counter)
        by_region: dict[str, Counter[str]] = defaultdict(Counter)
        by_discipline: dict[str, Counter[str]] = defaultdict(Counter)
        for entity in current["accepted"]:
            row = entity["record"]
            kind, region, discipline = row["kind"], row["region"], row["discipline"]
            by_kind[kind]["entities"] += 1
            by_kind[kind]["quantity"] += row["quantity"]
            by_region[region]["entities"] += 1
            by_region[region]["quantity"] += row["quantity"]
            by_discipline[discipline]["entities"] += 1
            by_discipline[discipline]["quantity"] += row["quantity"]
        return {"counts": current["counts"], "by_kind": self._plain(by_kind),
                "by_region": self._plain(by_region),
                "by_discipline": self._plain(by_discipline)}

    @staticmethod
    def _plain(value: dict[str, Counter]) -> dict:
        return {key: dict(sorted(counter.items())) for key, counter in sorted(value.items())}

    def coverage(self) -> dict:
        current = self.delivery.current(include_quarantine=True)
        rows = current["accepted"]
        by_source: dict[str, Counter[str]] = defaultdict(Counter)
        for entity in rows:
            selected = entity["selected"]
            source = selected["source"]
            by_source[source]["selected_entities"] += 1
            by_source[source]["selected_quantity"] += entity["record"]["quantity"]
            for evidence in entity["evidence"]:
                by_source[evidence["source"]]["supporting_claims"] += 1
        for conflict in current["conflicts"]:
            for evidence in conflict["evidence"]:
                by_source[evidence["source"]]["disputed_claims"] += 1
        for rejected in current["quarantined"]:
            by_source[rejected["source"]]["quarantined_claims"] += 1
        registered = self.ledger.db.execute("SELECT key,withdrawn_at FROM sources ORDER BY key").fetchall()
        return {"sources": [{"source": row["key"], "withdrawn": bool(row["withdrawn_at"]),
                             **dict(by_source[row["key"]])} for row in registered],
                "total_registered": len(registered),
                "contributing": sum(bool(by_source[row["key"]]) for row in registered)}

    def age_profile(self, reference_date: str) -> dict:
        try:
            today = date.fromisoformat(reference_date)
        except ValueError:
            raise HarvestError("reference_date", "use YYYY-MM-DD") from None
        buckets: dict[str, Counter[str]] = defaultdict(Counter)
        for entity in self.delivery.current()["accepted"]:
            published = date.fromisoformat(entity["record"]["published_at"])
            days = (today - published).days
            if days < 0:
                label = "future"
            elif days <= 30:
                label = "0-30"
            elif days <= 90:
                label = "31-90"
            elif days <= 365:
                label = "91-365"
            else:
                label = "366+"
            kind = entity["record"]["kind"]
            buckets[kind][label] += 1
            buckets[kind][label + "_quantity"] += entity["record"]["quantity"]
        return {"reference_date": reference_date, "by_kind": self._plain(buckets)}

    def trend(self) -> dict:
        rows = self.ledger.observations()
        by_month: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            month = row["captured_at"][:7]
            by_month[month]["observations"] += 1
            by_month[month][row["status"]] += 1
            by_month[month][row["record"]["kind"]] += 1
        return {"by_capture_month": self._plain(by_month)}

    def conflict_report(self) -> dict:
        current = self.delivery.current(include_quarantine=True)
        reasons = Counter(item["reason"] for item in current["conflicts"])
        affected_sources: dict[str, Counter[str]] = defaultdict(Counter)
        for item in current["conflicts"]:
            for evidence in item["evidence"]:
                affected_sources[evidence["source"]][item["reason"]] += 1
        overruled = [{"identity": entity["identity"], "authority": entity["authority"],
                      "objections": entity["objections"]}
                     for entity in current["accepted"] if entity["objections"]]
        return {"conflicts": current["conflicts"],
                "by_reason": dict(sorted(reasons.items())),
                "by_source": self._plain(affected_sources),
                "overruled": overruled,
                "overruled_count": len(overruled)}

    def delivery_health(self) -> dict:
        current = self.delivery.current(include_quarantine=True)
        all_batches = self.ledger.db.execute("SELECT state,count(*) AS total FROM batches GROUP BY state").fetchall()
        all_pages = self.ledger.db.execute("SELECT state,count(*) AS total FROM pages GROUP BY state").fetchall()
        observation_total = self.ledger.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        withdrawn = self.ledger.db.execute("SELECT count(*) FROM sources WHERE withdrawn_at IS NOT NULL").fetchone()[0]
        return {"current": current["counts"],
                "batches": {row["state"]: row["total"] for row in all_batches},
                "pages": {row["state"]: row["total"] for row in all_pages},
                "observations": observation_total, "withdrawn_sources": withdrawn,
                "ready": not any(row["state"] == "failed" for row in all_batches)
                and not any(row["state"] == "failed" for row in all_pages)}
