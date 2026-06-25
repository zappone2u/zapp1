# -*- coding: utf-8 -*-
"""
Mixin de synchronisation temps réel Firebase ⇄ Odoo
====================================================
Fournit la synchronisation *incrémentale* et *bidirectionnelle* :

  • Odoo → Firebase : à chaque create / write / unlink d'un enregistrement
    synchronisé, seule la donnée modifiée est poussée vers la Realtime Database
    (pas de re-synchronisation complète).

  • Firebase → Odoo : les webhooks écrivent avec le contexte ``from_firebase=True``
    pour éviter de renvoyer la donnée vers Firebase (anti-boucle).

  • Notification UI : chaque changement (dans les deux sens) émet une notification
    sur le bus Odoo pour rafraîchir instantanément le backend et le portail.

Chaque modèle qui hérite de ce mixin doit implémenter :
  - ``_firebase_rtdb_path()``  → chemin du noeud Realtime Database, ou None
  - ``_firebase_payload()``    → dict des données à pousser vers Firebase
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Champs purement techniques : leur modification ne doit PAS déclencher un push
# vers Firebase (ils sont écrits par la synchronisation elle-même).
_SYNC_METADATA_FIELDS = frozenset(
    {
        "last_sync_date",
        "firebase_data",
        "firebase_created_at",
        "is_sync_locked",
        "sync_lock_time",
    }
)


class FirebaseSyncMixin(models.AbstractModel):
    _name = "firebase.sync.mixin"
    _description = "Synchronisation temps réel Firebase"

    # ──────────────────────────────────────────────────────────────────────
    #  À surcharger dans les modèles concrets
    # ──────────────────────────────────────────────────────────────────────

    def _firebase_rtdb_path(self):
        """Chemin Realtime Database de l'enregistrement (ex: 'SIK/vehicle/<uid>').

        Retourner None pour ignorer le push (donnée incomplète, pas d'UO, etc.).
        """
        self.ensure_one()
        return None

    def _firebase_payload(self):
        """Données à pousser vers Firebase pour cet enregistrement."""
        self.ensure_one()
        return {}

    def _firebase_bus_channels(self):
        """Canaux bus à notifier lors d'un changement (backend + portail)."""
        self.ensure_one()
        channels = ["inventory_fireman"]
        uo = getattr(self, "uo_id", False)
        if uo:
            channels.append("inventory_fireman_uo_%s" % uo.id)
        return channels

    # ──────────────────────────────────────────────────────────────────────
    #  Mécanique interne
    # ──────────────────────────────────────────────────────────────────────

    def _firebase_connector(self):
        return self.env["firebase.connector"]._get_default()

    def _firebase_push(self):
        """Pousse les enregistrements modifiés vers Firebase (incrémental)."""
        if self.env.context.get("from_firebase"):
            # La donnée vient de Firebase : ne pas la renvoyer (anti-boucle).
            return
        connector = self._firebase_connector()
        if not connector:
            return
        for record in self:
            try:
                path = record._firebase_rtdb_path()
            except Exception:
                path = None
            if not path:
                continue
            payload = record._firebase_payload()
            if payload:
                connector._rtdb_update(path, payload)

    def _firebase_notify(self, event):
        """Émet une notification bus pour rafraîchir l'UI instantanément."""
        connector_model = self.env["firebase.connector"].sudo()
        for record in self:
            uo = getattr(record, "uo_id", False)
            payload = {
                "model": record._name,
                "id": record.id,
                "event": event,
                "uo_id": uo.id if uo else False,
                "uo_code": getattr(uo, "uo_code", False) if uo else False,
            }
            for channel in record._firebase_bus_channels():
                connector_model._notify_realtime(channel, payload)

    # ──────────────────────────────────────────────────────────────────────
    #  Surcharges ORM
    # ──────────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._firebase_push()
        records._firebase_notify("created")
        return records

    def write(self, vals):
        res = super().write(vals)
        # Ne pousser/notifier que si des champs « métier » ont changé.
        tracked = set(vals) - _SYNC_METADATA_FIELDS
        if tracked:
            self._firebase_push()
            self._firebase_notify("updated")
        return res

    def unlink(self):
        # Mémoriser chemins/canaux avant suppression.
        connector = self._firebase_connector()
        to_delete = []
        notifications = []
        for record in self:
            uo = getattr(record, "uo_id", False)
            notifications.append(
                {
                    "channels": record._firebase_bus_channels(),
                    "payload": {
                        "model": record._name,
                        "id": record.id,
                        "event": "deleted",
                        "uo_id": uo.id if uo else False,
                        "uo_code": getattr(uo, "uo_code", False) if uo else False,
                    },
                }
            )
            if connector and not self.env.context.get("from_firebase"):
                try:
                    path = record._firebase_rtdb_path()
                except Exception:
                    path = None
                if path:
                    to_delete.append(path)

        res = super().unlink()

        for path in to_delete:
            connector._rtdb_delete(path)

        connector_model = self.env["firebase.connector"].sudo()
        for notif in notifications:
            for channel in notif["channels"]:
                connector_model._notify_realtime(channel, notif["payload"])

        return res
