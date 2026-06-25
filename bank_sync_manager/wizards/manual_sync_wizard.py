# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime, timedelta


class ManualSyncWizard(models.TransientModel):
    _name = "manual.sync.wizard"
    _description = "Manual Bank Sync Wizard"

    connection_id = fields.Many2one("bank.connection", string="Bank Connection", required=True)
    date_from = fields.Date(
        string="From Date", required=True, default=lambda self: fields.Date.today() - timedelta(days=30)
    )
    date_to = fields.Date(string="To Date", required=True, default=fields.Date.today)

    auto_match = fields.Boolean(string="Auto Match with Invoices", default=True)
    match_partner_only = fields.Boolean(string="Match Same Partner Only", default=True)

    def action_sync(self):
        """Execute manual sync"""
        self.ensure_one()

        if self.date_from > self.date_to:
            raise UserError(_("From Date cannot be after To Date"))

        # Sync transactions
        self.connection_id.action_sync_transactions()

        # Auto match if enabled
        if self.auto_match:
            transactions = self.env["bank.transaction"].search(
                [
                    ("connection_id", "=", self.connection_id.id),
                    ("date", ">=", self.date_from),
                    ("date", "<=", self.date_to),
                    ("state", "in", ["draft", "unmatched"]),
                ]
            )

            matched_count = 0
            for transaction in transactions:
                if transaction.partner_id:
                    try:
                        transaction.action_auto_match()
                        if transaction.state in ["matched", "partial"]:
                            matched_count += 1
                    except:
                        pass

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Sync Complete"),
                    "message": _("%s transactions matched automatically") % matched_count,
                    "type": "success",
                    "sticky": False,
                },
            }

        return {"type": "ir.actions.act_window_close"}
