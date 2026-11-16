from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import Record


@dataclass(frozen=True)
class Candidate:
    record: Record
    entity_id: str
    status: str
    reason: str


def semantic_signature(record: Record) -> tuple[str, str, int, str]:
    return record.discipline.casefold(), record.published_at, record.quantity, record.reference


def describe_conflict(records: Iterable[Record]) -> str:
    items = list(records)
    if not items:
        return ""
    first = items[0]
    if any(item.kind != first.kind for item in items):
        return "kind_mismatch"
    if any(item.discipline.casefold() != first.discipline.casefold() for item in items):
        return "discipline_mismatch"
    if any(item.quantity != first.quantity for item in items):
        return "quantity_mismatch"
    if any(item.published_at != first.published_at for item in items):
        return "date_mismatch"
    return ""


def choose_preferred(rows: list[dict]) -> dict:
    return sorted(rows, key=lambda row: (-row["trust"], row["source"],
                                         row["record_id"], -row["version"]))[0]


def same_claim(first: dict, second: dict) -> bool:
    return (first["discipline"].casefold() == second["discipline"].casefold()
            and first["quantity"] == second["quantity"]
            and first["published_at"] == second["published_at"])


def authority_match(record: dict, source_key: str, rules: list[dict]) -> dict | None:
    matches = [rule for rule in rules
               if rule["source"] == source_key and rule["kind"] == record["kind"]
               and record["region"] in rule["regions"]
               and rule["valid_from"] <= record["published_at"] <= rule["valid_until"]]
    if not matches:
        return None
    return sorted(matches, key=lambda rule: (rule["key"], rule["revision"]))[0]


def reconcile_group(rows: list[dict]) -> tuple[str, str, dict | None]:
    if not rows:
        return "empty", "", None
    if any(row["status"] != "accepted" for row in rows):
        rows = [row for row in rows if row["status"] == "accepted"]
    if not rows:
        return "quarantined", "all_records_quarantined", None
    records = [Record(**row["record"]) for row in rows]
    conflict = describe_conflict(records)
    if conflict:
        return "conflict", conflict, None
    return "accepted", "", choose_preferred(rows)


def duplicate_ids(records: Iterable[Record]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for record in records:
        if record.source_record_id in seen:
            repeated.add(record.source_record_id)
        seen.add(record.source_record_id)
    return repeated
