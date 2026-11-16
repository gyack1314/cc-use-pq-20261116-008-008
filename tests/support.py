from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import json

from talent_harvest.contracts import canonical
from talent_harvest.ledger import Ledger
from talent_harvest.pipeline import Harvester


def source(key="company", fmt="json", kind="position", trust=70, field_map=None):
    return {"key": key, "name": str(key).title() + " Mirror", "format": fmt,
            "kind": kind, "field_map": field_map or {}, "trust": trust}


def rule(key="city-priority", source="city-portal", kind="position", regions=("Shanghai",),
         valid_from="2026-01-01", valid_until="2026-12-31", note=""):
    return {"key": key, "source": source, "kind": kind, "regions": list(regions),
            "valid_from": valid_from, "valid_until": valid_until, "note": note}


def record(record_id="role-1", organization="North University", title="AI Engineer",
           region="Shanghai", discipline="Computer Science", quantity=4,
           published_at="2026-09-01", reference="https://example.test/role/1", description="Training role"):
    return {"record_id": record_id, "organization": organization, "title": title,
            "region": region, "discipline": discipline, "quantity": quantity,
            "published_at": published_at, "reference": reference, "description": description}


def manifest(workspace: Path, key="company", batch="batch-1", pages=None,
             captured_at="2026-09-30T08:00:00+00:00", fmt="json"):
    pages = pages if pages is not None else [[record()]]
    directory = workspace / key / batch
    directory.mkdir(parents=True, exist_ok=True)
    descriptors = []
    for ordinal, rows in enumerate(pages, 1):
        if fmt == "json":
            payload = canonical({"records": rows}).encode("utf-8")
            suffix = "json"
        elif fmt == "csv":
            import io
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else list(record()))
            writer.writeheader()
            writer.writerows(rows)
            payload = stream.getvalue().encode("utf-8")
            suffix = "csv"
        elif fmt == "html":
            headers = list(rows[0]) if rows else list(record())
            cells = "".join("<tr>" + "".join(f"<td>{row.get(key, '')}</td>" for key in headers) + "</tr>" for row in rows)
            payload = ("<table><tr>" + "".join(f"<th>{key}</th>" for key in headers) +
                       "</tr>" + cells + "</table>").encode("utf-8")
            suffix = "html"
        else:
            raise ValueError(fmt)
        name = f"page-{ordinal}.{suffix}"
        (directory / name).write_bytes(payload)
        descriptors.append({"ordinal": ordinal, "path": name, "sha256": sha256(payload).hexdigest()})
    data = {"source": key, "batch": batch, "captured_at": captured_at, "pages": descriptors}
    path = directory / "manifest.json"
    path.write_text(canonical(data), encoding="utf-8")
    return path


class Workspace:
    def __init__(self):
        self.temp = TemporaryDirectory(prefix="harvest-test-")
        self.path = Path(self.temp.name)
        self.ledger = Ledger(self.path / "state.sqlite3")
        self.harvester = Harvester(self.ledger)

    def close(self):
        self.ledger.close()
        self.temp.cleanup()

    def register(self, **kwargs):
        return self.harvester.register_source(source(**kwargs))

    def register_rule(self, **kwargs):
        return self.harvester.register_rule(rule(**kwargs))

    def ingest(self, **kwargs):
        source_key = kwargs.pop("key", "company")
        path = manifest(self.path, key=source_key, **kwargs)
        return path, self.harvester.ingest(path)
