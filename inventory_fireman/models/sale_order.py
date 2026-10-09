# -*- coding: utf-8 -*-
from odoo import api, fields, models

# Champs dont la modification peut changer le palier de l'UO
TIER_FIELDS = {"state", "subscription_state", "order_line", "fireman_uo_id"}


class SaleOrder(models.Model):
    _inherit = "sale.order"

    fireman_uo_id = fields.Many2one(
        "fireman.uo",
        string="UO Inventory Fireman",
        compute="_compute_fireman_uo_id",
        recursive=True,
        store=True,
        readonly=False,
        copy=False,
        index=True,
        ondelete="restrict",
    )

    @api.depends("subscription_id.fireman_uo_id")
    def _compute_fireman_uo_id(self):
        # Renouvellements et upsells héritent de l'UO de l'abonnement parent.
        for order in self:
            order.fireman_uo_id = order.subscription_id.fireman_uo_id or order.fireman_uo_id

    def write(self, vals):
        previous_uos = self.fireman_uo_id if "fireman_uo_id" in vals else self.env["fireman.uo"]
        res = super().write(vals)
        if TIER_FIELDS & set(vals):
            (previous_uos | self.fireman_uo_id)._push_enqueue()
        return res
