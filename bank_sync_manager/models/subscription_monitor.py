# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)


class SubscriptionMonitor(models.Model):
    _name = "subscription.monitor"
    _description = "Subscription Payment Monitor"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "priority desc, next_check_date"

    name = fields.Char(string="Monitor Name", compute="_compute_name", store=True)
    subscription_id = fields.Many2one("sale.order", string="Subscription", required=True, ondelete="cascade")
    partner_id = fields.Many2one("res.partner", string="Customer", related="subscription_id.partner_id", store=True)

    # Payment Status
    payment_status = fields.Selection(
        [
            ("ok", "Payment OK"),
            ("warning", "Warning"),
            ("late", "Late Payment"),
            ("critical", "Critical"),
            ("suspended", "Suspended"),
        ],
        string="Payment Status",
        default="ok",
        tracking=True,
    )

    # Monitoring
    last_invoice_id = fields.Many2one("account.move", string="Last Invoice")
    last_invoice_date = fields.Date(string="Last Invoice Date", related="last_invoice_id.invoice_date")
    last_payment_date = fields.Date(string="Last Payment Date")
    days_overdue = fields.Integer(string="Days Overdue", compute="_compute_days_overdue", store=True)

    total_due = fields.Monetary(string="Total Due", compute="_compute_total_due", store=True)
    currency_id = fields.Many2one("res.currency", string="Currency", related="subscription_id.currency_id")

    # Actions
    warning_sent = fields.Boolean(string="Warning Sent", default=False)
    warning_date = fields.Date(string="Warning Date")
    suspension_date = fields.Date(string="Suspension Date")

    # Configuration
    grace_period = fields.Integer(string="Grace Period (days)", default=7, help="Days before warning after due date")
    critical_period = fields.Integer(
        string="Critical Period (days)", default=15, help="Days before suspension after due date"
    )
    auto_suspend = fields.Boolean(
        string="Auto Suspend", default=True, help="Automatically suspend after critical period"
    )

    # Priority
    priority = fields.Selection(
        [
            ("0", "Low"),
            ("1", "Normal"),
            ("2", "High"),
            ("3", "Critical"),
        ],
        string="Priority",
        default="1",
        compute="_compute_priority",
        store=True,
    )

    next_check_date = fields.Date(string="Next Check Date", compute="_compute_next_check_date", store=True)

    active = fields.Boolean(default=True)
    notes = fields.Text(string="Notes")

    @api.depends("subscription_id", "partner_id")
    def _compute_name(self):
        for record in self:
            record.name = f"{record.partner_id.name} - {record.subscription_id.name}"

    @api.depends("last_invoice_date", "last_payment_date")
    def _compute_days_overdue(self):
        for record in self:
            if record.last_invoice_id and record.last_invoice_id.invoice_date_due:
                due_date = record.last_invoice_id.invoice_date_due
                if record.last_invoice_id.payment_state != "paid":
                    delta = fields.Date.today() - due_date
                    record.days_overdue = delta.days if delta.days > 0 else 0
                else:
                    record.days_overdue = 0
            else:
                record.days_overdue = 0

    @api.depends("subscription_id")
    def _compute_total_due(self):
        for record in self:
            unpaid_invoices = self.env["account.move"].search(
                [
                    ("partner_id", "=", record.partner_id.id),
                    ("subscription_id", "=", record.subscription_id.id),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ["not_paid", "partial"]),
                ]
            )
            record.total_due = sum(unpaid_invoices.mapped("amount_residual"))

    @api.depends("days_overdue", "total_due", "payment_status")
    def _compute_priority(self):
        for record in self:
            if record.payment_status == "critical" or record.days_overdue >= record.critical_period:
                record.priority = "3"
            elif record.payment_status == "late" or record.days_overdue >= record.grace_period:
                record.priority = "2"
            elif record.payment_status == "warning" or record.total_due > 0:
                record.priority = "1"
            else:
                record.priority = "0"

    @api.depends("payment_status", "last_invoice_date")
    def _compute_next_check_date(self):
        for record in self:
            if record.payment_status in ["critical", "late"]:
                record.next_check_date = fields.Date.today()
            elif record.payment_status == "warning":
                record.next_check_date = fields.Date.today() + timedelta(days=1)
            else:
                record.next_check_date = fields.Date.today() + timedelta(days=7)

    def action_check_payment_status(self):
        """Check payment status and take action"""
        self.ensure_one()

        # Récupérer la dernière facture
        last_invoice = self.env["account.move"].search(
            [
                ("partner_id", "=", self.partner_id.id),
                ("subscription_id", "=", self.subscription_id.id),
                ("state", "=", "posted"),
                ("move_type", "=", "out_invoice"),
            ],
            order="invoice_date desc",
            limit=1,
        )

        self.last_invoice_id = last_invoice.id if last_invoice else False

        # Vérifier le statut de paiement
        if last_invoice and last_invoice.payment_state == "paid":
            self.payment_status = "ok"
            self.warning_sent = False
            self.last_payment_date = fields.Date.today()

            # Réactiver l'abonnement si suspendu
            if self.subscription_id.stage_id.category == "closed":
                self.action_reactivate_subscription()

        elif self.days_overdue >= self.critical_period:
            self.payment_status = "critical"
            if self.auto_suspend and self.subscription_id.stage_id.category != "closed":
                self.action_suspend_subscription()

        elif self.days_overdue >= self.grace_period:
            self.payment_status = "late"
            if not self.warning_sent:
                self.action_send_warning()

        elif self.total_due > 0:
            self.payment_status = "warning"

        else:
            self.payment_status = "ok"
            self.warning_sent = False

    def action_send_warning(self):
        """Send warning email to customer"""
        self.ensure_one()

        template = self.env.ref("bank_sync_manager.email_template_payment_warning", raise_if_not_found=False)

        if template:
            template.send_mail(self.id, force_send=True)

        self.warning_sent = True
        self.warning_date = fields.Date.today()

        # Créer une activité
        self.activity_schedule(
            "mail.mail_activity_data_warning",
            summary=_("Payment Warning Sent"),
            note=_(f"Warning sent to {self.partner_id.name} for overdue payment"),
            user_id=self.subscription_id.user_id.id or self.env.user.id,
        )

        _logger.info(f"Payment warning sent for subscription {self.subscription_id.name}")

    def action_suspend_subscription(self):
        """Suspend the subscription"""
        self.ensure_one()

        # Chercher un stage "Suspendu" ou créer
        suspended_stage = self.env["sale.subscription.stage"].search(
            [("name", "ilike", "suspendu"), ("category", "=", "closed")], limit=1
        )

        if not suspended_stage:
            suspended_stage = self.env["sale.subscription.stage"].search([("category", "=", "closed")], limit=1)

        if suspended_stage:
            self.subscription_id.stage_id = suspended_stage.id

        self.payment_status = "suspended"
        self.suspension_date = fields.Date.today()

        # Envoyer notification
        self.subscription_id.message_post(
            body=_(f"Subscription suspended due to non-payment ({self.days_overdue} days overdue)"),
            subject=_("Subscription Suspended"),
        )

        # Créer une activité urgente
        self.activity_schedule(
            "mail.mail_activity_data_warning",
            summary=_("URGENT: Subscription Suspended"),
            note=_(f"Subscription suspended for {self.partner_id.name} - {self.days_overdue} days overdue"),
            user_id=self.subscription_id.user_id.id or self.env.user.id,
        )

        _logger.warning(f"Subscription {self.subscription_id.name} suspended for non-payment")

    def action_reactivate_subscription(self):
        """Reactivate the subscription after payment"""
        self.ensure_one()

        # Chercher un stage actif
        active_stage = self.env["sale.subscription.stage"].search([("category", "=", "progress")], limit=1)

        if active_stage:
            self.subscription_id.stage_id = active_stage.id

        self.payment_status = "ok"
        self.suspension_date = False
        self.warning_sent = False

        # Notification
        self.subscription_id.message_post(
            body=_("Subscription reactivated - Payment received"),
            subject=_("Subscription Reactivated"),
        )

        _logger.info(f"Subscription {self.subscription_id.name} reactivated after payment")

    @api.model
    def cron_check_all_subscriptions(self):
        """Cron job to check all active subscriptions"""
        monitors = self.search(
            [
                ("active", "=", True),
                ("next_check_date", "<=", fields.Date.today()),
            ]
        )

        for monitor in monitors:
            try:
                monitor.action_check_payment_status()
            except Exception as e:
                _logger.error(f"Error checking subscription {monitor.subscription_id.name}: {e}")

    @api.model
    def create_monitors_for_new_subscriptions(self):
        """Create monitors for subscriptions without one"""
        subscriptions = self.env["sale.order"].search([("stage_id.category", "=", "progress")])

        for subscription in subscriptions:
            existing = self.search([("subscription_id", "=", subscription.id)], limit=1)
            if not existing:
                self.create(
                    {
                        "subscription_id": subscription.id,
                    }
                )
