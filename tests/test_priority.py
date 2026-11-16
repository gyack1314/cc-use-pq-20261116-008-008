from __future__ import annotations

import json
import unittest

from talent_harvest.analytics import Analytics
from talent_harvest.audit import Auditor
from talent_harvest.cli import main
from talent_harvest.contracts import HarvestError
from talent_harvest.export import Delivery, verify_snapshot
from talent_harvest.ledger import Ledger
from talent_harvest.lineage import Lineage
from tests.support import Workspace, manifest, record, rule, source


class RegistrationTests(unittest.TestCase):
    """登记状态：优先规则的登记、幂等、重叠拒收、修正与来源撤回。"""

    def setUp(self):
        self.work = Workspace()
        self.work.register()
        self.work.register(key="city-portal", trust=60)
        self.work.register(key="campus", trust=80)

    def tearDown(self):
        self.work.close()

    def test_rule_registration_is_idempotent_and_listed(self):
        first = self.work.register_rule()
        self.assertEqual(first, {"rule": "city-priority", "revision": 1,
                                 "status": "active", "unchanged": False})
        again = self.work.register_rule()
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["revision"], 1)
        listed = self.work.ledger.rules()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "active")
        self.assertEqual(listed[0]["regions"], ["Shanghai"])
        self.assertIsNone(listed[0]["superseded_at"])
        self.assertEqual(self.work.ledger.rule_rejections(), [])
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_overlapping_rule_is_rejected_with_retained_reason(self):
        self.work.register_rule(valid_from="2026-01-01", valid_until="2026-12-31")
        attempt = rule(key="campus-priority", source="campus",
                       valid_from="2026-06-01", valid_until="2026-12-31")
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(attempt)
        self.assertEqual(caught.exception.code, "rule_overlap_unresolvable")
        rejections = self.work.ledger.rule_rejections()
        self.assertEqual(len(rejections), 1)
        rejected = rejections[0]
        self.assertEqual(rejected["rule"], "campus-priority")
        self.assertEqual(rejected["code"], "rule_overlap_unresolvable")
        self.assertEqual(rejected["payload"]["source"], "campus")
        self.assertEqual(rejected["conflicts"][0]["rule"], "city-priority")
        self.assertEqual(rejected["conflicts"][0]["regions"], ["Shanghai"])
        self.assertEqual(rejected["conflicts"][0]["valid_from"], "2026-06-01")
        self.assertEqual(rejected["conflicts"][0]["valid_until"], "2026-12-31")
        self.assertEqual([entry["key"] for entry in self.work.ledger.rules()], ["city-priority"])
        events = [event["event_type"] for event in self.work.ledger.events()]
        self.assertIn("rule_rejected", events)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_disjoint_regions_avoid_overlap(self):
        self.work.register_rule(regions=["Shanghai"])
        result = self.work.register_rule(key="campus-priority", source="campus",
                                         regions=["Beijing"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.work.ledger.rule_rejections(), [])
        self.assertEqual(len(self.work.ledger.active_rules()), 2)

    def test_disjoint_periods_avoid_overlap_but_touching_periods_conflict(self):
        self.work.register_rule(valid_from="2026-01-01", valid_until="2026-06-30")
        later = self.work.register_rule(key="campus-priority", source="campus",
                                        valid_from="2026-07-01", valid_until="2026-12-31")
        self.assertEqual(later["status"], "active")
        touching = rule(key="campus-priority", source="campus",
                        valid_from="2026-06-30", valid_until="2026-12-31")
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(touching)
        self.assertEqual(caught.exception.code, "rule_overlap_unresolvable")
        self.assertEqual(len(self.work.ledger.rule_rejections()), 1)

    def test_same_source_overlap_is_adjudicable(self):
        self.work.register_rule(valid_from="2026-01-01", valid_until="2026-06-30")
        result = self.work.register_rule(key="city-priority-ext",
                                         valid_from="2026-03-01", valid_until="2026-09-30")
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.work.ledger.rule_rejections(), [])

    def test_rule_revision_supersedes_previous(self):
        self.work.register_rule(regions=["Shanghai"])
        revised = self.work.register_rule(regions=["Shanghai", "Suzhou"], note="扩展")
        self.assertEqual(revised["revision"], 2)
        listed = self.work.ledger.rules()
        self.assertEqual([entry["status"] for entry in listed], ["superseded", "active"])
        self.assertIsNotNone(listed[0]["superseded_at"])
        self.assertEqual(listed[1]["regions"], ["Shanghai", "Suzhou"])
        events = [event["event_type"] for event in self.work.ledger.events()]
        self.assertIn("rule_revised", events)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_rule_revision_is_rechecked_against_other_rules(self):
        self.work.register_rule(regions=["Shanghai"])
        self.work.register_rule(key="campus-priority", source="campus", regions=["Beijing"])
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(
                rule(key="campus-priority", source="campus", regions=["Shanghai"]))
        self.assertEqual(caught.exception.code, "rule_overlap_unresolvable")
        listed = {entry["key"]: entry for entry in self.work.ledger.rules()}
        self.assertEqual(listed["campus-priority"]["regions"], ["Beijing"])
        self.assertEqual(listed["campus-priority"]["status"], "active")
        self.assertEqual(len(self.work.ledger.rule_rejections()), 1)

    def test_rule_requires_live_source(self):
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(rule(source="ghost"))
        self.assertEqual(caught.exception.code, "source_unknown")
        self.work.harvester.withdraw("campus", "retired")
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(rule(key="campus-priority", source="campus"))
        self.assertEqual(caught.exception.code, "source_withdrawn")
        self.assertEqual(self.work.ledger.rule_rejections(), [])

    def test_rule_kind_must_match_source(self):
        self.work.register(key="programs", kind="program")
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.register_rule(rule(source="programs", kind="position"))
        self.assertEqual(caught.exception.code, "rule_kind_mismatch")

    def test_rule_contract_validation(self):
        invalid = [
            (rule(valid_from="2026-12-31", valid_until="2026-01-01"), "rule_period"),
            (rule(regions=[]), "rule_regions"),
            (rule(regions=["Shanghai", "  "]), "rule_regions"),
            (rule(valid_from="2026-13-01"), "invalid_day"),
            (rule(kind="person"), "rule_kind"),
            (rule(key="Bad Key"), "invalid_identifier"),
        ]
        for declaration, code in invalid:
            with self.subTest(code=code), self.assertRaises(HarvestError) as caught:
                self.work.harvester.register_rule(declaration)
            self.assertEqual(caught.exception.code, code)


class PageEvidenceTests(unittest.TestCase):
    """页证据：采入页按当时有效的规则版本保存依据，修订不改已完成页。"""

    def setUp(self):
        self.work = Workspace()
        self.work.register()
        self.work.register(key="city-portal", trust=60)

    def tearDown(self):
        self.work.close()

    def page_basis(self, batch, ordinal):
        page = self.work.ledger.page("company", batch, ordinal)
        return json.loads(page["rules_basis"])

    def test_page_basis_records_active_rule_versions(self):
        self.work.register_rule()
        self.work.ingest()
        self.assertEqual(self.page_basis("batch-1", 1), {"city-priority": 1})
        evidence = Lineage(self.work.ledger).batch("company", "batch-1")
        self.assertEqual(evidence["pages"][0]["rules_basis"], {"city-priority": 1})
        inspected = self.work.harvester.inspect("company", "batch-1")
        self.assertEqual(json.loads(inspected["pages"][0]["rules_basis"]), {"city-priority": 1})

    def test_page_basis_empty_without_rules(self):
        self.work.ingest()
        self.assertEqual(self.page_basis("batch-1", 1), {})

    def test_completed_page_keeps_basis_after_rule_correction(self):
        self.work.register_rule(regions=["Shanghai"])
        self.work.ingest()
        self.work.register_rule(regions=["Shanghai", "Suzhou"])
        self.assertEqual(self.page_basis("batch-1", 1), {"city-priority": 1})
        self.work.ingest(batch="batch-2", pages=[[record(quantity=6)]])
        self.assertEqual(self.page_basis("batch-2", 1), {"city-priority": 2})
        first = Lineage(self.work.ledger).batch("company", "batch-1")
        second = Lineage(self.work.ledger).batch("company", "batch-2")
        self.assertEqual(first["pages"][0]["rules_basis"], {"city-priority": 1})
        self.assertEqual(second["pages"][0]["rules_basis"], {"city-priority": 2})
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_supplementary_page_uses_basis_at_commit_time(self):
        self.work.register_rule(regions=["Shanghai"])
        path = manifest(self.work.path, pages=[[record()], [record(record_id="role-2",
                                                                   title="Analyst")]])
        first = self.work.harvester.ingest(path, only_page=1)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(self.page_basis("batch-1", 1), {"city-priority": 1})
        self.work.register_rule(regions=["Shanghai", "Suzhou"])
        resumed = self.work.harvester.resume(path)
        self.assertEqual(resumed["state"], "complete")
        self.assertEqual(self.page_basis("batch-1", 1), {"city-priority": 1})
        self.assertEqual(self.page_basis("batch-1", 2), {"city-priority": 2})
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_auditor_checks_rule_registry_integrity(self):
        self.work.register_rule(regions=["Shanghai"])
        self.work.register_rule(regions=["Shanghai", "Suzhou"])
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])
        self.work.ledger.db.execute("UPDATE priority_rules SET superseded_at=NULL")
        self.work.ledger.db.commit()
        report = Auditor(self.work.ledger).verify_state()
        self.assertFalse(report["ok"])
        self.assertIn("rule_active", [finding["code"] for finding in report["findings"]])


class DisputeTests(unittest.TestCase):
    """争议：数量冲突时只有范围内、质量合格、未撤回的权威声明可用于现行供需。"""

    def setUp(self):
        self.work = Workspace()
        self.work.register()
        self.work.register(key="city-portal", trust=60)
        self.work.register(key="campus", trust=80)

    def tearDown(self):
        self.work.close()

    def test_authority_in_scope_resolves_quantity_conflict(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=10)]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["conflicts"], 0)
        self.assertEqual(current["counts"]["accepted"], 1)
        entity = current["accepted"][0]
        self.assertEqual(entity["record"]["quantity"], 10)
        self.assertEqual(entity["selected"]["source"], "city-portal")
        self.assertEqual(entity["authority"]["rule"], "city-priority")
        self.assertEqual(entity["authority"]["revision"], 1)
        self.assertEqual([item["source"] for item in entity["objections"]], ["company"])
        self.assertEqual(entity["objections"][0]["record"]["quantity"], 4)
        self.assertEqual([item["source"] for item in entity["evidence"]], ["city-portal"])

    def test_region_outside_scope_keeps_existing_conflict(self):
        self.work.register_rule(regions=["Shanghai"])
        shanghai = record(quantity=4)
        beijing = record(record_id="role-b", title="Data Engineer", region="Beijing", quantity=3)
        self.work.ingest(pages=[[shanghai, beijing]])
        self.work.ingest(key="city-portal",
                         pages=[[record(record_id="off-1", quantity=10),
                                 record(record_id="off-b", title="Data Engineer",
                                        region="Beijing", quantity=8)]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"], {"accepted": 1, "conflicts": 1, "quarantined": 0})
        self.assertEqual(current["accepted"][0]["record"]["region"], "Shanghai")
        conflict = current["conflicts"][0]
        self.assertEqual(conflict["reason"], "quantity_mismatch")
        self.assertEqual({item["record"]["region"] for item in conflict["evidence"]}, {"Beijing"})

    def test_period_outside_scope_keeps_existing_conflict(self):
        self.work.register_rule(valid_from="2026-01-01", valid_until="2026-06-30")
        in_period = record(quantity=4, published_at="2026-05-01")
        out_period = record(record_id="role-b", title="Data Engineer",
                            quantity=3, published_at="2026-09-01")
        self.work.ingest(pages=[[in_period, out_period]])
        self.work.ingest(key="city-portal",
                         pages=[[record(record_id="off-1", quantity=10, published_at="2026-05-01"),
                                 record(record_id="off-b", title="Data Engineer",
                                        quantity=8, published_at="2026-09-01")]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"], {"accepted": 1, "conflicts": 1, "quarantined": 0})
        self.assertEqual(current["accepted"][0]["record"]["published_at"], "2026-05-01")
        self.assertEqual(current["conflicts"][0]["evidence"][0]["record"]["published_at"],
                         "2026-09-01")

    def test_quarantined_authority_cannot_decide(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="campus", pages=[[record(record_id="cam-1", quantity=9)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=0)]])
        current = Delivery(self.work.ledger).current(include_quarantine=True)
        self.assertEqual(current["counts"]["accepted"], 0)
        self.assertEqual(current["counts"]["conflicts"], 1)
        self.assertEqual(current["counts"]["quarantined"], 1)
        self.assertEqual(current["quarantined"][0]["source"], "city-portal")
        self.assertIn("zero_quantity", current["quarantined"][0]["reasons"])

    def test_withdrawn_authority_cannot_decide(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="campus", pages=[[record(record_id="cam-1", quantity=9)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=10)]])
        resolved = Delivery(self.work.ledger).current()
        self.assertEqual(resolved["counts"]["accepted"], 1)
        self.assertEqual(resolved["accepted"][0]["selected"]["source"], "city-portal")
        self.work.harvester.withdraw("city-portal", "official feed retired")
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 0)
        self.assertEqual(current["counts"]["conflicts"], 1)
        self.assertEqual({item["source"] for item in current["conflicts"][0]["evidence"]},
                         {"company", "campus"})
        history = Delivery(self.work.ledger).history()["observations"]
        self.assertEqual(len(history), 3)
        self.assertEqual(self.work.ledger.rules()[0]["status"], "active")

    def test_objections_remain_traceable(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=10)]])
        delivery = Delivery(self.work.ledger)
        entity = delivery.current()["accepted"][0]
        traced = delivery.trace(entity["identity"])
        self.assertEqual(len(traced["current"]["objections"]), 1)
        self.assertEqual(traced["current"]["objections"][0]["source"], "company")
        self.assertEqual(len(traced["history"]), 2)
        report = Analytics(self.work.ledger).conflict_report()
        self.assertEqual(report["by_reason"], {})
        self.assertEqual(report["overruled_count"], 1)
        self.assertEqual(report["overruled"][0]["authority"]["rule"], "city-priority")
        self.assertEqual(report["overruled"][0]["objections"][0]["record"]["quantity"], 4)

    def test_rejected_rule_grants_no_authority(self):
        self.work.register_rule()
        with self.assertRaises(HarvestError):
            self.work.harvester.register_rule(rule(key="campus-priority", source="campus"))
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="campus", pages=[[record(record_id="cam-1", quantity=9)]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 0)
        self.assertEqual(current["counts"]["conflicts"], 1)

    def test_authority_outranks_trust_when_claims_agree(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="campus", pages=[[record(record_id="cam-1", quantity=4)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=4)]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 1)
        entity = current["accepted"][0]
        self.assertEqual(entity["selected"]["source"], "city-portal")
        self.assertEqual(entity["authority"]["rule"], "city-priority")
        self.assertEqual(entity["objections"], [])
        self.assertEqual(len(entity["evidence"]), 3)


class DeliveryTests(unittest.TestCase):
    """交付：快照保持原解释，新判断带规则版本依据。"""

    def setUp(self):
        self.work = Workspace()
        self.work.register()
        self.work.register(key="city-portal", trust=60)

    def tearDown(self):
        self.work.close()

    def test_snapshot_keeps_original_interpretation(self):
        self.work.register(key="campus", trust=80)
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="campus", pages=[[record(record_id="cam-1", quantity=9)]])
        delivery = Delivery(self.work.ledger)
        first_path = self.work.path / "snapshot-1.json"
        first = delivery.write_snapshot(first_path)
        self.assertEqual(first["counts"], {"accepted": 0, "conflicts": 1, "quarantined": 0})
        original_bytes = first_path.read_bytes()

        self.work.register_rule()
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=10)]])
        second_path = self.work.path / "snapshot-2.json"
        second = delivery.write_snapshot(second_path)
        self.assertEqual(second["counts"], {"accepted": 1, "conflicts": 0, "quarantined": 0})
        self.assertNotEqual(first["sha256"], second["sha256"])

        self.assertEqual(first_path.read_bytes(), original_bytes)
        verified = verify_snapshot(first_path, first["sha256"])
        self.assertEqual(verified["counts"], {"accepted": 0, "conflicts": 1, "quarantined": 0})
        payload = json.loads(second_path.read_bytes())
        self.assertEqual(payload["rules_basis"], {"city-priority": 1})
        self.assertEqual(payload["accepted"][0]["authority"]["rule"], "city-priority")

    def test_delivery_carries_version_basis(self):
        self.work.register_rule()
        self.work.ingest(pages=[[record(quantity=4)]])
        self.work.ingest(key="city-portal", pages=[[record(record_id="off-1", quantity=10)]])
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["rules_basis"], {"city-priority": 1})
        self.assertEqual(current["accepted"][0]["authority"]["revision"], 1)
        summary = Delivery(self.work.ledger).summary()
        self.assertEqual(summary["by_region"]["Shanghai"]["quantity"], 10)

    def test_rule_correction_produces_new_versioned_judgment(self):
        self.work.register_rule(regions=["Shanghai"])
        shanghai = record(quantity=4)
        suzhou = record(record_id="role-s", title="Platform Engineer", region="Suzhou", quantity=3)
        self.work.ingest(pages=[[shanghai, suzhou]])
        self.work.ingest(key="city-portal",
                         pages=[[record(record_id="off-1", quantity=10),
                                 record(record_id="off-s", title="Platform Engineer",
                                        region="Suzhou", quantity=7)]])
        delivery = Delivery(self.work.ledger)
        before = delivery.current()
        self.assertEqual(before["counts"], {"accepted": 1, "conflicts": 1, "quarantined": 0})
        snapshot_path = self.work.path / "before.json"
        snapshot = delivery.write_snapshot(snapshot_path)

        self.work.register_rule(regions=["Shanghai", "Suzhou"])
        after = delivery.current()
        self.assertEqual(after["counts"], {"accepted": 2, "conflicts": 0, "quarantined": 0})
        self.assertEqual(after["rules_basis"], {"city-priority": 2})
        revisions = {entity["record"]["region"]: entity["authority"]["revision"]
                     for entity in after["accepted"]}
        self.assertEqual(revisions, {"Shanghai": 2, "Suzhou": 2})
        self.assertEqual(verify_snapshot(snapshot_path, snapshot["sha256"])["counts"],
                         {"accepted": 1, "conflicts": 1, "quarantined": 0})
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_cli_register_rule_and_registry(self):
        declaration = self.work.path / "rule.json"
        declaration.write_text(json.dumps(rule()), encoding="utf-8")
        database = str(self.work.path / "cli.sqlite3")
        source_declaration = self.work.path / "source.json"
        source_declaration.write_text(json.dumps(source(key="city-portal")), encoding="utf-8")
        self.assertEqual(main(["--db", database, "register", str(source_declaration)]), 0)
        self.assertEqual(main(["--db", database, "register-rule", str(declaration)]), 0)
        self.assertEqual(main(["--db", database, "rules"]), 0)

        campus_declaration = self.work.path / "campus.json"
        campus_declaration.write_text(json.dumps(source(key="campus")), encoding="utf-8")
        conflicting = self.work.path / "conflicting.json"
        conflicting.write_text(json.dumps(rule(key="campus-priority", source="campus")),
                               encoding="utf-8")
        self.assertEqual(main(["--db", database, "register", str(campus_declaration)]), 0)
        self.assertEqual(main(["--db", database, "register-rule", str(conflicting)]), 2)
        with Ledger(database) as ledger:
            self.assertEqual(len(ledger.rules()), 1)
            rejections = ledger.rule_rejections()
            self.assertEqual(len(rejections), 1)
            self.assertEqual(rejections[0]["rule"], "campus-priority")
            self.assertEqual(rejections[0]["code"], "rule_overlap_unresolvable")


if __name__ == "__main__":
    unittest.main()
