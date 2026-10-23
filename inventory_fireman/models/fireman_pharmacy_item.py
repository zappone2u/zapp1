# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models

from .constants import PHARMACY_SOON_DAYS
from .utils import as_int, parse_day


class FiremanPharmacyItem(models.Model):
    """Stock de pharmacie d'une UO (lecture seule, alimenté depuis Firebase)."""

    _name = "fireman.pharmacy.item"
    _description = "Article de pharmacie"
    _order = "expiry_date, name, id"

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, index=True, copy=False)
    name = fields.Char(string="Article", required=True)
    quantity = fields.Integer(string="Quantité")
    lot = fields.Char(string="N° de lot")
    expiry_date = fields.Date(string="Péremption", index=True)
    location = fields.Char(string="Emplacement")
    days_left = fields.Integer(string="Jours restants", compute="_compute_expiry")
    expiry_state = fields.Selection(
        [("none", "Sans date"), ("expired", "Périmé"), ("soon", "À renouveler"), ("ok", "Valide")],
        string="État",
        compute="_compute_expiry",
    )

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Article déjà enregistré.")]

    @api.depends("expiry_date")
    @api.depends_context("tz")
    def _compute_expiry(self):
        today = fields.Date.context_today(self)
        for item in self:
            if not item.expiry_date:
                item.days_left, item.expiry_state = 0, "none"
                continue
            item.days_left = (item.expiry_date - today).days
            if item.days_left < 0:
                item.expiry_state = "expired"
            elif item.days_left <= PHARMACY_SOON_DAYS:
                item.expiry_state = "soon"
            else:
                item.expiry_state = "ok"

    @api.model
    def _expired_domain(self):
        return [("expiry_date", "<", fields.Date.context_today(self))]

    @api.model
    def _soon_domain(self):
        today = fields.Date.context_today(self)
        return [("expiry_date", ">=", today), ("expiry_date", "<=", today + timedelta(days=PHARMACY_SOON_DAYS))]

    @api.model
    def _alert_domain(self):
        """Périmés ou à renouveler."""
        limit = fields.Date.context_today(self) + timedelta(days=PHARMACY_SOON_DAYS)
        return [("expiry_date", "!=", False), ("expiry_date", "<=", limit)]

    @api.model
    def _vals_from_firebase(self, data):
        return {
            "name": str(data.get("name") or _("(sans nom)")),
            "quantity": max(as_int(data.get("quantity")), 0),
            "lot": str(data.get("lot") or ""),
            "expiry_date": parse_day(data.get("expiry")),
            "location": str(data.get("location") or ""),
        }
