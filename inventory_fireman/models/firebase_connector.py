# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .utils import as_list, to_naive_utc

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import auth, credentials, db, firestore
except ImportError:  # pragma: no cover
    firebase_admin = None
    _logger.warning("firebase-admin n'est pas installé : pip install firebase-admin")


def _clean(value):
    """Convertit les types Firestore (datetime avec nanosecondes…) en types simples."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if hasattr(value, "isoformat"):
        dt = to_naive_utc(value)
        return dt.isoformat() if dt else None
    return value


class FirebaseConnector(models.Model):
    """Accès Firebase (Realtime Database + Firestore + Auth) via le SDK Admin."""

    _name = "firebase.connector"
    _description = "Connecteur Firebase"

    name = fields.Char(string="Nom", required=True, default="Firebase")
    database_url = fields.Char(string="URL Realtime Database", required=True)
    service_account_json = fields.Text(string="Service account (JSON)")
    service_account_path = fields.Char(string="Service account (chemin du fichier)")

    is_connected = fields.Boolean(string="Connecté", readonly=True)
    last_connection_test = fields.Datetime(string="Dernier test", readonly=True)
    connection_error = fields.Text(string="Dernière erreur", readonly=True)
    last_full_pull = fields.Datetime(string="Dernière synchronisation complète", readonly=True)

    pending_event_count = fields.Integer(compute="_compute_event_counts")
    failed_event_count = fields.Integer(compute="_compute_event_counts")

    _sql_constraints = [("name_unique", "unique(name)", "Le nom du connecteur doit être unique.")]

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _compute_event_counts(self):
        Event = self.env["fireman.sync.event"]
        pending = Event.search_count([("state", "in", ("pending", "running"))])
        failed = Event.search_count([("state", "=", "failed")])
        for connector in self:
            connector.pending_event_count = pending
            connector.failed_event_count = failed

    def write(self, vals):
        res = super().write(vals)
        if {"name", "database_url", "service_account_json", "service_account_path"} & set(vals):
            self._drop_app()
        return res

    def _drop_app(self):
        if not firebase_admin:
            return
        for connector in self:
            try:
                firebase_admin.delete_app(firebase_admin.get_app(connector.name))
            except ValueError:
                pass

    @api.model
    def _get_default(self):
        return self.sudo().search([], limit=1)

    def _app(self):
        self.ensure_one()
        if not firebase_admin:
            raise UserError(_("Le module Python firebase-admin n'est pas installé."))
        try:
            return firebase_admin.get_app(self.name)
        except ValueError:
            pass
        if self.service_account_json:
            try:
                cred = credentials.Certificate(json.loads(self.service_account_json))
            except (ValueError, json.JSONDecodeError) as exc:
                raise UserError(_("Le JSON du service account est invalide : %s", exc))
        elif self.service_account_path:
            cred = credentials.Certificate(self.service_account_path)
        else:
            raise UserError(_("Renseignez le JSON ou le chemin du service account."))
        return firebase_admin.initialize_app(cred, {"databaseURL": self.database_url}, name=self.name)

    # ------------------------------------------------------------------
    # Accès Firebase
    # ------------------------------------------------------------------
    def rtdb(self, path="/"):
        self.ensure_one()
        return db.reference(path, app=self._app())

    def firestore_client(self):
        self.ensure_one()
        return firestore.client(app=self._app())

    def verify_id_token(self, token):
        """Valide un jeton Firebase ID et renvoie ses claims (uid, email…)."""
        self.ensure_one()
        return auth.verify_id_token(token, app=self._app())

    def iter_users(self):
        """(uid, données) de tous les documents Firestore `users`."""
        self.ensure_one()
        for doc in self.firestore_client().collection("users").stream():
            yield doc.id, _clean(doc.to_dict() or {})

    def get_user(self, uid):
        self.ensure_one()
        snap = self.firestore_client().collection("users").document(uid).get()
        return _clean(snap.to_dict() or {}) if snap.exists else None

    def find_user_by_email(self, email):
        """(uid, données Firestore) du compte Firebase portant cet email, sinon None."""
        self.ensure_one()
        try:
            record = auth.get_user_by_email(email.strip(), app=self._app())
        except auth.UserNotFoundError:
            return None
        return record.uid, (self.get_user(record.uid) or {"email": record.email})

    def set_user_uo_entry(self, uid, code, entry):
        """Remplace (entry = dict) ou retire (entry = None) l'UO `code` dans `users/{uid}.uo`.

        La lecture et l'écriture sont atomiques : l'application modifie le même tableau.
        """
        self.ensure_one()
        client = self.firestore_client()
        ref = client.collection("users").document(uid)

        @firestore.transactional
        def run(transaction):
            data = ref.get(transaction=transaction).to_dict() or {}
            result, placed = [], False
            for current in as_list(data.get("uo")):
                if not isinstance(current, dict):
                    continue
                if current.get("uo_name") != code:
                    result.append(current)
                elif entry is not None:
                    result.append({**current, **entry})
                    placed = True
            if entry is not None and not placed:
                result.append({"last_connection": datetime.now(timezone.utc), **entry})
            transaction.set(ref, {"uo": result}, merge=True)

        run(client.transaction())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            self.rtdb("/").get(shallow=True)
            self.write({"is_connected": True, "connection_error": False, "last_connection_test": fields.Datetime.now()})
        except Exception as exc:
            self.write(
                {"is_connected": False, "connection_error": str(exc), "last_connection_test": fields.Datetime.now()}
            )
            raise UserError(_("Connexion à Firebase impossible : %s", exc))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Firebase"), "message": _("Connexion réussie."), "type": "success"},
        }

    def action_full_pull(self):
        self.ensure_one()
        stats = self.env["fireman.sync.service"].pull_all()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Synchronisation Firebase → Odoo"),
                "message": _(
                    "%(uo)s UO synchronisée(s), %(ignored)s ignorée(s) (trigramme invalide), %(errors)s en erreur.",
                    uo=stats["uo"],
                    ignored=stats["ignored"],
                    errors=stats["errors"],
                ),
                "type": "warning" if stats["errors"] else "success",
            },
        }

    def action_process_queue(self):
        self.env["fireman.sync.event"]._process_queue()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_open_events(self):
        return self.env["ir.actions.act_window"]._for_xml_id("inventory_fireman.action_fireman_sync_event")
