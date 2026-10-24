# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .utils import as_list, parse_app_date


def _first(data, *keys):
    """Première valeur renseignée : l'application écrit `inventorName`, les anciennes versions `name`."""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _co_inventors_text(raw):
    """Équipiers `[{rank, name}]` → "CPL Martin, SGT Durand"."""
    people = []
    for person in as_list(raw):
        if isinstance(person, dict):
            parts = (str(person.get("rank") or "").strip(), str(person.get("name") or "").strip())
            label = " ".join(p for p in parts if p)
            if label:
                people.append(label)
    return ", ".join(people)


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
    co_inventors = fields.Char(string="Équipiers", help="Autres personnes ayant participé à l'inventaire.")
    is_group = fields.Boolean(string="Fait à plusieurs", compute="_compute_is_group", store=True)
    is_complete = fields.Boolean(string="Complet")
    vehicle_name = fields.Char(string="Véhicule")
    lack = fields.Text(string="Manques")
    comment = fields.Text(string="Commentaire")

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Inventaire déjà enregistré.")]

    @api.depends("date_text")
    def _compute_date(self):
        for inventory in self:
            inventory.date = parse_app_date(inventory.date_text)

    @api.depends("co_inventors")
    def _compute_is_group(self):
        for inventory in self:
            inventory.is_group = bool(inventory.co_inventors)

    @api.model
    def _vals_from_firebase(self, data):
        return {
            "date_text": _first(data, "inventoryDateStr", "date"),
            "inventor_name": _first(data, "inventorName", "name"),
            "rank": str(data.get("rank") or ""),
            "co_inventors": _co_inventors_text(data.get("coInventors")),
            "is_complete": bool(data.get("inventorFull") or data.get("inventor_full")),
            "vehicle_name": str(data.get("vehicle") or ""),
            "lack": str(data.get("lack") or ""),
            "comment": str(data.get("moreDescription") or ""),
        }
