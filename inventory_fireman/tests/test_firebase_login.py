# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged

from ..controllers.main import FiremanWebsite


@tagged("inventory_fireman")
class TestFirebaseUserCreation(TransactionCase):
    """Un contact existant ne doit pas pouvoir être revendiqué avec un email non vérifié."""

    def setUp(self):
        super().setUp()
        self.controller = FiremanWebsite()
        self.customer = self.env["res.partner"].create({"name": "Client", "email": "client@example.com"})

    def _login(self, uid, verified):
        return self.controller._find_or_create_user(self.env, uid, "client@example.com", verified, "Pirate")

    def test_unverified_email_does_not_take_over_an_existing_contact(self):
        user, error = self._login("pirate", verified=False)
        self.assertFalse(error)
        self.assertNotEqual(user.partner_id, self.customer)
        self.assertFalse(self.customer.firebase_uid)

    def test_verified_email_links_the_existing_contact(self):
        user, error = self._login("owner", verified=True)
        self.assertFalse(error)
        self.assertEqual(user.partner_id, self.customer)
        self.assertEqual(self.customer.firebase_uid, "owner")
