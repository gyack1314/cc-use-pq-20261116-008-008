from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
import json

from .contracts import HarvestError, Manifest, Record, Source, canonical, content_digest
from .intake import manifest_digest
from .ledger import Ledger


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    location: str
    message: str

    def document(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code,
                "location": self.location, "message": self.message}


class Auditor:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def verify_state(self) -> dict:
        findings: list[Finding] = []
        sources = self.ledger.db.execute("SELECT * FROM sources ORDER BY key").fetchall()
        for source in sources:
            findings.extend(self._source(source))
        batches = self.ledger.db.execute("SELECT * FROM batches ORDER BY source,batch").fetchall()
        for batch in batches:
            findings.extend(self._batch(batch))
        pages = self.ledger.db.execute("SELECT * FROM pages ORDER BY source,batch,ordinal").fetchall()
        for page in pages:
            findings.extend(self._page(page))
        observations = self.ledger.db.execute("SELECT * FROM observations ORDER BY id").fetchall()
        for observation in observations:
            findings.extend(self._observation(observation))
        records = self.ledger.db.execute("SELECT * FROM source_records ORDER BY source,record_id").fetchall()
        for record in records:
            findings.extend(self._record_cursor(record))
        findings.extend(self._rules())
        foreign_keys = self.ledger.db.execute("PRAGMA foreign_key_check").fetchall()
        for row in foreign_keys:
            findings.append(Finding("error", "foreign_key", str(tuple(row)), "dangling reference"))
        return {"ok": not any(f.severity == "error" for f in findings),
                "counts": {"sources": len(sources), "batches": len(batches), "pages": len(pages),
                           "observations": len(observations), "record_cursors": len(records)},
                "findings": [finding.document() for finding in findings]}

    def _rules(self) -> list[Finding]:
        findings: list[Finding] = []
        rows = self.ledger.db.execute("SELECT * FROM priority_rules ORDER BY key,revision").fetchall()
        by_key: dict[str, list] = defaultdict(list)
        for row in rows:
            by_key[row["key"]].append(row)
        for key, revisions in by_key.items():
            location = f"rule:{key}"
            numbers = [row["revision"] for row in revisions]
            if numbers != list(range(1, len(numbers) + 1)):
                findings.append(Finding("error", "rule_revision_gap", location,
                                        "rule revisions are not contiguous"))
            active = [row for row in revisions if row["superseded_at"] is None]
            if len(active) != 1 or active[0]["revision"] != max(numbers):
                findings.append(Finding("error", "rule_active", location,
                                        "exactly the latest revision must be active"))
        actives = [row for row in rows if row["superseded_at"] is None]
        for left, right in ((a, b) for i, a in enumerate(actives) for b in actives[i + 1:]):
            if left["source"] == right["source"] or left["kind"] != right["kind"]:
                continue
            shared = set(json.loads(left["regions"])) & set(json.loads(right["regions"]))
            if shared and max(left["valid_from"], right["valid_from"]) <= min(left["valid_until"], right["valid_until"]):
                findings.append(Finding("error", "rule_overlap",
                                        f"rule:{left['key']}|{right['key']}",
                                        f"active rules overlap on {sorted(shared)}"))
        return findings

    def _source(self, row: Any) -> list[Finding]:
        location = f"source:{row['key']}"
        try:
            Source.parse({"key": row["key"], "name": row["name"], "format": row["format"],
                          "kind": row["kind"], "field_map": json.loads(row["field_map"]),
                          "trust": row["trust"]})
        except (HarvestError, ValueError) as exc:
            return [Finding("error", "source_contract", location, str(exc))]
        if row["revision"] < 1:
            return [Finding("error", "source_revision", location, "revision must be positive")]
        return []

    def _batch(self, row: Any) -> list[Finding]:
        location = f"batch:{row['source']}/{row['batch']}"
        result: list[Finding] = []
        try:
            manifest = Manifest.parse(json.loads(row["manifest_json"]))
            if manifest_digest(manifest) != row["manifest_hash"]:
                result.append(Finding("error", "manifest_digest", location, "stored manifest hash mismatch"))
            if manifest.source != row["source"] or manifest.batch != row["batch"]:
                result.append(Finding("error", "manifest_identity", location, "manifest identifier mismatch"))
            if manifest.captured_at != row["captured_at"]:
                result.append(Finding("error", "capture_time", location, "capture time differs from manifest"))
        except (HarvestError, ValueError, TypeError) as exc:
            result.append(Finding("error", "manifest_invalid", location, str(exc)))
            return result
        pages = self.ledger.db.execute("SELECT ordinal,state,path,digest FROM pages WHERE source=? AND batch=? ORDER BY ordinal",
                                       (row["source"], row["batch"])).fetchall()
        if len(pages) != len(manifest.pages):
            result.append(Finding("error", "page_count", location, "page count differs from manifest"))
        for page, declared in zip(pages, manifest.pages):
            if page["ordinal"] != declared.ordinal or page["path"] != declared.path or page["digest"] != declared.sha256:
                result.append(Finding("error", "page_declaration", location, f"page {declared.ordinal} differs"))
        if row["state"] == "complete" and (not pages or any(p["state"] != "complete" for p in pages)):
            result.append(Finding("error", "batch_state", location, "complete batch has incomplete page"))
        if pages and all(p["state"] == "complete" for p in pages) and row["state"] != "complete":
            result.append(Finding("error", "batch_state", location, "all pages complete but batch is not"))
        if row["state"] == "complete" and not row["completed_at"]:
            result.append(Finding("error", "batch_time", location, "complete batch has no completion timestamp"))
        return result

    def _page(self, row: Any) -> list[Finding]:
        location = f"page:{row['source']}/{row['batch']}/{row['ordinal']}"
        result: list[Finding] = []
        actual = self.ledger.db.execute("SELECT count(*) FROM observations WHERE source=? AND batch=? AND ordinal=?",
                                        (row["source"], row["batch"], row["ordinal"])).fetchone()[0]
        if row["state"] == "complete":
            if row["record_count"] != actual:
                result.append(Finding("error", "page_count", location, f"stored {row['record_count']}, actual {actual}"))
            if row["processed_at"] is None:
                result.append(Finding("error", "page_time", location, "complete page has no timestamp"))
            if row["error_code"]:
                result.append(Finding("error", "page_error", location, "complete page still has error"))
            try:
                if not isinstance(json.loads(row["rules_basis"] or "{}"), dict):
                    raise ValueError("basis is not an object")
            except ValueError:
                result.append(Finding("error", "page_basis", location, "complete page lacks a valid rules basis"))
        elif actual:
            result.append(Finding("error", "partial_page", location, "incomplete page has observations"))
        if row["attempts"] < 0:
            result.append(Finding("error", "attempt_count", location, "negative attempts"))
        if row["state"] == "failed" and not row["error_code"]:
            result.append(Finding("error", "failure_code", location, "failed page has no code"))
        return result

    def _observation(self, row: Any) -> list[Finding]:
        location = f"observation:{row['id']}"
        result: list[Finding] = []
        try:
            raw = json.loads(row["raw_json"])
            record = Record(**json.loads(row["record_json"]))
        except (ValueError, TypeError) as exc:
            return [Finding("error", "observation_json", location, str(exc))]
        if content_digest(raw) != row["raw_digest"] or record.raw_digest != row["raw_digest"]:
            result.append(Finding("error", "raw_digest", location, "raw record digest mismatch"))
        if record.identity != row["identity"] or record.source != row["source"]:
            result.append(Finding("error", "record_identity", location, "normalized record identity mismatch"))
        if record.source_record_id != row["record_id"]:
            result.append(Finding("error", "source_record_id", location, "record identifier mismatch"))
        if row["version"] < 1:
            result.append(Finding("error", "record_version", location, "version must be positive"))
        if row["status"] not in {"accepted", "quarantined"}:
            result.append(Finding("error", "quality_status", location, "unexpected quality status"))
        return result

    def _record_cursor(self, row: Any) -> list[Finding]:
        location = f"cursor:{row['source']}/{row['record_id']}"
        latest = self.ledger.db.execute("SELECT id,version FROM observations WHERE source=? AND record_id=? ORDER BY id DESC LIMIT 1",
                                        (row["source"], row["record_id"])).fetchone()
        if latest is None:
            return [Finding("error", "orphan_cursor", location, "no corresponding observation")]
        result = []
        if latest["id"] != row["latest_observation"] or latest["version"] != row["version"]:
            result.append(Finding("error", "cursor_stale", location, "cursor does not point to latest observation"))
        versions = [v[0] for v in self.ledger.db.execute("SELECT version FROM observations WHERE source=? AND record_id=? ORDER BY id",
                                                       (row["source"], row["record_id"]))]
        if versions != list(range(1, len(versions) + 1)):
            result.append(Finding("error", "version_gap", location, "versions are not contiguous"))
        return result

    def operational(self) -> dict:
        batches = self.ledger.db.execute("SELECT source,batch,state,started_at,completed_at FROM batches ORDER BY source,batch")
        result = []
        for batch in batches:
            pages = self.ledger.db.execute("SELECT ordinal,state,attempts,error_code,record_count FROM pages WHERE source=? AND batch=? ORDER BY ordinal",
                                           (batch["source"], batch["batch"])).fetchall()
            result.append({**dict(batch), "pages": [dict(page) for page in pages],
                           "complete": sum(page["state"] == "complete" for page in pages),
                           "failed": sum(page["state"] == "failed" for page in pages),
                           "records": sum(page["record_count"] for page in pages)})
        return {"batches": result, "failed_batches": sum(item["state"] == "failed" for item in result),
                "pending_batches": sum(item["state"] == "pending" for item in result)}
