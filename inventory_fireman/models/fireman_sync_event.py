# -*- coding: utf-8 -*-
import json
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.modules import module as odoo_module
from odoo.exceptions import UserError

from .constants import MAX_PUSH_ATTEMPTS, SCOPES

_logger = logging.getLogger(__name__)

WAKE_KEY = "fireman_wake_cron"


class FiremanSyncEvent(models.Model):
    """File d'attente de synchronisation.

    * push : un enregistrement Odoo a changé et doit être écrit dans Firebase ;
    * pull : l'application a signalé un changement, Odoo relit l'UO dans Firebase.

    Les événements sont traités par un cron déclenché immédiatement à chaque ajout
    (``ir.cron._trigger``) et rejoué toutes les quelques minutes en filet de sécurité.
    """

    _name = "fireman.sync.event"
    _description = "Événement de synchronisation Firebase"
    _order = "id desc"

    direction = fields.Selection([("push", "Odoo → Firebase"), ("pull", "Firebase → Odoo")], required=True, index=True)
    op = fields.Selection([("upsert", "Créer / modifier"), ("delete", "Supprimer")], default="upsert", required=True)
    model = fields.Char()
    res_id = fields.Integer()
    target_key = fields.Char(index=True, help="Clé naturelle de la cible, ex. SIK/<uid du véhicule>")
    uo_code = fields.Char(string="UO", index=True)
    payload = fields.Text()
    state = fields.Selection(
        [("pending", "En attente"), ("running", "En cours"), ("done", "Terminé"), ("failed", "En échec")],
        default="pending",
        required=True,
        index=True,
    )
    attempts = fields.Integer(readonly=True)
    error = fields.Text(readonly=True)
    processed_date = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------
    # Mise en file
    # ------------------------------------------------------------------
    @api.model
    def _wake_cron(self):
        """Demande l'exécution immédiate du cron, une seule fois par transaction."""
        cr = self.env.cr
        if cr.precommit.data.get(WAKE_KEY):
            return
        cr.precommit.data[WAKE_KEY] = True
        env = self.env

        def trigger():
            cron = env.ref("inventory_fireman.ir_cron_fireman_sync", raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

        cr.precommit.add(trigger)

    @api.model
    def _enqueue_push(self, model, res_id, target_key, uo_code, op="upsert", payload=None):
        Event = self.sudo()
        if op == "upsert" and Event.search_count(
            [
                ("direction", "=", "push"),
                ("state", "=", "pending"),
                ("op", "=", "upsert"),
                ("model", "=", model),
                ("target_key", "=", target_key),
            ],
            limit=1,
        ):
            return
        Event.create(
            {
                "direction": "push",
                "op": op,
                "model": model,
                "res_id": res_id,
                "target_key": target_key,
                "uo_code": uo_code,
                "payload": json.dumps(payload) if payload else False,
            }
        )
        self._wake_cron()

    @api.model
    def enqueue_pull(self, code, scopes=None):
        scopes = sorted(set(scopes or SCOPES) & set(SCOPES))
        if not code or not scopes:
            return
        Event = self.sudo()
        pending = Event.search([("direction", "=", "pull"), ("state", "=", "pending"), ("uo_code", "=", code)], limit=1)
        if pending:
            merged = sorted(set(json.loads(pending.payload or "[]")) | set(scopes))
            pending.payload = json.dumps(merged)
        else:
            Event.create({"direction": "pull", "uo_code": code, "target_key": code, "payload": json.dumps(scopes)})
        self._wake_cron()

    @api.model
    def _blocked_targets(self):
        """Cibles dont une modification Odoo n'est pas encore (ou pas) poussée : la lecture
        de Firebase ne doit pas les écraser."""
        events = self.sudo().search_read(
            [("direction", "=", "push"), ("state", "in", ("pending", "running", "failed"))],
            ["model", "target_key"],
        )
        return {(e["model"], e["target_key"]) for e in events}

    # ------------------------------------------------------------------
    # Traitement
    # ------------------------------------------------------------------
    @api.model
    def _cron_process(self):
        self._process_queue()
        self._purge()

    @api.model
    def _cron_full_pull(self):
        self.env["fireman.sync.service"].pull_all()

    @api.model
    def _process_queue(self, limit=200, time_budget=50):
        self = self.sudo()
        now = fields.Datetime.now()
        self.search([("state", "=", "running"), ("write_date", "<", now - timedelta(minutes=10))]).write(
            {"state": "pending"}
        )
        events = self.search([("state", "=", "pending")], order="id", limit=limit)
        if not events:
            return 0
        connector = self.env["firebase.connector"]._get_default()
        started = time.monotonic()
        done = 0
        for event in events:
            if time.monotonic() - started > time_budget:
                self._wake_cron()
                break
            event.write({"state": "running"})
            self._commit()
            try:
                with self.env.cr.savepoint():
                    event._run(connector)
            except Exception as exc:
                attempts = event.attempts + 1
                _logger.warning("Synchronisation Firebase #%s (%s) en échec : %s", event.id, event.target_key, exc)
                event.write(
                    {
                        "state": "failed" if attempts >= MAX_PUSH_ATTEMPTS else "pending",
                        "attempts": attempts,
                        "error": str(exc)[:2000],
                    }
                )
            else:
                done += 1
                event.write({"state": "done", "processed_date": fields.Datetime.now(), "error": False})
            self._commit()
        return done

    def _commit(self):
        """Valide après chaque événement : ce qui est déjà écrit dans Firebase ne doit pas être rejoué."""
        if not getattr(odoo_module, "current_test", None):
            self.env.cr.commit()

    def _run(self, connector):
        self.ensure_one()
        if not connector:
            raise UserError(_("Aucun connecteur Firebase configuré."))
        if self.direction == "pull":
            scopes = json.loads(self.payload or "[]") or None
            self.env["fireman.sync.service"].pull_uo(self.uo_code, scopes, connector=connector)
            return
        Model = self.env[self.model]
        if self.op == "delete":
            Model._push_delete(connector, json.loads(self.payload or "{}"))
        else:
            record = Model.browse(self.res_id).exists()
            if record:
                record._push_upsert(connector)

    @api.model
    def _purge(self):
        now = fields.Datetime.now()
        self.sudo().search(
            [
                "|",
                "&",
                ("state", "=", "done"),
                ("processed_date", "<", now - timedelta(days=7)),
                "&",
                ("state", "=", "failed"),
                ("write_date", "<", now - timedelta(days=60)),
            ]
        ).unlink()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry(self):
        self.filtered(lambda e: e.state in ("failed", "done")).write(
            {"state": "pending", "attempts": 0, "error": False}
        )
        self._wake_cron()

    def action_process_now(self):
        self._process_queue()
        return {"type": "ir.actions.client", "tag": "reload"}
