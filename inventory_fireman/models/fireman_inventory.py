# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .utils import parse_app_date


class FiremanInventory(models.Model):
    """Historique des inventaires réalisés dans l'application (lecture seule, alimenté depuis Firebase)."""

    _name = "fireman.inventory"
    _description = "Inventaire"
    _order = "date desc, id desc"

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, index=True, copy=False)
    date_text = fields.Char(string="Date (texte)")
    date = fields.Datetime(string="Date", compute="_compute_date", store=True)
    inventor_name = fields.Char(string="Inventoriste")
    rank = fields.Char(string="Grade")
    is_complete = fields.Boolean(string="Complet")
    vehicle_name = fields.Char(string="Véhicule")
    lack = fields.Text(string="Manques")
    comment = fields.Text(string="Commentaire")

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Inventaire déjà enregistré.")]

    @api.depends("date_text")
    def _compute_date(self):
        for inventory in self:
            inventory.date = parse_app_date(inventory.date_text)

    @api.model
    def _vals_from_firebase(self, data):
        return {
            "date_text": str(data.get("date") or ""),
            "inventor_name": str(data.get("name") or ""),
            "rank": str(data.get("rank") or ""),
            "is_complete": bool(data.get("inventor_full")),
            "vehicle_name": str(data.get("vehicle") or ""),
            "lack": str(data.get("lack") or ""),
            "comment": str(data.get("moreDescription") or ""),
        }
