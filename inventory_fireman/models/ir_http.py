# -*- coding: utf-8 -*-
import traceback

from werkzeug.exceptions import HTTPException

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError, UserError
from odoo.http import SessionExpiredException, request

PORTAL_PREFIXES = ("/my", "/firebase", "/login/firebase", "/fireman/portal")


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _handle_error(cls, exception):
        cls._log_portal_exception(exception)
        return super()._handle_error(exception)

    @classmethod
    def _log_portal_exception(cls, exception):
        """Exceptions inattendues sur les pages du portail, journalisées hors de la transaction annulée."""
        expected = (HTTPException, UserError, AccessError, SessionExpiredException)
        if isinstance(exception, expected) or not request.httprequest.path.startswith(PORTAL_PREFIXES):
            return
        try:
            login = (request.session.login or "").lower()
            identity = {"email": login} if request.session.uid and login else {}
            with request.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["fireman.app.log"]._log_portal(
                    "portal_server",
                    f"{type(exception).__name__}: {exception}",
                    stack="".join(traceback.format_exception(exception)),
                    context={"method": request.httprequest.method},
                    identity=identity,
                    step=request.httprequest.path[:60],
                )
        except Exception:
            pass
