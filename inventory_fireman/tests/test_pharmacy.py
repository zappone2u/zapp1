# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.utils import parse_day


@tagged("inventory_fireman")
class TestPharmacy(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Test pharmacie", "code": "TPH"})
        cls.Item = cls.env["fireman.pharmacy.item"]
        cls.service = cls.env["fireman.sync.service"].with_context(fireman_no_push=True)
        cls.today = fields.Date.context_today(cls.uo)

    def _iso(self, days):
        return (self.today + timedelta(days=days)).strftime("%Y-%m-%d")

    def _pull(self, raw):
        self.service._apply_pharmacy(self.uo, raw)
        self.uo.invalidate_recordset()

    def _items(self):
        return {i.uid: i for i in self.uo.pharmacy_item_ids}

    def test_parse_day(self):
        self.assertEqual(parse_day("2026-08-31"), date(2026, 8, 31))
        self.assertEqual(parse_day("31/08/2026"), date(2026, 8, 31))
        self.assertEqual(parse_day(" 2026-08-31 "), date(2026, 8, 31))
        for invalid in (None, "", "bientôt", "2026-13-01", "31/02/2026", 20260831):
            self.assertFalse(parse_day(invalid), invalid)

    def test_pull_creates_items_with_all_fields(self):
        self._pull(
            {
                "pha_1": {
                    "name": "Compresses",
                    "quantity": 12,
                    "lot": "L42",
                    "expiry": "2027-05-01",
                    "location": "Tiroir 3",
                },
                "pha_2": {"name": "Ancien article", "quantity": 4},
            }
        )
        items = self._items()
        self.assertEqual(set(items), {"pha_1", "pha_2"})
        full = items["pha_1"]
        self.assertEqual(
            (full.name, full.quantity, full.lot, full.expiry_date, full.location),
            ("Compresses", 12, "L42", date(2027, 5, 1), "Tiroir 3"),
        )
        old = items["pha_2"]
        self.assertFalse(old.expiry_date)
        self.assertEqual(old.expiry_state, "none")

    def test_pull_is_idempotent(self):
        raw = {"pha_1": {"name": "Gants", "quantity": 2, "expiry": self._iso(10)}}
        self._pull(raw)
        item_id = self._items()["pha_1"].id
        write_date = self._items()["pha_1"].write_date
        self._pull(raw)
        self.assertEqual(self._items()["pha_1"].id, item_id)
        self.assertEqual(self._items()["pha_1"].write_date, write_date)
        self.assertEqual(len(self.uo.pharmacy_item_ids), 1)

    def test_pull_updates_and_removes(self):
        self._pull({"pha_1": {"name": "Gants", "quantity": 2}, "pha_2": {"name": "Sérum", "quantity": 1}})
        self._pull({"pha_1": {"name": "Gants", "quantity": 7, "expiry": "31/12/2030"}})
        items = self._items()
        self.assertEqual(set(items), {"pha_1"})
        self.assertEqual(items["pha_1"].quantity, 7)
        self.assertEqual(items["pha_1"].expiry_date, date(2030, 12, 31))

    def test_removed_date_is_cleared(self):
        self._pull({"pha_1": {"name": "Gants", "quantity": 2, "expiry": self._iso(5)}})
        self._pull({"pha_1": {"name": "Gants", "quantity": 2}})
        self.assertFalse(self._items()["pha_1"].expiry_date)

    def test_empty_or_missing_node_clears_the_stock(self):
        self._pull({"pha_1": {"name": "Gants", "quantity": 2}})
        self._pull(None)
        self.assertFalse(self.uo.pharmacy_item_ids)

    def test_malformed_entries_are_tolerated(self):
        self._pull(
            {
                "pha_1": "pas un dict",
                "pha_2": {"name": "", "quantity": "abc", "expiry": "n'importe quoi"},
                "pha_3": {"name": "Négatif", "quantity": -5},
            }
        )
        items = self._items()
        self.assertEqual(set(items), {"pha_2", "pha_3"})
        self.assertEqual(items["pha_2"].quantity, 0)
        self.assertTrue(items["pha_2"].name)
        self.assertFalse(items["pha_2"].expiry_date)
        self.assertEqual(items["pha_3"].quantity, 0)

    def test_expiry_state_boundaries(self):
        self._pull(
            {
                "yesterday": {"name": "a", "expiry": self._iso(-1)},
                "today": {"name": "b", "expiry": self._iso(0)},
                "limit": {"name": "c", "expiry": self._iso(30)},
                "beyond": {"name": "d", "expiry": self._iso(31)},
                "none": {"name": "e"},
            }
        )
        states = {uid: item.expiry_state for uid, item in self._items().items()}
        self.assertEqual(
            states, {"yesterday": "expired", "today": "soon", "limit": "soon", "beyond": "ok", "none": "none"}
        )
        self.assertEqual(self._items()["yesterday"].days_left, -1)
        self.assertEqual(self._items()["limit"].days_left, 30)

    def test_uo_alert_counters_and_action(self):
        self._pull(
            {
                "e1": {"name": "a", "expiry": self._iso(-30)},
                "e2": {"name": "b", "expiry": self._iso(-1)},
                "s1": {"name": "c", "expiry": self._iso(0)},
                "ok": {"name": "d", "expiry": self._iso(400)},
                "none": {"name": "e"},
            }
        )
        self.assertEqual(self.uo.pharmacy_expired_count, 2)
        self.assertEqual(self.uo.pharmacy_soon_count, 1)
        action = self.uo.action_open_pharmacy_alerts()
        shown = self.Item.search(action["domain"])
        self.assertEqual(set(shown.mapped("uid")), {"e1", "e2", "s1"})

    def test_alerts_are_scoped_to_the_uo(self):
        other = self.env["fireman.uo"].create({"name": "Autre", "code": "TP2"})
        self.service._apply_pharmacy(other, {"x": {"name": "x", "expiry": self._iso(-5)}})
        self._pull({"y": {"name": "y", "expiry": self._iso(-5)}})
        self.assertEqual(self.uo.pharmacy_expired_count, 1)
        self.assertEqual(self.Item.search(self.uo.action_open_pharmacy_alerts()["domain"]).uo_id, self.uo)

    def test_search_filters_match_the_states(self):
        self._pull(
            {
                "e": {"name": "a", "expiry": self._iso(-2)},
                "s": {"name": "b", "expiry": self._iso(5)},
                "o": {"name": "c", "expiry": self._iso(90)},
            }
        )
        self.assertEqual(
            set(self.Item.search(self.Item._expired_domain() + [("uo_id", "=", self.uo.id)]).mapped("uid")), {"e"}
        )
        self.assertEqual(
            set(self.Item.search(self.Item._soon_domain() + [("uo_id", "=", self.uo.id)]).mapped("uid")), {"s"}
        )

    def test_pharmacy_scope_is_accepted_by_the_pull_queue(self):
        Event = self.env["fireman.sync.event"]
        Event.enqueue_pull(self.uo.code, ["pharmacy", "inconnu"])
        event = Event.search([("direction", "=", "pull"), ("uo_code", "=", self.uo.code)], limit=1)
        self.assertEqual(event.payload, '["pharmacy"]')
