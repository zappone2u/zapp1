# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class InvoiceMatching(models.Model):
    _name = "invoice.matching"
    _description = "Invoice Matching with Bank Transaction"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    transaction_id = fields.Many2one("bank.transaction", string="Bank Transaction", required=True, ondelete="cascade")
    invoice_id = fields.Many2one(
        "account.move",
        string="Invoice",
        required=True,
        ondelete="cascade",
        domain=[("move_type", "in", ["out_invoice", "out_refund"])],
    )

    # Amounts
    transaction_amount = fields.Monetary(string="Transaction Amount", related="transaction_id.amount", readonly=True)
    invoice_amount = fields.Monetary(string="Invoice Amount", related="invoice_id.amount_total", readonly=True)
    invoice_residual = fields.Monetary(string="Invoice Residual", related="invoice_id.amount_residual", readonly=True)
    matched_amount = fields.Monetary(string="Matched Amount", required=True, tracking=True)
    currency_id = fields.Many2one("res.currency", string="Currency", related="transaction_id.currency_id")

    # Matching Info
    state = fields.Selection(
        [
            ("auto", "Auto Matched"),
            ("manual", "Manual Match"),
            ("confirmed", "Confirmed"),
            ("reconciled", "Reconciled"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        default="manual",
        tracking=True,
    )

    match_score = fields.Float(string="Match Score", default=0.0, help="Automatic matching confidence score (0-100)")

    partner_id = fields.Many2one("res.partner", string="Partner", related="invoice_id.partner_id", readonly=True)

    # Subscription Info
    subscription_id = fields.Many2one(
        "sale.order", string="Subscription", related="invoice_id.subscription_id", readonly=True
    )

    notes = fields.Text(string="Notes")

    @api.constrains("matched_amount", "invoice_residual", "transaction_amount")
    def _check_matched_amount(self):
        for record in self:
            if record.matched_amount <= 0:
                raise ValidationError(_("Matched amount must be positive"))
            if record.matched_amount > record.invoice_residual:
                raise ValidationError(_("Matched amount cannot exceed invoice residual"))
            if record.matched_amount > abs(record.transaction_amount):
                raise ValidationError(_("Matched amount cannot exceed transaction amount"))

    @api.onchange("invoice_id")
    def _onchange_invoice_id(self):
        if self.invoice_id and self.transaction_id:
            # Proposer le montant résiduel minimum
            self.matched_amount = min(abs(self.transaction_id.remaining_amount), self.invoice_id.amount_residual)

            # Calculer le score de matching
            self._compute_match_score()

    def _compute_match_score(self):
        """Calculate automatic matching confidence score"""
        score = 0.0

        if not self.transaction_id or not self.invoice_id:
            self.match_score = score
            return

        # Score par montant (40 points)
        if abs(self.transaction_amount) == self.invoice_amount:
            score += 40
        elif abs(abs(self.transaction_amount) - self.invoice_amount) / self.invoice_amount < 0.05:
            score += 30  # Différence < 5%
        elif abs(abs(self.transaction_amount) - self.invoice_amount) / self.invoice_amount < 0.1:
            score += 20  # Différence < 10%

        # Score par date (30 points)
        if self.transaction_id.date and self.invoice_id.invoice_date:
            date_diff = abs((self.transaction_id.date - self.invoice_id.invoice_date).days)
            if date_diff == 0:
                score += 30
            elif date_diff <= 3:
                score += 25
            elif date_diff <= 7:
                score += 20
            elif date_diff <= 15:
                score += 10

        # Score par partenaire (30 points)
        if self.transaction_id.partner_id and self.invoice_id.partner_id:
            if self.transaction_id.partner_id == self.invoice_id.partner_id:
                score += 30
        elif self.transaction_id.partner_name and self.invoice_id.partner_id:
            if self.transaction_id.partner_name.lower() in self.invoice_id.partner_id.name.lower():
                score += 20

        self.match_score = score

    def action_confirm(self):
        """Confirm the matching"""
        for record in self:
            record.state = "confirmed"

            # Mettre à jour l'état de la transaction
            if record.transaction_id.remaining_amount == 0:
                record.transaction_id.state = "matched"
            elif record.transaction_id.matched_amount > 0:
                record.transaction_id.state = "partial"

    def action_reconcile(self):
        """Reconcile the payment with invoice"""
        self.ensure_one()

        if self.state == "reconciled":
            return

        # Réconcilier les écritures comptables
        move_line_transaction = self.transaction_id.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and l.partner_id == self.partner_id
        )

        move_line_invoice = self.invoice_id.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")

        if move_line_transaction and move_line_invoice:
            (move_line_transaction + move_line_invoice).reconcile()

        self.state = "reconciled"

        # Vérifier si l'abonnement doit être réactivé
        if self.subscription_id:
            self.subscription_id.check_payment_status()

    def action_cancel(self):
        """Cancel the matching"""
        for record in self:
            if record.state == "reconciled":
                raise UserError(_("Cannot cancel a reconciled matching"))
            record.state = "cancelled"

            # Recalculer l'état de la transaction
            if record.transaction_id.matched_amount == 0:
                record.transaction_id.state = "unmatched"
            elif record.transaction_id.remaining_amount > 0:
                record.transaction_id.state = "partial"
