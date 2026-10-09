# -*- coding: utf-8 -*-
import logging

from markupsafe import Markup, escape

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

CONTACT_CATEGORIES = {
    "support_technique": "Support technique",
    "facturation": "Facturation / Abonnement",
    "compte": "Compte utilisateur",
    "application": "Application mobile",
    "fonctionnalite": "Demande de fonctionnalité",
    "autre": "Autre",
}


class FiremanWebsite(http.Controller):
    """Pages publiques : accueil, tarifs, contact, connexion Firebase."""

    @http.route(["/", "/home", "/firebase/home"], type="http", auth="public", website=True, sitemap=True)
    def home(self, **kw):
        return request.render("inventory_fireman.firebase_home_page_redesign", {})

    @http.route(["/policies", "/terms"], type="http", auth="public", website=True, sitemap=True)
    def policies(self, **kw):
        return request.render("inventory_fireman.portal_policies", {})

    @http.route(["/pricing", "/my/pricing", "/abonnement"], type="http", auth="public", website=True, sitemap=False)
    def pricing(self, **kw):
        products = request.env["product.template"].sudo().search([("fireman_tier", "!=", False)], order="list_price")
        return request.render("inventory_fireman.firebase_subscription_info", {"tier_products": products})

    @http.route(["/my/firebase/register"], type="http", auth="public", website=True, sitemap=False)
    def register(self, **kw):
        return request.render("inventory_fireman.firebase_register_page", {})

    @http.route(
        ["/my/firebase/login", "/firebase/login", "/login/firebase"],
        type="http",
        auth="public",
        website=True,
        sitemap=False,
    )
    def login(self, **kw):
        if not request.env.user._is_public():
            return request.redirect("/my/uo")
        return request.render("inventory_fireman.firebase_login_page", {})

    # ------------------------------------------------------------------
    # Connexion : le navigateur s'authentifie auprès de Firebase puis envoie son jeton ID
    # ------------------------------------------------------------------
    @http.route("/my/firebase/auth", type="json", auth="public", methods=["POST"])
    def authenticate(self, id_token=None, **kw):
        if not id_token:
            return {"success": False, "error": "Jeton manquant"}
        env = request.env(su=True)
        connector = env["firebase.connector"]._get_default()
        if not connector:
            return {"success": False, "error": "Service indisponible"}
        try:
            claims = connector.verify_id_token(id_token)
        except Exception:
            _logger.info("Jeton Firebase invalide", exc_info=True)
            return {"success": False, "error": "Jeton Firebase invalide"}

        uid = claims["uid"]
        email = (claims.get("email") or "").strip().lower()
        if not email:
            return {"success": False, "error": "Email introuvable dans le compte Firebase"}

        user = self._find_or_create_user(env, uid, email, bool(claims.get("email_verified")), claims.get("name"))
        if not user:
            return {"success": False, "error": "Ce compte ne peut pas se connecter avec Firebase"}

        user.partner_id.last_login_firebase = False if False else None  # pas de champ dédié : voir fireman.member
        user.partner_id.last_login_firebase = False if False else None  # pas de champ dédié : voir fireman.member
        request.session.uid = user.id
        request.session.login = user.login
        request.session.session_token = user._compute_session_token(request.session.sid)
        request.session.context = dict(request.env.context)
        request.env = request.env(user=user.id)
        return {"success": True, "redirect_url": "/my/uo"}

    def _find_or_create_user(self, env, uid, email, email_verified, display_name):
        Users = env["res.users"].with_context(active_test=False)
        user = Users.search([("partner_id.firebase_uid", "=", uid)], limit=1)
        if not user:
            user = Users.search([("login", "=", email)], limit=1)
            # Un compte Odoo existant n'est rattaché que si Firebase a vérifié l'email.
            if user and not email_verified:
                return None
        if user:
            if not user.share or not user.active:  # jamais d'utilisateur interne via Firebase
                return None
            if not user.partner_id.firebase_uid:
                user.partner_id.firebase_uid = uid
            return user

        Partner = env["res.partner"]
        partner = Partner.search([("firebase_uid", "=", uid)], limit=1) or Partner.search(
            [
                ("email", "=ilike", email),
                ("is_company", "=", False),
                ("firebase_uid", "=", False),
                ("user_ids", "=", False),
            ],
            limit=1,
        )
        if partner:
            partner.firebase_uid = uid
        else:
            partner = Partner.create({"name": display_name or email, "email": email, "firebase_uid": uid})
        return Users.with_context(no_reset_password=True).create(
            {
                "login": email,
                "partner_id": partner.id,
                "groups_id": [(6, 0, [env.ref("base.group_portal").id])],
            }
        )

    # ------------------------------------------------------------------
    # Contact : crée un ticket Helpdesk
    # ------------------------------------------------------------------
    @http.route(
        ["/contact", "/contactus", "/contactus-thank-you"],
        type="http",
        auth="public",
        website=True,
        sitemap=True,
        methods=["GET", "POST"],
    )
    def contact(self, **kw):
        if request.httprequest.method == "GET":
            ticket = request.session.pop("contact_ticket_success", None)
            values = {"form_values": {}}
            if ticket:
                values.update(success=True, ticket_id=ticket["ticket_id"], ticket_email=ticket["ticket_email"])
            return request.render("inventory_fireman.portal_contact", values)

        name = (kw.get("contact_name") or "").strip()
        email = (kw.get("contact_email") or "").strip().lower()
        subject = (kw.get("contact_subject") or "").strip()
        message = (kw.get("contact_message") or "").strip()
        category = kw.get("contact_category") if kw.get("contact_category") in CONTACT_CATEGORIES else "autre"
        uo_code = (kw.get("contact_uo") or "").strip().upper()

        error = None
        if not (name and email and subject and message):
            error = "Veuillez remplir tous les champs obligatoires."
        elif "@" not in email:
            error = "Adresse email invalide."
        if error:
            return request.render("inventory_fireman.portal_contact", {"error": error, "form_values": kw})

        try:
            description = Markup(
                "<ul><li><strong>Nom :</strong> {name}</li><li><strong>Email :</strong> {email}</li>"
                "<li><strong>Catégorie :</strong> {category}</li>{uo}</ul><hr/>"
                "<p><strong>Message :</strong></p><p>{message}</p><hr/>"
                "<small>Ticket soumis depuis le formulaire de contact du site.</small>"
            ).format(
                name=name,
                email=email,
                category=CONTACT_CATEGORIES[category],
                uo=Markup("<li><strong>Code UO :</strong> {}</li>").format(uo_code) if uo_code else "",
                message=Markup("<br/>").join(escape(line) for line in message.splitlines()),
            )
            Ticket = request.env["helpdesk.ticket"].sudo()
            team = request.env["helpdesk.team"].sudo().search([], limit=1)
            ticket = Ticket.create(
                {
                    "name": f"[{CONTACT_CATEGORIES[category]}] {subject}",
                    "partner_name": name,
                    "partner_email": email,
                    "description": description,
                    "team_id": team.id or False,
                }
            )
        except Exception:
            _logger.exception("Création du ticket de contact impossible")
            return request.render(
                "inventory_fireman.portal_contact",
                {"error": "Une erreur technique est survenue. Veuillez réessayer.", "form_values": kw},
            )
        request.session["contact_ticket_success"] = {"ticket_id": ticket.id, "ticket_email": email}
        return request.redirect("/contact")
