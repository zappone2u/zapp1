# -*- coding: utf-8 -*-
import hashlib
import re
from datetime import timedelta

from odoo import api, fields, models

from .fireman_uo import is_valid_code

KINDS = [
    ("startup_crash", "Fermeture au démarrage"),
    ("flutter_error", "Erreur d'affichage"),
    ("async_error", "Erreur non interceptée"),
    ("auth", "Connexion"),
    ("sync", "Synchronisation"),
    ("network", "Réseau"),
    ("version", "Version"),
    ("other", "Autre"),
]
LEVELS = [("info", "Info"), ("warning", "Avertissement"), ("error", "Erreur")]

MAX_REPORTS_PER_REQUEST = 20
MAX_PER_DEVICE_HOUR = 60
MAX_PER_IP_HOUR = 600
RETENTION_DAYS = 90

_LIMITS = {
    "message": 2000,
    "stack": 20000,
    "context": 4000,
    "step": 60,
    "platform": 30,
    "os_version": 200,
    "device_model": 100,
    "app_version": 30,
    "build": 30,
    "device_id": 64,
}
_NOISE = re.compile(r"0x[0-9a-fA-F]+|\d+")


class FiremanAppLog(models.Model):
    """Rapports envoyés par l'application (plantages, erreurs, échecs de connexion...).

    Indépendants de toute UO : ils arrivent même quand l'utilisateur n'est pas (ou plus) connecté.
    """

    _name = "fireman.app.log"
    _description = "Journal de l'application mobile"
    _order = "id desc"

    kind = fields.Selection(KINDS, required=True, default="other", index=True)
    level = fields.Selection(LEVELS, required=True, default="error", index=True)
    message = fields.Char()
    stack = fields.Text()
    context = fields.Text()
    fingerprint = fields.Char(index=True, help="Regroupe les rapports d'une même erreur.")
    resolved = fields.Boolean(index=True)
    event_date = fields.Datetime(string="Survenu le", help="Heure sur l'appareil (UTC).")
    step = fields.Char(string="Dernière étape", help="Dernière étape de démarrage atteinte.")
    device_id = fields.Char(index=True)
    platform = fields.Char()
    os_version = fields.Char(string="Système")
    device_model = fields.Char(string="Appareil")
    app_version = fields.Char(string="Version", index=True)
    build = fields.Char()
    user_email = fields.Char(string="Email")
    firebase_uid = fields.Char()
    verified = fields.Boolean(string="Identité vérifiée")
    uo_code = fields.Char(string="UO", index=True)
    ip_hash = fields.Char(index=True)

    @api.model
    def _clean(self, value, limit):
        return str(value or "").replace("\x00", "")[:limit]

    @api.model
    def _fingerprint(self, kind, message, stack):
        first_frame = next((line for line in (stack or "").splitlines() if line.strip()), "")
        raw = "|".join((kind, _NOISE.sub("#", message or ""), _NOISE.sub("#", first_frame)))
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @api.model
    def _ingest(self, reports, ip="", identity=None):
        """Enregistre un lot de rapports ; retourne le nombre accepté (anti-abus : plafonds par appareil et par IP)."""
        identity = identity or {}
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else False
        since = fields.Datetime.now() - timedelta(hours=1)
        env = self.sudo()
        ip_room = (
            MAX_PER_IP_HOUR - env.search_count([("ip_hash", "=", ip_hash), ("create_date", ">=", since)])
            if ip_hash
            else MAX_PER_IP_HOUR
        )
        room_by_device = {}
        kinds = dict(KINDS)
        levels = dict(LEVELS)
        vals_list = []
        for raw in list(reports or [])[:MAX_REPORTS_PER_REQUEST]:
            if not isinstance(raw, dict) or ip_room <= 0:
                continue
            vals = {f: self._clean(raw.get(f), n) for f, n in _LIMITS.items()}
            device = vals["device_id"]
            if device not in room_by_device:
                room_by_device[device] = MAX_PER_DEVICE_HOUR - (
                    env.search_count([("device_id", "=", device), ("create_date", ">=", since)]) if device else 0
                )
            if room_by_device[device] <= 0:
                continue
            room_by_device[device] -= 1
            ip_room -= 1
            kind = raw.get("kind") if raw.get("kind") in kinds else "other"
            code = self._clean(raw.get("uo"), 10).strip().upper()
            event_date = False
            try:
                event_date = fields.Datetime.to_datetime(str(raw.get("date") or "")[:19].replace("T", " "))
            except (ValueError, TypeError):
                pass
            vals.update(
                kind=kind,
                level=raw.get("level") if raw.get("level") in levels else "error",
                uo_code=code if is_valid_code(code) else False,
                event_date=event_date,
                fingerprint=self._fingerprint(kind, vals["message"], vals["stack"]),
                ip_hash=ip_hash,
                user_email=identity.get("email") or False,
                firebase_uid=identity.get("uid") or False,
                verified=bool(identity),
            )
            vals_list.append(vals)
        if vals_list:
            env.create(vals_list)
        return len(vals_list)

    @api.autovacuum
    def _gc_old_logs(self):
        limit = fields.Datetime.now() - timedelta(days=RETENTION_DAYS)
        self.sudo().search([("create_date", "<", limit)]).unlink()
