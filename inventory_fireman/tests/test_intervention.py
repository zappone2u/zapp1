# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("inventory_fireman")
class TestIntervention(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Test retours", "code": "TIN"})
        cls.service = cls.env["fireman.sync.service"].with_context(fireman_no_push=True)

    def _pull(self, interventions=None, movements=None):
        self.service._apply_interventions(self.uo, interventions)
        self.service._apply_movements(self.uo, movements)
        self.uo.invalidate_recordset()

    def _raw(self, **lines):
        return {
            "i1": {
                "vehicleId": "v1",
                "vehicle": "VSAV 1",
                "rank": "CPL",
                "inventorName": "Dupont",
                "date": "2026-10-20T13:30:00.000Z",
                "note": "Intervention nuit",
                "pending": True,
                "lines": lines
                or {
                    "l1": {"name": "Glucose", "quantity": 3, "restocked": False},
                    "l2": {"name": "Gants", "quantity": 2, "restocked": False},
                },
            }
        }

    def test_pull_creates_intervention_and_lines(self):
        self._pull(self._raw())
        intervention = self.uo.intervention_ids
        self.assertEqual(len(intervention), 1)
        self.assertEqual(
            (intervention.vehicle_name, intervention.rank, intervention.inventor_name, intervention.note),
            ("VSAV 1", "CPL", "Dupont", "Intervention nuit"),
        )
        self.assertEqual(intervention.date, datetime(2026, 10, 20, 13, 30))
        self.assertEqual(sorted(intervention.line_ids.mapped("name")), ["Gants", "Glucose"])
        self.assertEqual(intervention.state, "pending")
        self.assertEqual(self.uo.restock_pending_count, 1)

    def test_pull_is_idempotent(self):
        raw = self._raw()
        self._pull(raw)
        intervention = self.uo.intervention_ids
        write_date, line_dates = intervention.write_date, intervention.line_ids.mapped("write_date")
        self._pull(raw)
        self.assertEqual(self.uo.intervention_ids, intervention)
        self.assertEqual(intervention.write_date, write_date)
        self.assertEqual(intervention.line_ids.mapped("write_date"), line_dates)

    def test_restock_is_followed_line_by_line(self):
        self._pull(self._raw())
        done = {
            "l1": {
                "name": "Glucose",
                "quantity": 3,
                "restocked": True,
                "restockedDate": "2026-10-20T14:00:00.000Z",
                "restockedBy": "Pharma",
                "picks": [
                    {"pharmacyId": "a", "lot": "A", "quantity": 2, "expiry": "2026-10-21"},
                    {"pharmacyId": "b", "lot": "", "quantity": 1},
                ],
            },
            "l2": {"name": "Gants", "quantity": 2, "restocked": False},
        }
        self._pull(self._raw(**done))
        lines = {line.name: line for line in self.uo.intervention_ids.line_ids}
        glucose = lines["Glucose"]
        self.assertTrue(glucose.restocked)
        self.assertEqual(glucose.restocked_by, "Pharma")
        self.assertEqual(glucose.restocked_date, datetime(2026, 10, 20, 14, 0))
        self.assertEqual(glucose.picked_quantity, 3)
        self.assertEqual(glucose.lots, "2 × A, 1 × sans lot")
        self.assertEqual(self.uo.intervention_ids.state, "pending")
        self.assertEqual(self.uo.restock_pending_count, 1)

        done["l2"] = {"name": "Gants", "quantity": 2, "restocked": True}
        self._pull(self._raw(**done))
        self.assertEqual(self.uo.intervention_ids.state, "done")
        self.assertEqual(self.uo.restock_pending_count, 0)

    def test_removed_lines_and_interventions_disappear(self):
        self._pull(self._raw())
        self._pull(self._raw(l1={"name": "Glucose", "quantity": 3}))
        self.assertEqual(self.uo.intervention_ids.line_ids.mapped("name"), ["Glucose"])
        self._pull(None)
        self.assertFalse(self.uo.intervention_ids)

    def test_malformed_entries_are_tolerated(self):
        self._pull(
            {
                "bad": "pas un dict",
                "i2": {"vehicle": "VTU", "date": "n'importe quoi", "lines": {"x": "non", "y": {"quantity": "abc"}}},
            }
        )
        intervention = self.uo.intervention_ids
        self.assertEqual(intervention.vehicle_name, "VTU")
        self.assertFalse(intervention.date)
        self.assertEqual(len(intervention.line_ids), 1)
        self.assertEqual(intervention.line_ids.quantity, 0)

    def test_interventions_are_scoped_to_the_uo(self):
        other = self.env["fireman.uo"].create({"name": "Autre", "code": "TI2"})
        self.service._apply_interventions(other, self._raw())
        self._pull(self._raw())
        self.assertEqual(self.uo.restock_pending_count, 1)
        self.assertEqual(other.restock_pending_count, 1)
        self.assertEqual(
            self.env["fireman.intervention"].search(self.uo.action_open_restocks()["domain"]).uo_id, self.uo
        )

    def test_movements_are_created_once_and_removed_with_firebase(self):
        movements = {
            "m1": {
                "kind": "restock",
                "name": "Glucose",
                "lot": "A",
                "expiry": "2026-10-21",
                "quantity": 2,
                "date": "2026-10-20T14:00:00.000Z",
                "by": "Pharma",
                "ref": "Réarmement VSAV 1",
            },
            "m2": {"kind": "inconnu", "name": "Gants", "quantity": -4},
        }
        self._pull(movements=movements)
        by_uid = {m.uid: m for m in self.uo.pharmacy_movement_ids}
        self.assertEqual(set(by_uid), {"m1", "m2"})
        first = by_uid["m1"]
        self.assertEqual((first.kind, first.name, first.lot, first.quantity), ("restock", "Glucose", "A", 2))
        self.assertEqual(str(first.expiry_date), "2026-10-21")
        self.assertEqual((first.user_name, first.reference), ("Pharma", "Réarmement VSAV 1"))
        self.assertEqual((by_uid["m2"].kind, by_uid["m2"].quantity), ("other", 0))

        self._pull(movements=movements)
        self.assertEqual({m.uid: m.id for m in self.uo.pharmacy_movement_ids}, {k: v.id for k, v in by_uid.items()})
        self._pull(movements={"m1": movements["m1"]})
        self.assertEqual(self.uo.pharmacy_movement_ids.mapped("uid"), ["m1"])

    def test_scope_is_accepted_by_the_pull_queue(self):
        Event = self.env["fireman.sync.event"]
        Event.enqueue_pull(self.uo.code, ["interventions", "inconnu"])
        event = Event.search([("direction", "=", "pull"), ("uo_code", "=", self.uo.code)], limit=1)
        self.assertEqual(event.payload, '["interventions"]')

    def test_ten_glucoses_with_two_expiry_dates_stay_two_lots(self):
        """2 périment demain, 8 après-demain : deux lots distincts, 10 en stock au total."""
        today = fields.Date.context_today(self.uo)
        tomorrow, after = today + timedelta(days=1), today + timedelta(days=2)
        self.service._apply_pharmacy(
            self.uo,
            {
                "a": {"name": "Glucose", "quantity": 2, "lot": "A", "expiry": tomorrow.strftime("%Y-%m-%d")},
                "b": {"name": "Glucose", "quantity": 8, "lot": "B", "expiry": after.strftime("%Y-%m-%d")},
            },
        )
        items = self.uo.pharmacy_item_ids
        self.assertEqual(sum(items.mapped("quantity")), 10)
        groups = self.env["fireman.pharmacy.item"].read_group([("uo_id", "=", self.uo.id)], ["quantity:sum"], ["name"])
        self.assertEqual(groups[0]["quantity"], 10)
        self.assertEqual(sorted(items.mapped("expiry_date")), [tomorrow, after])
