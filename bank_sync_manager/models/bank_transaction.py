# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class BankTransaction(models.Model):
    _name = "bank.transaction"
    _description = "Bank Transaction"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(
        string="Transaction Number", required=True, copy=False, readonly=True, default=lambda self: _("New")
    )
    connection_id = fields.Many2one("bank.connection", string="Bank Connection", required=True, ondelete="cascade")
    reference = fields.Char(string="Bank Reference", required=True)
    date = fields.Date(string="Transaction Date", required=True, tracking=True)
    amount = fields.Monetary(string="Amount", required=True, tracking=True)
    currency_id = fields.Many2one("res.currency", string="Currency", default=lambda self: self.env.company.currency_id)

    # Partner Information
    partner_name = fields.Char(string="Partner Name")
    partner_account = fields.Char(string="Partner Account")
    partner_id = fields.Many2one("res.partner", string="Partner", tracking=True)

    description = fields.Text(string="Description")

    # Matching
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("unmatched", "Unmatched"),
            ("partial", "Partially Matched"),
            ("matched", "Matched"),
            ("reconciled", "Reconciled"),
        ],
        string="Status",
        default="draft",
        tracking=True,
    )

    matching_ids = fields.One2many("invoice.matching", "transaction_id", string="Invoice Matches")
    matched_invoice_ids = fields.Many2many(
        "account.move", string="Matched Invoices", compute="_compute_matched_invoices", store=True
    )
    matched_amount = fields.Monetary(string="Matched Amount", compute="_compute_matched_amount", store=True)
    remaining_amount = fields.Monetary(string="Remaining Amount", compute="_compute_matched_amount", store=True)

    # Accounting
    journal_id = fields.Many2one("account.journal", string="Journal", related="connection_id.journal_id", store=True)
    move_id = fields.Many2one("account.move", string="Journal Entry", readonly=True)

    notes = fields.Text(string="Notes")

    @api.model
    def create(self, vals):
        if vals.get("name", _("New")) == _("New"):
            vals["name"] = self.env["ir.sequence"].next_by_code("bank.transaction") or _("New")
        return super(BankTransaction, self).create(vals)

    @api.depends("matching_ids", "matching_ids.invoice_id")
    def _compute_matched_invoices(self):
        for record in self:
            record.matched_invoice_ids = record.matching_ids.mapped("invoice_id")

    @api.depends("matching_ids", "matching_ids.matched_amount", "amount")
    def _compute_matched_amount(self):
        for record in self:
            record.matched_amount = sum(record.matching_ids.mapped("matched_amount"))
            record.remaining_amount = record.amount - record.matched_amount

    @api.onchange("partner_name", "partner_account")
    def _onchange_partner_info(self):
        """Try to find partner based on name or account"""
        if self.partner_name and not self.partner_id:
            partner = self.env["res.partner"].search(
                ["|", ("name", "ilike", self.partner_name), ("ref", "=", self.partner_account)], limit=1
            )
            if partner:
                self.partner_id = partner

    def action_auto_match(self):
        """Automatically match transaction with invoices"""
        self.ensure_one()

        if not self.partner_id:
            raise UserError(_("Please select a partner first"))

        # Rechercher les factures correspondantes
        domain = [
            ("partner_id", "=", self.partner_id.id),
            ("move_type", "in", ["out_invoice", "out_refund"]),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
        ]

        # Chercher par montant exact
        invoices = self.env["account.move"].search(domain + [("amount_residual", "=", abs(self.amount))])

        if not invoices:
            # Chercher par période (±3 jours)
            date_from = self.date - timedelta(days=3)
            date_to = self.date + timedelta(days=3)
            invoices = self.env["account.move"].search(
                domain
                + [
                    ("invoice_date", ">=", date_from),
                    ("invoice_date", "<=", date_to),
                ],
                limit=5,
            )

        if invoices:
            # Créer les matchings
            for invoice in invoices:
                if invoice.amount_residual > 0:
                    matched_amount = min(abs(self.remaining_amount), invoice.amount_residual)
                    self.env["invoice.matching"].create(
                        {
                            "transaction_id": self.id,
                            "invoice_id": invoice.id,
                            "matched_amount": matched_amount,
                            "state": "auto",
                        }
                    )

            self.state = "matched" if self.remaining_amount == 0 else "partial"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Success"),
                    "message": _("%s invoice(s) matched") % len(invoices),
                    "type": "success",
                },
            }
        else:
            self.state = "unmatched"
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No Match"),
                    "message": _("No matching invoices found"),
                    "type": "warning",
                },
            }

    def action_reconcile(self):
        """Create account move and reconcile with invoices"""
        self.ensure_one()

        if self.state != "matched":
            raise UserError(_("Transaction must be fully matched before reconciliation"))

        if self.move_id:
            raise UserError(_("Transaction already reconciled"))

        # Créer l'écriture comptable
        move_vals = {"journal_id": self.journal_id.id, "date": self.date, "ref": self.reference, "line_ids": []}

        # Ligne de banque
        bank_line = {
            "account_id": self.journal_id.default_account_id.id,
            "name": self.description or self.reference,
            "debit": self.amount if self.amount > 0 else 0,
            "credit": abs(self.amount) if self.amount < 0 else 0,
            "partner_id": self.partner_id.id,
        }
        move_vals["line_ids"].append((0, 0, bank_line))

        # Lignes de contrepartie pour chaque facture
        for matching in self.matching_ids:
            invoice = matching.invoice_id
            receivable_account = invoice.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")[
                0
            ].account_id

            counterpart_line = {
                "account_id": receivable_account.id,
                "name": f"Payment: {invoice.name}",
                "debit": 0 if self.amount > 0 else matching.matched_amount,
                "credit": matching.matched_amount if self.amount > 0 else 0,
                "partner_id": self.partner_id.id,
            }
            move_vals["line_ids"].append((0, 0, counterpart_line))

        # Créer et poster l'écriture
        move = self.env["account.move"].create(move_vals)
        move.action_post()

        self.move_id = move.id
        self.state = "reconciled"

        # Réconcilier avec les factures
        for matching in self.matching_ids:
            matching.action_reconcile()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Success"),
                "message": _("Transaction reconciled successfully"),
                "type": "success",
            },
        }

    def action_view_matches(self):
        """View matched invoices"""
        self.ensure_one()
        return {
            "name": _("Matched Invoices"),
            "type": "ir.actions.act_window",
            "res_model": "invoice.matching",
            "view_mode": "list,form",
            "domain": [("transaction_id", "=", self.id)],
            "context": {"default_transaction_id": self.id},
        }


from datetime import timedelta
