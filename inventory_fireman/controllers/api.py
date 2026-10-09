# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, http
from odoo.http import request

from ..models.fireman_uo import is_valid_code
from ..models.utils import as_list

_logger = logging.getLogger(__name__)

# success = terminé ; warning = en nouvelle tentative après une erreur ; failed = abandonné ;
# pending = en attente / en cours sans erreur.
LOG_LEVELS = {
    "success": [("state", "=", "done")],
    "warning": [("state", "in", ("pending", "running")), ("attempts", ">", 0)],
    "failed": [("state", "=", "failed")],
    "pending": [("state", "in", ("pending", "running")), ("attempts", "=", 0)],
}


def _level(event):
    if event.state == "done":
        return "success"
    if event.state == "failed":
        return "failed"
    return "warning" if event.attempts else "pending"


def _json(data, status=200):
    return request.make_json_response(data, status=status)


class FiremanSyncApi(http.Controller):
    """Point d'entrée appelé par l'application dès qu'elle a modifié Firebase.

    L'application ne transmet aucune donnée : elle indique seulement « l'UO X a changé ».
    Odoo relit alors l'état de référence dans Firebase (via la file de synchronisation),
    ce qui rend l'appel idempotent et impossible à utiliser pour injecter des données.
    """

    @http.route("/fireman/api/v1/sync", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def sync(self, **kw):
        header = request.httprequest.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return _json({"error": "missing_token"}, 401)
        try:
            payload = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return _json({"error": "invalid_json"}, 400)

        code = str(payload.get("uo") or "").strip()
        if not is_valid_code(code):
            return _json({"error": "invalid_uo"}, 400)
        scopes = payload.get("scopes")
        if scopes is not None and not isinstance(scopes, list):
            return _json({"error": "invalid_scopes"}, 400)

        env = request.env(su=True)
        connector = env["firebase.connector"]._get_default()
        if not connector:
            return _json({"error": "not_configured"}, 503)
        try:
            uid = connector.verify_id_token(header[7:])["uid"]
            user = connector.get_user(uid) or {}
        except Exception:
            _logger.info("Jeton Firebase refusé pour /fireman/api/v1/sync", exc_info=True)
            return _json({"error": "invalid_token"}, 401)

        member_of = any(e.get("uo_name") == code for e in as_list(user.get("uo")) if isinstance(e, dict))
        if not (user.get("admin") is True or member_of):
            return _json({"error": "forbidden"}, 403)

        env["fireman.sync.event"].enqueue_pull(code, scopes)
        return _json({"status": "queued"})

    @http.route("/fireman/api/v1/logs", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def logs(self, **kw):
        """Journal de synchronisation Odoo ↔ Firebase, réservé aux super administrateurs."""
        header = request.httprequest.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return _json({"error": "missing_token"}, 401)
        try:
            payload = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return _json({"error": "invalid_json"}, 400)

        env = request.env(su=True)
        connector = env["firebase.connector"]._get_default()
        if not connector:
            return _json({"error": "not_configured"}, 503)
        try:
            uid = connector.verify_id_token(header[7:])["uid"]
            user = connector.get_user(uid) or {}
        except Exception:
            _logger.info("Jeton Firebase refusé pour /fireman/api/v1/logs", exc_info=True)
            return _json({"error": "invalid_token"}, 401)
        if user.get("admin") is not True:
            return _json({"error": "forbidden"}, 403)

        code = str(payload.get("uo") or "").strip()
        level = str(payload.get("level") or "all")
        if level not in ("all", *LOG_LEVELS):
            return _json({"error": "invalid_level"}, 400)
        try:
            limit = min(max(int(payload.get("limit", 100)), 1), 300)
            offset = max(int(payload.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return _json({"error": "invalid_paging"}, 400)

        Event = env["fireman.sync.event"]
        base = [("uo_code", "=", code)] if code else []
        counts = {name: Event.search_count(base + domain) for name, domain in LOG_LEVELS.items()}
        events = Event.search(base + (LOG_LEVELS[level] if level != "all" else []), limit=limit, offset=offset)

        def iso(value):
            return value.isoformat() + "Z" if value else None

        return _json(
            {
                "counts": counts,
                "uos": env["fireman.uo"].search([]).mapped("code"),
                "events": [
                    {
                        "id": e.id,
                        "uo": e.uo_code,
                        "direction": e.direction,
                        "op": e.op,
                        "model": e.model,
                        "target": e.target_key,
                        "state": e.state,
                        "level": _level(e),
                        "attempts": e.attempts,
                        "error": e.error,
                        "created": iso(e.create_date),
                        "processed": iso(e.processed_date),
                    }
                    for e in events
                ],
            }
        )
