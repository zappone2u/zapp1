# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, http
from odoo.http import request

from ..models.utils import as_list

_logger = logging.getLogger(__name__)

CODE_FORBIDDEN = set(".$#[]/ ")


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
        if not code or len(code) > 32 or CODE_FORBIDDEN & set(code):
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
