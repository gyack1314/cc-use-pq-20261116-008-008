from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any
import re

from .contracts import HarvestError, Record, Source, compact, content_digest, identifier, required


FIELDS = {"record_id", "organization", "title", "region", "discipline",
          "quantity", "published_at", "reference", "description"}


def search_key(value: str) -> str:
    return re.sub(r"[^\w]+", "", compact(value).casefold())


def category(value: Any, name: str) -> str:
    text = compact(required(value, name, 80))
    if not search_key(text):
        raise HarvestError("category", f"{name} cannot be punctuation only")
    return text


def quantity(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise HarvestError("quantity", "quantity must be nonnegative integer")
    if isinstance(value, str):
        value = value.strip().replace(",", "")
    if not isinstance(value, int) and not (isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
        raise HarvestError("quantity", "quantity must be nonnegative integer")
    amount = int(value)
    if amount < 0 or amount > 1_000_000:
        raise HarvestError("quantity", "quantity outside supported range")
    return amount


def published(value: Any) -> str:
    if not isinstance(value, str):
        raise HarvestError("published_at", "published date missing")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HarvestError("published_at", "published date must be YYYY-MM-DD") from None


def lookup(raw: dict[str, Any], source: Source, field: str, default: Any = None) -> Any:
    source_name = source.field_map.get(field, field)
    return raw.get(source_name, default)


def normalize(source: Source, raw: dict[str, Any]) -> Record:
    if not isinstance(raw, dict):
        raise HarvestError("record_shape", "record must be object")
    record_id = identifier(lookup(raw, source, "record_id"), "record_id")
    organization = category(lookup(raw, source, "organization"), "organization")
    title = category(lookup(raw, source, "title"), "title")
    region = category(lookup(raw, source, "region"), "region")
    discipline = category(lookup(raw, source, "discipline"), "discipline")
    amount = quantity(lookup(raw, source, "quantity"))
    date = published(lookup(raw, source, "published_at"))
    reference = lookup(raw, source, "reference", "")
    description = lookup(raw, source, "description", "")
    if not isinstance(reference, str) or len(reference) > 500:
        raise HarvestError("reference", "reference must be short string")
    if not isinstance(description, str) or len(description) > 2000:
        raise HarvestError("description", "description must be short string")
    if not reference.strip():
        reference = f"{source.key}:{record_id}"
    identity_material = "|".join((source.kind, search_key(organization), search_key(title), search_key(region)))
    identity = sha256(identity_material.encode("utf-8")).hexdigest()
    return Record(source.key, record_id, source.kind, organization, title, region, discipline, amount,
                  date, compact(reference), compact(description), identity, content_digest(raw))


def normalize_page(source: Source, rows: list[dict[str, Any]]) -> list[Record]:
    records = [normalize(source, row) for row in rows]
    ids = [record.source_record_id for record in records]
    if len(set(ids)) != len(ids):
        raise HarvestError("duplicate_page_id", "source record id repeats in page")
    return records
