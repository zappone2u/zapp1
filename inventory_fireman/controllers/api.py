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


def _event_scopes(event):
    if event.direction != "pull" or not event.payload:
        return []
    try:
        scopes = json.loads(event.payload)
    except ValueError:
        return []
    return [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []


def _event_record(env, event):
    """Nom lisible de l'enregistrement poussé (None s'il n'existe plus ou n'est pas identifiable)."""
    if event.direction != "push" or event.op != "upsert" or not event.res_id or event.model not in env:
        return None
    try:
        return env[event.model].browse(event.res_id).exists().display_name or None
    except Exception:
        return None


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

    @http.route(
        "/fireman/api/v1/templates", type="http", auth="public", methods=["POST"], csrf=False, save_session=False
    )
    def templates(self, **kw):
        """Modèles de véhicules proposés à une UO (partagés + réservés à l'UO), pour ses membres."""
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

        env = request.env(su=True)
        connector = env["firebase.connector"]._get_default()
        if not connector:
            return _json({"error": "not_configured"}, 503)
        try:
            uid = connector.verify_id_token(header[7:])["uid"]
            user = connector.get_user(uid) or {}
        except Exception:
            _logger.info("Jeton Firebase refusé pour /fireman/api/v1/templates", exc_info=True)
            return _json({"error": "invalid_token"}, 401)
        member_of = any(e.get("uo_name") == code for e in as_list(user.get("uo")) if isinstance(e, dict))
        if not (user.get("admin") is True or member_of):
            return _json({"error": "forbidden"}, 403)

        uo = env["fireman.uo"].search([("code", "=", code)], limit=1)
        templates = env["fireman.vehicle.template"]._available_for(uo)
        return _json({"templates": [t._api_payload() for t in templates]})

    @http.route(
        "/fireman/api/v1/app-logs", type="http", auth="public", methods=["POST"], csrf=False, save_session=False
    )
    def app_logs(self, **kw):
        """Rapports d'erreur de l'application. Accepté sans connexion (plantage au démarrage, aucune UO) :
        un jeton Firebase valide, s'il est fourni, identifie seulement l'auteur ; les envois sont plafonnés."""
        raw = request.httprequest.get_data(cache=False)
        if len(raw) > 200_000:
            return _json({"error": "too_large"}, 413)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return _json({"error": "invalid_json"}, 400)
        reports = payload.get("reports") if isinstance(payload, dict) else None
        if not isinstance(reports, list):
            return _json({"error": "invalid_reports"}, 400)

        env = request.env(su=True)
        identity = {}
        header = request.httprequest.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            connector = env["firebase.connector"]._get_default()
            if connector:
                try:
                    claims = connector.verify_id_token(header[7:])
                    identity = {"uid": claims.get("uid"), "email": (claims.get("email") or "").lower()}
                except Exception:
                    _logger.info("Jeton Firebase ignoré pour /fireman/api/v1/app-logs")
        accepted = env["fireman.app.log"]._ingest(reports, ip=request.httprequest.remote_addr or "", identity=identity)
        return _json({"accepted": accepted})

    @http.route("/fireman/portal/log", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def portal_log(self, **kw):
        """Erreurs survenues dans le navigateur sur le portail (connexion, inscription...) : plafonnées par IP.
        L'identité n'est retenue que si une session Odoo est ouverte."""
        raw = request.httprequest.get_data(cache=False)
        if len(raw) > 100_000:
            return _json({"error": "too_large"}, 413)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return _json({"error": "invalid_json"}, 400)
        reports = payload.get("reports") if isinstance(payload, dict) else None
        if not isinstance(reports, list):
            return _json({"error": "invalid_reports"}, 400)
        user = request.env.user
        identity = {} if user._is_public() else {"email": (user.login or "").lower()}
        accepted = request.env["fireman.app.log"]._ingest(
            reports, ip=request.httprequest.remote_addr or "", identity=identity, source="portal"
        )
        return _json({"accepted": accepted})

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
                        "uo": e.uo_code or None,
                        "direction": e.direction,
                        "op": e.op,
                        "model": e.model or None,
                        "target": e.target_key or None,
                        "scopes": _event_scopes(e),
                        "record": _event_record(env, e),
                        "state": e.state,
                        "level": _level(e),
                        "attempts": e.attempts,
                        "error": e.error or None,
                        "created": iso(e.create_date),
                        "processed": iso(e.processed_date),
                    }
                    for e in events
                ],
            }
        )
