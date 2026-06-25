# -*- coding: utf-8 -*-
"""
Webhook Firebase → Odoo
========================
Endpoint appelé en temps réel par une Cloud Function Firebase dès qu'une
donnée change dans la Realtime Database (inventaire, véhicule, statut pompier).

Sécurité : la requête doit porter un header `X-Firebase-Webhook-Secret`
dont la valeur correspond à la clé configurée dans le connecteur Firebase
(champ `webhook_secret`).  Si le secret est absent ou incorrect → 401.

Routes disponibles :
  POST /firebase/webhook/inventory   → màj d'un inventaire (firebase.inventory.history)
  POST /firebase/webhook/vehicle     → màj d'un véhicule   (firebase.vehicle)
  POST /firebase/webhook/pompier     → màj d'un pompier     (res.partner is_firebase_pompier)
  POST /firebase/webhook/uo          → màj d'une UO         (res.partner is_firebase_uo)
"""

import json
import logging
from datetime import datetime

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  Clé secrète partagée (à placer aussi dans les variables d'env de Firebase)
#  Peut être surchargée par le champ `webhook_secret` du connecteur Firebase.
# ──────────────────────────────────────────────────────────────────────────────
FALLBACK_SECRET = "fireman_webhook_secret_change_me"


def _parse_firebase_date(value):
    """Convertit une date texte Firebase en datetime naive Odoo."""
    if not value:
        return False
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return False


class FirebaseWebhook(http.Controller):
    # ─────────────────────────────────────────────────────────────────────────
    #  Vérification du secret partagé
    # ─────────────────────────────────────────────────────────────────────────

    def _check_secret(self):
        """Vérifie le header X-Firebase-Webhook-Secret. Renvoie True si OK."""
        incoming_secret = request.httprequest.headers.get("X-Firebase-Webhook-Secret", "")

        # Récupérer le secret depuis le connecteur (si configuré)
        connector = request.env["firebase.connector"].sudo().search([], limit=1)
        expected = getattr(connector, "webhook_secret", None) or FALLBACK_SECRET

        if incoming_secret != expected:
            _logger.warning(
                "Webhook Firebase refusé — secret invalide depuis %s",
                request.httprequest.remote_addr,
            )
            return False
        return True

    def _json_response(self, data, status=200):
        """Helper pour retourner une réponse JSON."""
        return request.make_response(
            json.dumps(data),
            headers=[("Content-Type", "application/json")],
            status=status,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  INVENTAIRE  →  firebase.inventory.history
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        "/firebase/webhook/inventory",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_inventory(self, **kw):
        """
        Reçoit un inventaire créé/modifié dans Firebase et le synchronise dans Odoo.

        Payload JSON attendu :
        {
            "event"         : "created" | "updated" | "deleted",
            "firebase_uid"  : "<uid de l'inventaire>",
            "uo_code"       : "<trigramme UO, ex: SIK>",
            "data": {
                "inventoryDateStr"   : "11/10/2024 7:34",
                "inventorName"       : "Jean Dupont",
                "rank"               : "SGT",
                "inventorFull"       : true,
                "vehicle"            : "VSAV 1",
                "lack"               : "...",
                "moreDescription"    : "..."
            }
        }
        """
        if not self._check_secret():
            return self._json_response({"error": "Unauthorized"}, 401)

        try:
            payload = json.loads(request.httprequest.data or b"{}")
        except json.JSONDecodeError:
            return self._json_response({"error": "Invalid JSON"}, 400)

        event = payload.get("event", "updated")
        firebase_uid = payload.get("firebase_uid", "").strip()
        uo_code = payload.get("uo_code", "").strip().upper()
        data = payload.get("data", {})

        if not firebase_uid or not uo_code:
            return self._json_response({"error": "firebase_uid and uo_code are required"}, 400)

        # Chercher l'UO dans Odoo
        uo = (
            request.env["res.partner"]
            .sudo()
            .search([("uo_code", "=", uo_code), ("is_firebase_uo", "=", True)], limit=1)
        )
        if not uo:
            _logger.warning("Webhook inventory : UO '%s' introuvable dans Odoo", uo_code)
            return self._json_response({"error": f"UO '{uo_code}' not found"}, 404)

        InventoryHistory = request.env["firebase.inventory.history"].sudo().with_context(from_firebase=True)

        if event == "deleted":
            record = InventoryHistory.search([("firebase_uid", "=", firebase_uid)], limit=1)
            if record:
                record.unlink()
                _logger.info("Webhook: inventaire %s supprimé", firebase_uid)
            return self._json_response({"status": "deleted"})

        # Créé ou modifié
        vals = {
            "uo_id": uo.id,
            "firebase_uid": firebase_uid,
            "inventory_date_str": data.get("inventoryDateStr") or data.get("inventory_date_str", ""),
            "inventor_name": data.get("inventorName") or data.get("inventor_name", ""),
            "rank": data.get("rank", ""),
            "inventor_full": bool(data.get("inventorFull", data.get("inventor_full", False))),
            "vehicle": data.get("vehicle", ""),
            "lack": data.get("lack", ""),
            "more_description": data.get("moreDescription") or data.get("more_description", ""),
            "last_sync_date": fields.Datetime.now(),
        }

        existing = InventoryHistory.search([("firebase_uid", "=", firebase_uid)], limit=1)
        if existing:
            existing.write(vals)
            _logger.info("Webhook: inventaire %s mis à jour (UO: %s)", firebase_uid, uo_code)
            return self._json_response({"status": "updated", "id": existing.id})
        else:
            record = InventoryHistory.create(vals)
            _logger.info("Webhook: inventaire %s créé (UO: %s)", firebase_uid, uo_code)
            return self._json_response({"status": "created", "id": record.id})

    # ─────────────────────────────────────────────────────────────────────────
    #  VÉHICULE  →  firebase.vehicle
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        "/firebase/webhook/vehicle",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_vehicle(self, **kw):
        """
        Reçoit un véhicule créé/modifié dans Firebase.

        Payload JSON attendu :
        {
            "event"        : "created" | "updated" | "deleted",
            "firebase_uid" : "<uid du véhicule>",
            "uo_code"      : "<trigramme UO>",
            "data": {
                "label"        : "VSAV 1",
                "licensePlate" : "AA-123-BB",
                "status"       : "nothing" | "broken" | "repair",
                "notes"        : "...",
                "position"     : 0
            }
        }
        """
        if not self._check_secret():
            return self._json_response({"error": "Unauthorized"}, 401)

        try:
            payload = json.loads(request.httprequest.data or b"{}")
        except json.JSONDecodeError:
            return self._json_response({"error": "Invalid JSON"}, 400)

        event = payload.get("event", "updated")
        firebase_uid = payload.get("firebase_uid", "").strip()
        uo_code = payload.get("uo_code", "").strip().upper()
        data = payload.get("data", {})

        if not firebase_uid or not uo_code:
            return self._json_response({"error": "firebase_uid and uo_code are required"}, 400)

        uo = (
            request.env["res.partner"]
            .sudo()
            .search([("uo_code", "=", uo_code), ("is_firebase_uo", "=", True)], limit=1)
        )
        if not uo:
            return self._json_response({"error": f"UO '{uo_code}' not found"}, 404)

        Vehicle = request.env["firebase.vehicle"].sudo().with_context(from_firebase=True)

        if event == "deleted":
            record = Vehicle.search([("firebase_uid", "=", firebase_uid), ("uo_id", "=", uo.id)], limit=1)
            if record:
                record.unlink()
                _logger.info("Webhook: véhicule %s supprimé", firebase_uid)
            return self._json_response({"status": "deleted"})

        # Normaliser le statut Firebase → sélection Odoo
        status_map = {"nothing": "nothing", "broken": "broken", "repair": "repair"}
        raw_status = data.get("status", "nothing")
        odoo_status = status_map.get(raw_status, "nothing")

        vals = {
            "uo_id": uo.id,
            "firebase_uid": firebase_uid,
            "label": data.get("label", ""),
            "license_plate": data.get("licensePlate") or data.get("license_plate", ""),
            "status": odoo_status,
            "notes": data.get("notes", ""),
            "position": int(data.get("position", 0)),
            "verified": bool(data.get("verified", False)),
            "last_sync_date": fields.Datetime.now(),
        }

        existing = Vehicle.search([("firebase_uid", "=", firebase_uid), ("uo_id", "=", uo.id)], limit=1)
        if existing:
            existing.write(vals)
            _logger.info("Webhook: véhicule %s mis à jour", firebase_uid)
            return self._json_response({"status": "updated", "id": existing.id})
        else:
            record = Vehicle.create(vals)
            _logger.info("Webhook: véhicule %s créé", firebase_uid)
            return self._json_response({"status": "created", "id": record.id})

    # ─────────────────────────────────────────────────────────────────────────
    #  POMPIER  →  res.partner (is_firebase_pompier)
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        "/firebase/webhook/pompier",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_pompier(self, **kw):
        """
        Reçoit une mise à jour d'un profil pompier depuis Firebase.

        Payload JSON attendu :
        {
            "event"        : "created" | "updated" | "deleted",
            "firebase_uid" : "<uid Firebase du pompier>",
            "data": {
                "name"      : "Jean Dupont",
                "email"     : "jean@example.com",
                "grade"     : "SGT",
                "matricule" : "123",
                "status"    : "active" | "inactive" | "suspended"
            }
        }
        """
        if not self._check_secret():
            return self._json_response({"error": "Unauthorized"}, 401)

        try:
            payload = json.loads(request.httprequest.data or b"{}")
        except json.JSONDecodeError:
            return self._json_response({"error": "Invalid JSON"}, 400)

        event = payload.get("event", "updated")
        firebase_uid = payload.get("firebase_uid", "").strip()
        data = payload.get("data", {})

        if not firebase_uid:
            return self._json_response({"error": "firebase_uid is required"}, 400)

        Partner = request.env["res.partner"].sudo()

        if event == "deleted":
            record = Partner.search([("firebase_uid", "=", firebase_uid), ("is_firebase_pompier", "=", True)], limit=1)
            if record:
                record.write({"pompier_status": "inactive"})
                _logger.info("Webhook: pompier %s désactivé", firebase_uid)
            return self._json_response({"status": "deactivated"})

        vals = {}
        if data.get("name"):
            vals["name"] = data["name"]
        if data.get("email"):
            vals["email"] = data["email"]
        if data.get("grade"):
            vals["grade"] = data["grade"]
        if data.get("matricule"):
            vals["matricule"] = data["matricule"]
        if data.get("status") in ("active", "inactive", "suspended"):
            vals["pompier_status"] = data["status"]
        if data.get("last_login"):
            vals["last_login"] = _parse_firebase_date(data["last_login"]) or fields.Datetime.now()
        vals["last_sync_date"] = fields.Datetime.now()

        existing = Partner.search([("firebase_uid", "=", firebase_uid), ("is_firebase_pompier", "=", True)], limit=1)
        if existing:
            existing.write(vals)
            _logger.info("Webhook: pompier %s mis à jour", firebase_uid)
            return self._json_response({"status": "updated", "id": existing.id})
        else:
            # Créer un nouveau pompier minimal (pas d'UO liée ici, sera géré par la sync cron)
            vals.update(
                {
                    "firebase_uid": firebase_uid,
                    "is_firebase_pompier": True,
                    "is_company": False,
                    "name": data.get("name", "Pompier sans nom"),
                    "sync_status": "synced",
                }
            )
            record = Partner.create(vals)
            _logger.info("Webhook: pompier %s créé", firebase_uid)
            return self._json_response({"status": "created", "id": record.id})

    # ─────────────────────────────────────────────────────────────────────────
    #  UO  →  res.partner (is_firebase_uo)
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        "/firebase/webhook/uo",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_uo(self, **kw):
        """
        Reçoit une mise à jour d'une UO depuis Firebase.

        Payload JSON attendu :
        {
            "event"    : "updated",
            "uo_code"  : "SIK",
            "data": {
                "prefillTheQty"        : true,
                "sendInventoryToAll"   : false,
                "verified"             : true
            }
        }
        """
        if not self._check_secret():
            return self._json_response({"error": "Unauthorized"}, 401)

        try:
            payload = json.loads(request.httprequest.data or b"{}")
        except json.JSONDecodeError:
            return self._json_response({"error": "Invalid JSON"}, 400)

        uo_code = payload.get("uo_code", "").strip().upper()
        data = payload.get("data", {})

        if not uo_code:
            return self._json_response({"error": "uo_code is required"}, 400)

        uo = (
            request.env["res.partner"]
            .sudo()
            .search([("uo_code", "=", uo_code), ("is_firebase_uo", "=", True)], limit=1)
        )
        if not uo:
            return self._json_response({"error": f"UO '{uo_code}' not found"}, 404)

        vals = {"last_sync_date": fields.Datetime.now()}
        if "prefillTheQty" in data:
            vals["uo_prefill_qty"] = bool(data["prefillTheQty"])
        if "sendInventoryToAll" in data:
            vals["uo_send_inventory_to_all"] = bool(data["sendInventoryToAll"])
        if "verified" in data:
            vals["uo_verified"] = bool(data["verified"])

        uo.write(vals)
        _logger.info("Webhook: UO '%s' mise à jour depuis Firebase", uo_code)

        # Notifier l'UI (backend + portail de cette UO) pour rafraîchissement instantané
        connector = request.env["firebase.connector"].sudo()
        payload = {"model": "res.partner", "id": uo.id, "event": "updated", "uo_id": uo.id, "uo_code": uo_code}
        connector._notify_realtime("inventory_fireman", payload)
        connector._notify_realtime("inventory_fireman_uo_%s" % uo.id, payload)

        return self._json_response({"status": "updated", "id": uo.id})
