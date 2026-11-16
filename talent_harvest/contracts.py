from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import re
import unicodedata


class HarvestError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


IDENT = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
HEX = re.compile(r"^[a-f0-9]{64}$")
FORMATS = {"json", "csv", "html"}
KINDS = {"position", "program", "partnership"}


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise HarvestError("invalid_identifier", f"{field} must be lower-case identifier")
    return value


def required(value: Any, field: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise HarvestError("invalid_text", f"{field} must be 1-{maximum} characters")
    return value.strip()


def instant(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HarvestError("invalid_time", f"{field} needs timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HarvestError("invalid_time", f"{field} needs ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarvestError("invalid_time", f"{field} needs timezone")
    return parsed.isoformat()


def compact(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def day(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HarvestError("invalid_day", f"{field} must be YYYY-MM-DD")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HarvestError("invalid_day", f"{field} must be YYYY-MM-DD") from None


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    format: str
    kind: str
    field_map: dict[str, str]
    trust: int

    @classmethod
    def parse(cls, value: Any) -> "Source":
        if not isinstance(value, dict):
            raise HarvestError("source_shape", "source must be object")
        fmt, kind = value.get("format"), value.get("kind")
        if fmt not in FORMATS or kind not in KINDS:
            raise HarvestError("source_type", "unsupported format or business kind")
        raw_map = value.get("field_map", {})
        if not isinstance(raw_map, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in raw_map.items()):
            raise HarvestError("field_map", "field_map must map names to source fields")
        allowed = {"record_id", "organization", "title", "region", "discipline", "quantity",
                   "published_at", "reference", "description"}
        if not set(raw_map).issubset(allowed) or len(set(raw_map.values())) != len(raw_map):
            raise HarvestError("field_map", "field_map contains unknown or repeated fields")
        trust = value.get("trust", 50)
        if type(trust) is not int or not 0 <= trust <= 100:
            raise HarvestError("trust", "trust must be integer 0-100")
        return cls(identifier(value.get("key"), "source key"), required(value.get("name"), "name"),
                   fmt, kind, dict(sorted(raw_map.items())), trust)

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Page:
    ordinal: int
    path: str
    sha256: str

    @classmethod
    def parse(cls, value: Any) -> "Page":
        if not isinstance(value, dict):
            raise HarvestError("page_shape", "page must be object")
        ordinal, path, checksum = value.get("ordinal"), value.get("path"), value.get("sha256")
        if type(ordinal) is not int or ordinal < 1 or ordinal > 10000:
            raise HarvestError("page_ordinal", "ordinal must be positive integer")
        if not isinstance(path, str) or not path.strip() or Path(path).is_absolute() or ".." in Path(path).parts or "\\" in path or ":" in path:
            raise HarvestError("page_path", "page path must stay under manifest directory")
        if not isinstance(checksum, str) or not HEX.fullmatch(checksum):
            raise HarvestError("page_checksum", "sha256 must be 64 lowercase hex digits")
        return cls(ordinal, path, checksum)

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Manifest:
    source: str
    batch: str
    captured_at: str
    pages: tuple[Page, ...]

    @classmethod
    def parse(cls, value: Any) -> "Manifest":
        if not isinstance(value, dict):
            raise HarvestError("manifest_shape", "manifest must be object")
        raw_pages = value.get("pages")
        if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= 1000:
            raise HarvestError("pages", "manifest requires 1-1000 pages")
        pages = tuple(Page.parse(item) for item in raw_pages)
        ordinals = [page.ordinal for page in pages]
        if ordinals != list(range(1, len(pages) + 1)):
            raise HarvestError("page_sequence", "pages must be ordered 1 through N")
        paths = [page.path for page in pages]
        if len(set(paths)) != len(paths):
            raise HarvestError("page_path", "same file cannot represent multiple pages")
        return cls(identifier(value.get("source"), "source"), identifier(value.get("batch"), "batch"),
                   instant(value.get("captured_at"), "captured_at"), pages)

    def document(self) -> dict[str, Any]:
        return {"source": self.source, "batch": self.batch, "captured_at": self.captured_at,
                "pages": [page.document() for page in self.pages]}


@dataclass(frozen=True)
class Record:
    source: str
    source_record_id: str
    kind: str
    organization: str
    title: str
    region: str
    discipline: str
    quantity: int
    published_at: str
    reference: str
    description: str
    identity: str
    raw_digest: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Rule:
    key: str
    source: str
    kind: str
    regions: tuple[str, ...]
    valid_from: str
    valid_until: str
    note: str

    @classmethod
    def parse(cls, value: Any) -> "Rule":
        if not isinstance(value, dict):
            raise HarvestError("rule_shape", "rule must be object")
        kind = value.get("kind")
        if kind not in KINDS:
            raise HarvestError("rule_kind", "rule kind must be a supported business kind")
        raw_regions = value.get("regions")
        if not isinstance(raw_regions, list) or not 1 <= len(raw_regions) <= 64:
            raise HarvestError("rule_regions", "regions must list 1-64 region names")
        regions = []
        for item in raw_regions:
            if not isinstance(item, str) or not compact(item):
                raise HarvestError("rule_regions", "region names must be nonempty text")
            regions.append(compact(required(item, "region", 80)))
        valid_from = day(value.get("valid_from"), "valid_from")
        valid_until = day(value.get("valid_until"), "valid_until")
        if valid_from > valid_until:
            raise HarvestError("rule_period", "valid_from must not be after valid_until")
        note = value.get("note", "")
        if not isinstance(note, str) or len(note) > 240:
            raise HarvestError("rule_note", "note must be a short string")
        return cls(identifier(value.get("key"), "rule key"), identifier(value.get("source"), "rule source"),
                   kind, tuple(sorted(set(regions))), valid_from, valid_until, note.strip())

    def document(self) -> dict[str, Any]:
        return {"key": self.key, "source": self.source, "kind": self.kind,
                "regions": list(self.regions), "valid_from": self.valid_from,
                "valid_until": self.valid_until, "note": self.note}
