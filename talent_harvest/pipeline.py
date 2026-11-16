from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import HarvestError, Manifest, Record, Rule, Source
from .extractors import extract
from .intake import find_page, read_manifest, read_page
from .ledger import Ledger
from .normalize import normalize_page
from .policy import assess
from .reconcile import duplicate_ids


@dataclass(frozen=True)
class PageResult:
    ordinal: int
    state: str
    records: int
    unchanged: bool
    error: str = ""

    def document(self) -> dict[str, Any]:
        return {"ordinal": self.ordinal, "state": self.state, "records": self.records,
                "unchanged": self.unchanged, "error": self.error}


class Harvester:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def register_source(self, declaration: dict) -> dict:
        return self.ledger.register(Source.parse(declaration))

    def register_rule(self, declaration: dict) -> dict:
        return self.ledger.register_rule(Rule.parse(declaration))

    def ingest(self, manifest_path: str | Path, *, stop_on_error: bool = False,
               only_page: int | None = None) -> dict:
        path = Path(manifest_path)
        manifest = read_manifest(path)
        self.ledger.ensure_batch(manifest)
        source = self.ledger.source(manifest.source)
        pages = [find_page(manifest, only_page)] if only_page is not None else manifest.pages
        outcomes: list[PageResult] = []
        for page in pages:
            existing = self.ledger.page(manifest.source, manifest.batch, page.ordinal)
            if existing["state"] == "complete":
                outcomes.append(PageResult(page.ordinal, "complete", existing["record_count"], True))
                continue
            try:
                result = self._process(path.parent, manifest, source, page.ordinal)
                outcomes.append(PageResult(page.ordinal, "complete", result["records"], result["unchanged"]))
            except HarvestError as exc:
                self.ledger.fail_page(manifest.source, manifest.batch, page.ordinal, exc)
                outcomes.append(PageResult(page.ordinal, "failed", 0, False, str(exc)))
                if stop_on_error:
                    break
        batch = self.ledger.batch(manifest.source, manifest.batch)
        return {"source": manifest.source, "batch": manifest.batch, "state": batch["state"],
                "pages": [result.document() for result in outcomes],
                "completed_pages": sum(p["state"] == "complete" for p in batch["pages"]),
                "total_pages": len(batch["pages"])}

    def _process(self, base: Path, manifest: Manifest, source: Source, ordinal: int) -> dict:
        page = find_page(manifest, ordinal)
        mirror = read_page(base, page)
        rows = extract(mirror.body, source)
        records = normalize_page(source, rows)
        repeated = duplicate_ids(records)
        if repeated:
            raise HarvestError("duplicate_page_id", ",".join(sorted(repeated)))
        items: list[tuple[Record, dict, str, tuple[str, ...]]] = []
        for record, raw in zip(records, rows):
            decision = assess(record, source, manifest.captured_at)
            items.append((record, raw, decision.status, decision.reasons))
        return self.ledger.commit_page(manifest.source, manifest.batch, ordinal, items)

    def resume(self, manifest_path: str | Path) -> dict:
        return self.ingest(manifest_path)

    def page_retry(self, manifest_path: str | Path, ordinal: int) -> dict:
        return self.ingest(manifest_path, only_page=ordinal)

    def withdraw(self, key: str, reason: str) -> dict:
        if not reason.strip():
            raise HarvestError("withdraw_reason", "withdrawal requires a reason")
        return self.ledger.withdraw(key, reason.strip())

    def inspect(self, source: str, batch: str) -> dict:
        return self.ledger.batch(source, batch)
