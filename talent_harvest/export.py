from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
import json

from .contracts import HarvestError, canonical
from .ledger import Ledger
from .reconcile import authority_match, reconcile_group, same_claim


def _latest_per_source(rows: list[dict]) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = row["source"], row["record_id"]
        previous = latest.get(key)
        if previous is None or row["id"] > previous["id"]:
            latest[key] = row
    return list(latest.values())


def _project(row: dict) -> dict:
    return {"source": row["source"], "source_record_id": row["record_id"],
            "batch": row["batch"], "page": row["ordinal"], "version": row["version"],
            "captured_at": row["captured_at"], "observed_at": row["observed_at"],
            "status": row["status"], "reasons": row["reasons"], "trust": row["trust"],
            "record": row["record"], "raw_digest": row["raw_digest"]}


class Delivery:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def _entity(identity: str, group: list[dict], winner: dict,
                authority: dict | None, objections: list[dict]) -> dict:
        objection_ids = {row["id"] for row in objections}
        evidence = [_project(item) for item in sorted(group, key=lambda x: (x["source"], x["record_id"]))
                    if item["status"] == "accepted" and item["id"] not in objection_ids]
        return {"identity": identity, "record": winner["record"],
                "selected": _project(winner),
                "evidence": evidence,
                "objections": [_project(item) for item in
                               sorted(objections, key=lambda x: (x["source"], x["record_id"]))],
                "authority": authority}

    def _authority_winner(self, group: list[dict], rules: list[dict]) -> tuple[dict, dict] | None:
        matches = []
        for row in group:
            if row["status"] != "accepted":
                continue
            rule = authority_match(row["record"], row["source"], rules)
            if rule is not None:
                matches.append((row, rule))
        if not matches or len({row["source"] for row, _ in matches}) != 1:
            return None
        row, rule = sorted(matches, key=lambda item: (item[0]["source"], item[0]["record_id"],
                                                      -item[0]["version"], item[0]["id"]))[0]
        basis = {"rule": rule["key"], "revision": rule["revision"], "source": rule["source"],
                 "kind": rule["kind"], "regions": rule["regions"],
                 "valid_from": rule["valid_from"], "valid_until": rule["valid_until"]}
        return row, basis

    def current(self, *, include_quarantine: bool = False) -> dict:
        rows = [row for row in _latest_per_source(self.ledger.observations()) if not row["withdrawn_at"]]
        rules = self.ledger.active_rules()
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            groups[row["identity"]].append(row)
        accepted: list[dict] = []
        conflicts: list[dict] = []
        quarantined: list[dict] = []
        for identity in sorted(groups):
            group = groups[identity]
            rejected = [row for row in group if row["status"] != "accepted"]
            if include_quarantine:
                quarantined.extend(_project(row) for row in rejected)
            status, reason, winner = reconcile_group(group)
            if status == "accepted" and winner:
                authoritative = self._authority_winner(group, rules)
                if authoritative is not None:
                    winner, rule = authoritative
                    accepted.append(self._entity(identity, group, winner, rule, []))
                else:
                    accepted.append(self._entity(identity, group, winner, None, []))
            elif status == "conflict":
                authoritative = self._authority_winner(group, rules)
                if authoritative is not None:
                    winner, rule = authoritative
                    objections = [row for row in group if row["status"] == "accepted"
                                  and row["id"] != winner["id"]
                                  and not same_claim(row["record"], winner["record"])]
                    accepted.append(self._entity(identity, group, winner, rule, objections))
                else:
                    conflicts.append({"identity": identity, "reason": reason,
                                      "evidence": [_project(item) for item in group if item["status"] == "accepted"]})
        return {"schema_version": 1, "rules_basis": {rule["key"]: rule["revision"] for rule in rules},
                "accepted": accepted, "conflicts": conflicts,
                "quarantined": quarantined if include_quarantine else [],
                "counts": {"accepted": len(accepted), "conflicts": len(conflicts),
                           "quarantined": len(quarantined) if include_quarantine else 0}}

    def history(self, *, source: str | None = None, identity: str | None = None) -> dict:
        rows = self.ledger.observations(source=source, identity=identity)
        return {"schema_version": 1, "observations":
                [{**_project(row), "identity": row["identity"], "raw": row["raw"],
                  "withdrawn_at": row["withdrawn_at"]} for row in rows]}

    def trace(self, identity: str) -> dict:
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise HarvestError("identity", "identity must be sha256 hex")
        history = self.history(identity=identity)["observations"]
        if not history:
            raise HarvestError("identity_unknown", identity)
        sources = sorted({row["source"] for row in history})
        events = [event for event in self.ledger.events() if event["source"] in sources]
        current = self.current(include_quarantine=True)
        matching = [item for section in ("accepted", "conflicts") for item in current[section]
                    if item["identity"] == identity]
        return {"identity": identity, "history": history, "events": events,
                "current": matching[0] if matching else None}

    def summary(self) -> dict:
        current = self.current(include_quarantine=True)
        by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"entities": 0, "quantity": 0})
        by_region: dict[str, dict[str, int]] = defaultdict(lambda: {"entities": 0, "quantity": 0})
        for entity in current["accepted"]:
            record = entity["record"]
            by_kind[record["kind"]]["entities"] += 1
            by_kind[record["kind"]]["quantity"] += record["quantity"]
            by_region[record["region"]]["entities"] += 1
            by_region[record["region"]]["quantity"] += record["quantity"]
        return {"counts": current["counts"], "by_kind": dict(sorted(by_kind.items())),
                "by_region": dict(sorted(by_region.items()))}

    def write_snapshot(self, destination: str | Path, *, include_quarantine: bool = False) -> dict:
        path = Path(destination)
        payload = self.current(include_quarantine=include_quarantine)
        data = canonical(payload).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)
        return {"path": str(path), "sha256": sha256(data).hexdigest(),
                "bytes": len(data), "counts": payload["counts"]}


def verify_snapshot(path: str | Path, expected_sha256: str) -> dict:
    payload = Path(path).read_bytes()
    actual = sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise HarvestError("snapshot_digest", f"expected {expected_sha256}, got {actual}")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise HarvestError("snapshot_json", "snapshot is not valid UTF-8 JSON") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise HarvestError("snapshot_schema", "unexpected delivery schema")
    return {"sha256": actual, "counts": data.get("counts", {})}
