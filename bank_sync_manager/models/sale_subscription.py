# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class SaleOrder(models.Model):
    _inherit = "sale.order"

    monitor_id = fields.Many2one("subscription.monitor", string="Payment Monitor", compute="_compute_monitor_id")
    payment_status = fields.Selection(related="monitor_id.payment_status", string="Payment Status", readonly=True)
    days_overdue = fields.Integer(related="monitor_id.days_overdue", string="Days Overdue", readonly=True)

    def _compute_monitor_id(self):
        """Compute monitor_id by searching for existing monitor"""
        for record in self:
            if record.id:
                monitor = self.env["subscription.monitor"].search([("subscription_id", "=", record.id)], limit=1)
                record.monitor_id = monitor.id if monitor else False
            else:
                record.monitor_id = False

    def check_payment_status(self):
        """Check payment status for this subscription"""
        self.ensure_one()
        if self.monitor_id:
            self.monitor_id.action_check_payment_status()
        else:
            # Créer un monitor si inexistant
            self.env["subscription.monitor"].create(
                {
                    "subscription_id": self.id,
                }
            )

    def action_view_payment_monitor(self):
        """View payment monitor"""
        self.ensure_one()
        return {
            "name": _("Payment Monitor"),
            "type": "ir.actions.act_window",
            "res_model": "subscription.monitor",
            "view_mode": "form",
            "res_id": self.monitor_id.id,
            "target": "current",
        }


class AccountMove(models.Model):
    _inherit = "account.move"

    subscription_id = fields.Many2one(
        "sale.order", string="Subscription", compute="_compute_subscription_id", store=True
    )

    @api.depends("invoice_line_ids")
    def _compute_subscription_id(self):
        for record in self:
            subscriptions = record.invoice_line_ids.mapped("sale_line_ids.order_id")
            record.subscription_id = subscriptions[0] if subscriptions else False
