# -*- coding: utf-8 -*-
import re

from odoo.tests import HttpCase, TransactionCase, tagged


@tagged("inventory_fireman")
class TestRankAndGroupInventory(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Caserne", "code": "TRG"})
        cls.service = cls.env["fireman.sync.service"].with_context(fireman_no_push=True)

    def _user(self, **extra):
        return ("uid-rg", {"email": "rg@example.com", "uo": [{"uo_name": self.uo.code, "verified": True}], **extra})

    def test_inventory_reads_the_keys_written_by_the_app(self):
        raw = {
            "inv-1": {
                "vehicle": "VSAV",
                "rank": "SGT",
                "inventorName": "Dupont",
                "inventorFull": True,
                "inventoryDateStr": "11/10/2024 7:34",
                "coInventors": [{"rank": "CPL", "name": "Martin"}, {"rank": "SA1", "name": "Durand"}],
            },
            "inv-2": {"vehicle": "VTU", "name": "Ancien", "inventor_full": False, "date": "1/2/2023 8:00"},
        }
        self.service._apply_inventories(self.uo, raw)
        solo, group = self.uo.inventory_ids.sorted("uid", reverse=True)
        self.assertEqual((group.inventor_name, group.rank, group.is_complete), ("Dupont", "SGT", True))
        self.assertEqual(group.date_text, "11/10/2024 7:34")
        self.assertEqual(group.co_inventors, "CPL Martin, SA1 Durand")
        self.assertTrue(group.is_group)
        self.assertEqual((solo.inventor_name, solo.is_group), ("Ancien", False))

    def test_existing_inventory_is_corrected_on_next_read(self):
        self.env["fireman.inventory"].create({"uo_id": self.uo.id, "uid": "inv-1", "vehicle_name": "VSAV"})
        self.service._apply_inventories(self.uo, {"inv-1": {"vehicle": "VSAV", "inventorName": "Dupont"}})
        self.assertEqual(self.uo.inventory_ids.inventor_name, "Dupont")

    def test_rank_is_read_from_the_app_profile(self):
        self.service._apply_members(self.uo, [self._user(rank="CCH")], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-rg")])
        self.assertEqual(partner.fireman_rank, "CCH")
        self.service._apply_members(self.uo, [self._user(rank="inconnu")], set())
        self.assertEqual(partner.fireman_rank, "CCH")

    def test_rank_changed_in_odoo_is_pushed_and_not_overwritten_by_a_read(self):
        self.service._apply_members(self.uo, [self._user(rank="CCH")], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-rg")])
        partner.with_context(fireman_no_push=False).fireman_rank = "ADJ"
        events = self.env["fireman.sync.event"].search([("model", "=", "res.partner"), ("res_id", "=", partner.id)])
        self.assertEqual(events.mapped("target_key"), ["user/uid-rg"])
        blocked = self.env["fireman.sync.event"]._blocked_targets()
        self.service._apply_members(self.uo, [self._user(rank="CCH")], blocked)
        self.assertEqual(partner.fireman_rank, "ADJ")

    def test_rank_push_writes_the_user_profile(self):
        partner = self.env["res.partner"].create({"name": "p", "firebase_uid": "uid-p", "fireman_rank": "LTN"})
        calls = []

        class Connector:
            def set_user_fields(self, uid, values):
                calls.append((uid, values))

        partner._push_upsert(Connector())
        self.assertEqual(calls, [("uid-p", {"rank": "LTN"})])

    def test_pull_does_not_push_back(self):
        self.service._apply_members(self.uo, [self._user(rank="CCH")], set())
        self.assertFalse(self.env["fireman.sync.event"].search([("model", "=", "res.partner")]))

    def test_profile_fields_are_read_from_the_app(self):
        self.service._apply_members(self.uo, [self._user(firstName="Jean", lastName="Dupont", rank="SGT")], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-rg")])
        self.assertEqual(
            (partner.fireman_first_name, partner.fireman_last_name, partner.name), ("Jean", "Dupont", "Jean Dupont")
        )
        self.service._apply_members(
            self.uo, [self._user(firstName="Jean", lastName="Durand", email="new@example.com")], set()
        )
        self.assertEqual((partner.name, partner.email), ("Jean Durand", "new@example.com"))

    def test_name_changed_in_odoo_is_pushed_with_first_and_last_name(self):
        self.service._apply_members(self.uo, [self._user(firstName="Jean", lastName="Dupont")], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-rg")])
        partner.with_context(fireman_no_push=False).write({"fireman_first_name": "Jeanne", "fireman_rank": "CPL"})
        blocked = self.env["fireman.sync.event"]._blocked_targets()
        self.service._apply_members(self.uo, [self._user(firstName="Jean", lastName="Dupont")], blocked)
        self.assertEqual(partner.fireman_first_name, "Jeanne")
        calls = []

        class Connector:
            def set_user_fields(self, uid, values):
                calls.append((uid, values))

        partner._push_upsert(Connector())
        self.assertEqual(
            calls,
            [("uid-rg", {"rank": "CPL", "firstName": "Jeanne", "lastName": "Dupont", "name": "Jeanne Dupont"})],
        )

    def test_intervention_keeps_vehicle_and_item_ids(self):
        self.service._apply_interventions(
            self.uo,
            {
                "i1": {
                    "vehicleId": "v1",
                    "vehicle": "VSAV",
                    "lines": {"l1": {"itemId": "it1", "name": "Gants", "quantity": 2}},
                }
            },
        )
        intervention = self.uo.env["fireman.intervention"].search([("uo_id", "=", self.uo.id)])
        self.assertEqual((intervention.vehicle_uid, intervention.line_ids.item_uid), ("v1", "it1"))


@tagged("inventory_fireman", "post_install", "-at_install")
class TestPortalAccount(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create(
            {
                "name": "Portail",
                "login": "portal-rank",
                "password": "portal-rank-pw",
                "email": "portal-rank@example.com",
                "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )

    def _token(self):
        page = self.url_open("/my/account").text
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def test_page_only_asks_for_the_essentials(self):
        self.authenticate("portal-rank", "portal-rank-pw")
        html = self.url_open("/my/account").text
        self.assertIn('name="rank"', html)
        for field in ("phone", "street", "company_name", "vat"):
            self.assertNotIn(f'name="{field}"', html)

    def test_rank_and_name_are_saved_and_pushed(self):
        self.user.partner_id.firebase_uid = "uid-portal"
        self.authenticate("portal-rank", "portal-rank-pw")
        data = {"csrf_token": self._token(), "rank": "SCH", "first_name": "Nouveau", "last_name": "Nom"}
        self.url_open("/my/account", data=data)
        partner = self.user.partner_id
        self.assertEqual(
            (partner.fireman_rank, partner.fireman_first_name, partner.fireman_last_name, partner.name),
            ("SCH", "Nouveau", "Nom", "Nouveau Nom"),
        )
        events = self.env["fireman.sync.event"].search([("model", "=", "res.partner"), ("res_id", "=", partner.id)])
        self.assertEqual(events.mapped("target_key"), ["user/uid-portal"])

    def test_invalid_data_is_refused(self):
        self.authenticate("portal-rank", "portal-rank-pw")
        base = {"csrf_token": self._token(), "first_name": "A", "last_name": "B"}
        self.url_open("/my/account", data={**base, "rank": "GENERAL"})
        self.url_open("/my/account", data={**base, "rank": "COL", "first_name": " "})
        self.assertFalse(self.user.partner_id.fireman_rank)
