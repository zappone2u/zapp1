# -*- coding: utf-8 -*-
from odoo import fields, models

from .constants import RANKS


class ResPartner(models.Model):
    _inherit = "res.partner"

    firebase_uid = fields.Char(string="UID Firebase", index=True, copy=False)
    fireman_super_admin = fields.Boolean(
        string="Super administrateur Inventory Fireman",
        help="Membre de toutes les UO, non décompté dans les quotas.",
    )
    fireman_rank = fields.Selection([(r, r) for r in RANKS], string="Grade")
    fireman_first_name = fields.Char(string="Prénom (application)")
    fireman_last_name = fields.Char(string="Nom de famille (application)")
    fireman_member_ids = fields.One2many("fireman.member", "partner_id", string="Appartenances UO")

    _sql_constraints = [("firebase_uid_unique", "unique(firebase_uid)", "Cet UID Firebase est déjà lié à un contact.")]

    # ------------------------------------------------------------------
    # Firebase : grade, prénom et nom sont des champs du profil `users/{uid}`
    # ------------------------------------------------------------------
    _PROFILE_FIELDS = {"fireman_rank", "fireman_first_name", "fireman_last_name"}

    def write(self, vals):
        res = super().write(vals)
        if self._PROFILE_FIELDS & set(vals) and not self.env.context.get("fireman_no_push"):
            for partner in self.filtered("firebase_uid"):
                self.env["fireman.sync.event"]._enqueue_push("res.partner", partner.id, partner._push_profile_key(), "")
        return res

    def _push_profile_key(self):
        self.ensure_one()
        return f"user/{self.firebase_uid}"

    def _push_upsert(self, connector):
        self.ensure_one()
        first = (self.fireman_first_name or "").strip()
        last = (self.fireman_last_name or "").strip()
        values = {
            "rank": self.fireman_rank,
            "firstName": first,
            "lastName": last,
            "name": f"{first} {last}".strip(),
        }
        values = {k: v for k, v in values.items() if v}
        if self.firebase_uid and values:
            connector.set_user_fields(self.firebase_uid, values)
