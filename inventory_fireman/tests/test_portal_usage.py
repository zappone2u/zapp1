# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.constants import TIERS


@tagged("inventory_fireman")
class TestPortalUsage(TransactionCase):
    """Rendu du tableau de bord : jauges, invitations en attente et alertes de pharmacie."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Test portail", "code": "TPO"})
        cls.alice = cls.env["res.partner"].create({"name": "alice", "email": "a@example.com", "firebase_uid": "ua"})
        cls.bob = cls.env["res.partner"].create({"name": "bob", "email": "b@example.com", "firebase_uid": "ub"})

    def _render(self, is_admin=True):
        html = self.env["ir.qweb"]._render(
            "inventory_fireman.fireman_uo_usage", {"uo": self.uo, "is_admin": is_admin, "tiers": dict(TIERS)}
        )
        return str(html)

    def test_full_quota_is_flagged_with_text_and_a_choice_to_upgrade(self):
        self.uo.invite_member(self.alice)
        html = self._render()
        self.assertIn("1 / 1", html)
        self.assertIn("Limite atteinte", html)
        self.assertIn("bg-danger", html)
        self.assertNotIn("bg-success", html.split("Utilisateurs")[1].split("Abonnement")[0])

    def test_pending_invitation_is_visible_to_admins_only(self):
        self.uo.invite_member(self.alice)
        self.uo.invite_member(self.bob)
        self.assertIn("1 personne(s) en attente de validation", self._render(is_admin=True))
        self.assertIn("palier sup", self._render(is_admin=True))
        self.assertNotIn("en attente de validation", self._render(is_admin=False))

    def test_pharmacy_alerts_are_shown_at_every_tier(self):
        self.assertEqual(self.uo.tier, "discovery")
        today = fields.Date.context_today(self.uo)
        self.env["fireman.pharmacy.item"].create(
            [
                {"uo_id": self.uo.id, "uid": "1", "name": "a", "expiry_date": today - timedelta(days=3)},
                {"uo_id": self.uo.id, "uid": "2", "name": "b", "expiry_date": today + timedelta(days=4)},
            ]
        )
        self.uo.invalidate_recordset()
        html = self._render(is_admin=False)
        self.assertIn("1 p\u00e9rim\u00e9(s)", html)
        self.assertIn("1 \u00e0 renouveler sous 30 jours", html)
        self.assertIn("alert-danger", html)

    def test_no_alert_banner_when_everything_is_fine(self):
        html = self._render()
        self.assertNotIn("Pharmacie :", html)
        self.assertNotIn("en attente de validation", html)

    def test_gauge_levels(self):
        gauge = lambda count, maximum: str(  # noqa: E731
            self.env["ir.qweb"]._render(
                "inventory_fireman.fireman_gauge",
                {"label": "x", "count": count, "maximum": maximum, "url": False, "link_label": ""},
            )
        )
        self.assertIn("bg-success", gauge(7, 10))
        self.assertIn("bg-warning", gauge(8, 10))
        self.assertIn("Presque complet", gauge(8, 10))
        self.assertIn("bg-danger", gauge(10, 10))
        self.assertIn("bg-danger", gauge(12, 10), "un d\u00e9passement reste rouge et born\u00e9 \u00e0 100 %")
        self.assertIn("width: 100%", gauge(12, 10))
        illimite = gauge(50, -1)
        self.assertIn("\u221e", illimite)
        self.assertNotIn("progress-bar", illimite)
