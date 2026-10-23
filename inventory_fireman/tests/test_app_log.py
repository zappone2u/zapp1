# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged

from ..models import fireman_app_log


@tagged("inventory_fireman")
class TestAppLog(TransactionCase):
    """Rapports de l'application : acceptés sans UO ni connexion, nettoyés et plafonnés."""

    def setUp(self):
        super().setUp()
        self.Log = self.env["fireman.app.log"]

    def _report(self, **kw):
        return {"kind": "startup_crash", "message": "boom", "device_id": "dev-1", "app_version": "30.0.0", **kw}

    def test_report_without_uo_or_user_is_stored(self):
        self.assertEqual(self.Log._ingest([self._report(step="firebase")], ip="1.2.3.4"), 1)
        log = self.Log.search([("device_id", "=", "dev-1")])
        self.assertEqual((log.kind, log.step, log.verified, log.uo_code), ("startup_crash", "firebase", False, False))

    def test_identity_comes_from_the_verified_token_not_the_payload(self):
        self.Log._ingest(
            [self._report(user_email="evil@example.com")], identity={"uid": "u1", "email": "real@example.com"}
        )
        log = self.Log.search([("device_id", "=", "dev-1")])
        self.assertEqual((log.user_email, log.firebase_uid, log.verified), ("real@example.com", "u1", True))

    def test_unknown_values_and_oversized_fields_are_normalised(self):
        self.Log._ingest([self._report(kind="nope", level="nope", message="x" * 5000, uo="pas valide!")])
        log = self.Log.search([("device_id", "=", "dev-1")])
        self.assertEqual((log.kind, log.level, log.uo_code), ("other", "error", False))
        self.assertEqual(len(log.message), 2000)

    def test_same_error_shares_a_fingerprint_despite_changing_numbers(self):
        self.Log._ingest(
            [
                self._report(message="Erreur 12 a 0xAB", stack="#0 f (a.dart:10)"),
                self._report(message="Erreur 99 a 0xCD", stack="#0 f (a.dart:10)"),
            ]
        )
        self.assertEqual(len(set(self.Log.search([]).mapped("fingerprint"))), 1)

    def test_device_is_rate_limited(self):
        reports = [self._report() for _ in range(fireman_app_log.MAX_REPORTS_PER_REQUEST + 5)]
        self.assertEqual(self.Log._ingest(reports), fireman_app_log.MAX_REPORTS_PER_REQUEST)
        for _ in range(3):
            self.Log._ingest(reports)
        self.assertEqual(self.Log._ingest([self._report()]), 0)
        self.assertEqual(self.Log._ingest([self._report(device_id="dev-2")]), 1)
