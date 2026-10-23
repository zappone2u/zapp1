# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged


@tagged("inventory_fireman")
class TestProfileSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Ancien nom", "code": "TPF"})
        cls.service = cls.env["fireman.sync.service"].with_context(fireman_no_push=True)

    def test_uo_name_is_read_from_firebase(self):
        self.service._apply_uo(self.uo, {"name": "  Caserne de Sierck  ", "verified": True})
        self.assertEqual(self.uo.name, "Caserne de Sierck")

    def test_uo_name_is_kept_when_missing_or_empty(self):
        self.service._apply_uo(self.uo, {"verified": True})
        self.service._apply_uo(self.uo, {"name": "  ", "verified": True})
        self.assertEqual(self.uo.name, "Ancien nom")

    def test_member_name_follows_the_app_profile(self):
        entry = {"uo_name": self.uo.code, "verified": False}
        user = (
            "uid-pf",
            {"email": "jean.dupont@example.com", "firstName": "Jean", "lastName": "Dupont", "uo": [entry]},
        )
        self.service._apply_members(self.uo, [user], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-pf")])
        self.assertEqual(partner.name, "Jean Dupont")

        user[1]["firstName"] = "Jeanne"
        self.service._apply_members(self.uo, [user], set())
        self.assertEqual(partner.name, "Jeanne Dupont")

    def test_member_without_profile_keeps_a_name_derived_from_email(self):
        user = ("uid-pg", {"email": "marie_curie@example.com", "uo": [{"uo_name": self.uo.code, "verified": False}]})
        self.service._apply_members(self.uo, [user], set())
        partner = self.env["res.partner"].search([("firebase_uid", "=", "uid-pg")])
        self.assertEqual(partner.name, "Marie Curie")
