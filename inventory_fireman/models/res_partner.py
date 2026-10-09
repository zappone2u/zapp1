# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    firebase_uid = fields.Char(string="UID Firebase", index=True, copy=False)
    fireman_super_admin = fields.Boolean(
        string="Super administrateur Inventory Fireman",
        help="Membre de toutes les UO, non décompté dans les quotas.",
    )
    fireman_member_ids = fields.One2many("fireman.member", "partner_id", string="Appartenances UO")

    _sql_constraints = [("firebase_uid_unique", "unique(firebase_uid)", "Cet UID Firebase est déjà lié à un contact.")]
