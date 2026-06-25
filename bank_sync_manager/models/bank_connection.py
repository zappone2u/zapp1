# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import requests
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class BankConnection(models.Model):
    _name = "bank.connection"
    _description = "Bank Connection Configuration"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(string="Connection Name", required=True, tracking=True)
    bank_name = fields.Char(string="Bank Name", required=True)
    connection_type = fields.Selection(
        [
            ("saltedge", "Salt Edge"),
            ("csv", "CSV Import"),
            ("psd2", "PSD2 Standard"),
            ("custom", "Custom Integration"),
        ],
        string="Connection Type",
        required=True,
        default="saltedge",
        tracking=True,
    )

    # Salt Edge Configuration
    saltedge_app_id = fields.Char(string="Salt Edge App ID")
    saltedge_secret = fields.Char(string="Salt Edge Secret")
    saltedge_customer_id = fields.Char(string="Customer ID", readonly=True, help="Salt Edge Customer ID")
    saltedge_connection_id = fields.Char(string="Connection ID", readonly=True, help="Salt Edge Connection ID")

    # Generic API Configuration (for other types)
    api_url = fields.Char(string="API URL", default="https://www.saltedge.com/api/v6")
    api_key = fields.Char(string="API Key")
    api_secret = fields.Char(string="API Secret")
    client_id = fields.Char(string="Client ID")
    client_secret = fields.Char(string="Client Secret")

    # Bank Account Info
    iban = fields.Char(string="IBAN")
    account_number = fields.Char(string="Account Number")
    journal_id = fields.Many2one("account.journal", string="Journal", domain=[("type", "=", "bank")], required=True)

    # Sync Configuration
    auto_sync = fields.Boolean(string="Auto Synchronization", default=True)
    sync_frequency = fields.Selection(
        [
            ("hourly", "Hourly"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
        ],
        string="Sync Frequency",
        default="daily",
    )
    last_sync_date = fields.Datetime(string="Last Sync Date", readonly=True)
    next_sync_date = fields.Datetime(string="Next Sync Date", compute="_compute_next_sync_date")

    # Status
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("connected", "Connected"),
            ("error", "Error"),
            ("disconnected", "Disconnected"),
        ],
        string="Status",
        default="draft",
        tracking=True,
    )

    error_message = fields.Text(string="Error Message", readonly=True)
    active = fields.Boolean(default=True)

    # Statistics
    transaction_count = fields.Integer(string="Transactions", compute="_compute_statistics")
    matched_count = fields.Integer(string="Matched", compute="_compute_statistics")
    unmatched_count = fields.Integer(string="Unmatched", compute="_compute_statistics")

    @api.depends("last_sync_date", "sync_frequency")
    def _compute_next_sync_date(self):
        for record in self:
            if record.last_sync_date and record.auto_sync:
                if record.sync_frequency == "hourly":
                    record.next_sync_date = record.last_sync_date + timedelta(hours=1)
                elif record.sync_frequency == "daily":
                    record.next_sync_date = record.last_sync_date + timedelta(days=1)
                elif record.sync_frequency == "weekly":
                    record.next_sync_date = record.last_sync_date + timedelta(weeks=1)
            else:
                record.next_sync_date = False

    def _compute_statistics(self):
        for record in self:
            transactions = self.env["bank.transaction"].search([("connection_id", "=", record.id)])
            record.transaction_count = len(transactions)
            record.matched_count = len(transactions.filtered(lambda t: t.state == "matched"))
            record.unmatched_count = len(transactions.filtered(lambda t: t.state == "unmatched"))

    def action_test_connection(self):
        """Test the bank connection"""
        self.ensure_one()
        try:
            if self.connection_type == "saltedge":
                # Test Salt Edge connection
                if not self.saltedge_app_id or not self.saltedge_secret:
                    raise ValidationError(_("Salt Edge App ID and Secret are required"))

                # Create or get customer
                customer_id = self._saltedge_get_or_create_customer()
                self.saltedge_customer_id = customer_id

                self.state = "connected"
                self.error_message = False
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Success"),
                        "message": _("Salt Edge connection successful! Customer ID: %s") % customer_id,
                        "type": "success",
                        "sticky": False,
                    },
                }
            elif self.connection_type == "psd2":
                # TODO: Implémenter PSD2
                raise UserError(_("PSD2 connection not yet implemented"))
            else:
                raise UserError(_("Connection type not supported"))

        except Exception as e:
            self.state = "error"
            self.error_message = str(e)
            _logger.error(f"Bank connection test failed: {e}")
            raise UserError(_("Connection failed: %s") % str(e))

    def action_sync_transactions(self):
        """Synchronize bank transactions"""
        self.ensure_one()

        if self.state != "connected":
            raise UserError(_("Please test and establish the connection first"))

        try:
            # Récupérer les transactions depuis la dernière sync
            transactions_data = self._fetch_transactions()

            # Créer les transactions dans Odoo
            created_count = 0
            for trans_data in transactions_data:
                existing = self.env["bank.transaction"].search(
                    [("reference", "=", trans_data.get("reference")), ("connection_id", "=", self.id)], limit=1
                )

                if not existing:
                    self.env["bank.transaction"].create(
                        {
                            "connection_id": self.id,
                            "reference": trans_data.get("reference"),
                            "date": trans_data.get("date"),
                            "amount": trans_data.get("amount"),
                            "description": trans_data.get("description"),
                            "partner_name": trans_data.get("partner_name"),
                            "partner_account": trans_data.get("partner_account"),
                        }
                    )
                    created_count += 1

            self.last_sync_date = fields.Datetime.now()

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Sync Complete"),
                    "message": _("%s new transactions imported") % created_count,
                    "type": "success",
                    "sticky": False,
                },
            }

        except Exception as e:
            _logger.error(f"Transaction sync failed: {e}")
            raise UserError(_("Sync failed: %s") % str(e))

    def _fetch_transactions(self):
        """Fetch transactions from bank API"""
        self.ensure_one()

        if self.connection_type == "saltedge":
            return self._saltedge_fetch_transactions()

        return []

    def _saltedge_get_headers(self):
        """Generate Salt Edge API headers"""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "App-id": self.saltedge_app_id,
            "Secret": self.saltedge_secret,
        }

    def _saltedge_get_or_create_customer(self):
        """Create or get Salt Edge customer"""
        headers = self._saltedge_get_headers()
        api_url = self.api_url or "https://www.saltedge.com/api/v6"

        # Check if customer already exists
        if self.saltedge_customer_id:
            try:
                response = requests.get(f"{api_url}/customers/{self.saltedge_customer_id}", headers=headers)
                if response.status_code == 200:
                    return self.saltedge_customer_id
            except Exception as exc:
                _logger.warning(f"Exception checking Salt Edge customer: {exc}")

        # Create new customer
        customer_data = {"data": {"identifier": f"odoo_{self.env.company.id}_{self.id}"}}
        response = requests.post(f"{api_url}/customers", headers=headers, json=customer_data)

        if response.status_code in [200, 201]:
            resp_json = response.json()
            data = resp_json.get("data", {})
            customer_id = data.get("id") or data.get("customer_id")
            if customer_id:
                return customer_id
            _logger.error(f"Salt Edge customer creation response missing 'id'/'customer_id': {resp_json}")
            raise ValidationError(
                _("Salt Edge customer creation failed: missing 'id' or 'customer_id' in response. Full response: %s")
                % response.text
            )
        else:
            resp_json = response.json()
            error_class = resp_json.get("error", {}).get("class")
            if error_class == "DuplicatedCustomer":
                identifier = customer_data["data"]["identifier"]
                get_resp = requests.get(f"{api_url}/customers?identifier={identifier}", headers=headers)
                if get_resp.status_code == 200:
                    data = get_resp.json().get("data", {})
                    customer_id = data.get("id") or data.get("customer_id")
                    if customer_id:
                        return customer_id
                    _logger.error(f"Salt Edge fetch existing customer missing 'id'/'customer_id': {get_resp.json()}")
                    raise ValidationError(
                        _("Salt Edge fetch existing customer failed: missing 'id' or 'customer_id' in response. Full response: %s")
                        % get_resp.text
                    )
                else:
                    _logger.error(f"Salt Edge fetch existing customer failed: {get_resp.text}")
                    raise ValidationError(_("Salt Edge fetch existing customer failed: %s") % get_resp.text)
            _logger.error(f"Failed to create Salt Edge customer: {response.text}")
            raise ValidationError(_("Failed to create Salt Edge customer: %s") % response.text)

    def action_saltedge_connect_bank(self):
        """Generate Salt Edge connect URL to link bank account"""
        self.ensure_one()

        if not self.saltedge_customer_id:
            self._saltedge_get_or_create_customer()

        headers = self._saltedge_get_headers()
        api_url = self.api_url or "https://www.saltedge.com/api/v6"

        # Create connect session
        connect_data = {
            "data": {
                "customer_id": self.saltedge_customer_id,
                "consent": {
                    "scopes": ["account_details", "transactions_details"],
                    "from_date": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
                },
                "attempt": {
                    "return_to": f"{self.env['ir.config_parameter'].sudo().get_param('web.base.url')}/bank_sync/saltedge/callback"
                },
            }
        }

        response = requests.post(f"{api_url}/connect_sessions/create", headers=headers, json=connect_data)

        if response.status_code in [200, 201]:
            connect_url = response.json()["data"]["connect_url"]
            return {
                "type": "ir.actions.act_url",
                "url": connect_url,
                "target": "new",
            }
        else:
            raise UserError(_("Failed to create connect session: %s") % response.text)

    def _saltedge_fetch_transactions(self):
        """Fetch transactions from Salt Edge"""
        self.ensure_one()

        if not self.saltedge_customer_id or not self.saltedge_connection_id:
            _logger.warning("No Salt Edge customer or connection ID configured")
            return []

        headers = self._saltedge_get_headers()
        api_url = self.api_url or "https://www.saltedge.com/api/v6"

        # Get all accounts for this connection
        response = requests.get(
            f"{api_url}/accounts", headers=headers, params={"connection_id": self.saltedge_connection_id}
        )

        if response.status_code != 200:
            raise UserError(_("Failed to fetch accounts: %s") % response.text)

        accounts = response.json().get("data", [])
        all_transactions = []

        # Fetch transactions for each account
        for account in accounts:
            account_id = account["id"]

            # Date range
            from_date = (self.last_sync_date or (datetime.now() - timedelta(days=30))).strftime("%Y-%m-%d")

            tx_response = requests.get(
                f"{api_url}/transactions",
                headers=headers,
                params={"connection_id": self.saltedge_connection_id, "account_id": account_id, "from_date": from_date},
            )

            if tx_response.status_code == 200:
                transactions = tx_response.json().get("data", [])

                for tx in transactions:
                    all_transactions.append(
                        {
                            "reference": tx["id"],
                            "date": tx["made_on"],
                            "amount": float(tx["amount"]),
                            "description": tx.get("description", ""),
                            "partner_name": tx.get("extra", {}).get("payee") or tx.get("extra", {}).get("payer"),
                            "partner_account": "",
                        }
                    )

        return all_transactions

    @api.model
    def cron_sync_all_connections(self):
        """Cron job to sync all active connections"""
        connections = self.search([("auto_sync", "=", True), ("state", "=", "connected"), ("active", "=", True)])

        for connection in connections:
            if not connection.next_sync_date or connection.next_sync_date <= fields.Datetime.now():
                try:
                    connection.action_sync_transactions()
                    _logger.info(f"Successfully synced connection: {connection.name}")
                except Exception as e:
                    _logger.error(f"Failed to sync connection {connection.name}: {e}")
