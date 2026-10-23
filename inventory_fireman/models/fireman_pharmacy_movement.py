# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .utils import as_int, parse_day, to_naive_utc

MOVEMENT_KINDS = [
    ("restock", "Réarmement d'un véhicule"),
    ("withdrawal", "Prélèvement"),
    ("expired", "Retrait pour péremption"),
    ("other", "Autre"),
]


class FiremanPharmacyMovement(models.Model):
    """Trace des sorties de stock de la pharmacie (lecture seule, alimentée depuis Firebase)."""

    _name = "fireman.pharmacy.movement"
    _description = "Mouvement de pharmacie"
    _order = "date desc, id desc"

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, index=True, copy=False)
    date = fields.Datetime(string="Date")
    kind = fields.Selection(MOVEMENT_KINDS, string="Type", required=True, default="other")
    name = fields.Char(string="Article", required=True)
    lot = fields.Char(string="N° de lot")
    expiry_date = fields.Date(string="Péremption")
    quantity = fields.Integer(string="Quantité")
    user_name = fields.Char(string="Fait par")
    reference = fields.Char(string="Référence")

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Mouvement déjà enregistré.")]

    @api.model
    def _vals_from_firebase(self, data):
        kind = str(data.get("kind") or "")
        return {
            "date": to_naive_utc(data.get("date")),
            "kind": kind if kind in dict(MOVEMENT_KINDS) else "other",
            "name": str(data.get("name") or "(sans nom)"),
            "lot": str(data.get("lot") or ""),
            "expiry_date": parse_day(data.get("expiry")),
            "quantity": max(as_int(data.get("quantity")), 0),
            "user_name": str(data.get("by") or ""),
            "reference": str(data.get("ref") or ""),
        }
