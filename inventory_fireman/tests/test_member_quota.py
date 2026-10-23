# -*- coding: utf-8 -*-
from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.utils import QuotaExceeded


@tagged("inventory_fireman")
class TestMemberQuota(TransactionCase):
    """Le palier Découverte (sans abonnement) autorise un seul utilisateur."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uo = cls.env["fireman.uo"].create({"name": "Test quota", "code": "TQ1"})
        cls.Member = cls.env["fireman.member"]
        cls.Partner = cls.env["res.partner"]
        cls.alice = cls._partner("alice", "uid-alice")
        cls.bob = cls._partner("bob", "uid-bob")

    @classmethod
    def _partner(cls, name, uid):
        return cls.Partner.create({"name": name, "email": f"{name}@example.com", "firebase_uid": uid})

    def _members(self):
        return self.Member.search([("uo_id", "=", self.uo.id), ("partner_id.fireman_super_admin", "=", False)])

    def _firebase_user(self, partner, verified):
        entry = {"uo_name": self.uo.code, "verified": verified, "uo_admin": False, "uo_pharmacist": False}
        return partner.firebase_uid, {"email": partner.email, "uo": [entry]}

    def _pull_members(self, *users):
        service = self.env["fireman.sync.service"].with_context(fireman_no_push=True)
        service._apply_members(self.uo, list(users), set())

    def _pending_member_pushes(self, partner):
        return self.env["fireman.sync.event"].search(
            [
                ("direction", "=", "push"),
                ("model", "=", "fireman.member"),
                ("target_key", "=", f"{self.uo.code}/{partner.firebase_uid}"),
                ("state", "=", "pending"),
            ]
        )

    def test_refused_member_is_not_kept(self):
        self.Member.create({"uo_id": self.uo.id, "partner_id": self.alice.id, "verified": True})
        # assertRaises ouvre son propre point de sauvegarde et masquerait le défaut : on intercepte à la main.
        try:
            self.Member.create({"uo_id": self.uo.id, "partner_id": self.bob.id, "verified": True})
        except QuotaExceeded:
            pass
        else:
            self.fail("QuotaExceeded attendue")
        self.assertEqual(self._members().partner_id, self.alice)

    def test_invite_is_verified_when_a_slot_is_free(self):
        member = self.uo.invite_member(self.alice)
        self.assertTrue(member.verified)
        self.assertFalse(member.quota_suspended)

    def test_invite_stays_pending_when_full(self):
        self.uo.invite_member(self.alice)
        member = self.uo.invite_member(self.bob, is_admin=True)
        self.assertFalse(member.verified)
        self.assertTrue(member.quota_suspended)
        self.assertTrue(member.is_admin)
        self.assertEqual(self.uo.member_count, 1)

    def test_validation_from_the_app_is_refused_when_full(self):
        self.uo.invite_member(self.alice)
        self.uo.last_pull_date = fields.Datetime.now()
        self._pull_members(self._firebase_user(self.alice, True), self._firebase_user(self.bob, True))
        bob = self._members().filtered(lambda m: m.partner_id == self.bob)
        self.assertFalse(bob.verified)
        self.assertTrue(bob.quota_suspended)
        self.assertTrue(self._members().filtered(lambda m: m.partner_id == self.alice).verified)
        self.assertTrue(self._pending_member_pushes(self.bob), "Firebase doit être corrigé")

    def test_existing_verified_member_is_untouched_by_the_app(self):
        self.uo.invite_member(self.alice)
        self._pending_member_pushes(self.alice).unlink()  # l'invitation elle-même met un envoi en file
        self.uo.last_pull_date = fields.Datetime.now()
        self._pull_members(self._firebase_user(self.alice, True))
        self.assertTrue(self._members().verified)
        self.assertFalse(self._pending_member_pushes(self.alice))

    def test_first_read_of_an_uo_keeps_existing_state(self):
        self.assertFalse(self.uo.last_pull_date)
        self._pull_members(self._firebase_user(self.alice, True), self._firebase_user(self.bob, True))
        self.assertEqual(len(self._members().filtered("verified")), 2)

    def test_member_removed_in_the_app_disappears_and_frees_the_slot(self):
        self.uo.invite_member(self.alice)
        pending = self.uo.invite_member(self.bob)
        self.uo.last_pull_date = fields.Datetime.now()
        # L'application a retiré l'UO du compte d'alice : seule bob y figure encore.
        self._pull_members(self._firebase_user(self.bob, False))
        self.assertEqual(self._members().partner_id, self.bob)
        self.uo._enforce_quotas()
        self.assertTrue(pending.verified, "la place libérée est attribuée à la personne en attente")

    def test_removal_only_concerns_the_removed_person(self):
        self.uo.invite_member(self.alice)
        self.uo.invite_member(self.bob)
        self.uo.last_pull_date = fields.Datetime.now()
        self._pull_members(self._firebase_user(self.alice, True), self._firebase_user(self.bob, False))
        self.assertEqual(self._members().partner_id, self.alice | self.bob)

    def test_pending_member_is_activated_when_a_slot_is_freed(self):
        alice = self.uo.invite_member(self.alice)
        bob = self.uo.invite_member(self.bob)
        self.assertFalse(bob.verified)
        alice.unlink()
        self.uo._enforce_quotas()
        self.assertTrue(bob.verified)
        self.assertFalse(bob.quota_suspended)
