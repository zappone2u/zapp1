# -*- coding: utf-8 -*-

import logging
import requests
from dateutil.relativedelta import relativedelta

from odoo import http, fields, _
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.exceptions import AccessError, ValidationError
from markupsafe import Markup

_logger = logging.getLogger(__name__)

# Configuration Firebase - API REST (pas besoin de SDK Admin)
FIREBASE_WEB_API_KEY = "AIzaSyCbPwjcyqE4Wa2eW9_uL7UxJesbhQ-zB_U"
FIREBASE_VERIFY_TOKEN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={}"

# Import Firebase Admin SDK (optionnel, pour la synchronisation)
try:
    from firebase_admin import db, firestore
except ImportError:
    _logger.warning("Firebase Admin SDK non installé. La synchronisation Firebase ne sera pas disponible.")
    db = None
    firestore = None


class FirebasePortal(CustomerPortal):
    """Portail pour les administrateurs Firebase"""

    def _prepare_home_portal_values(self, counters):
        """Ajouter les compteurs Firebase au portail"""
        values = super()._prepare_home_portal_values(counters)
        partner = request.env.user.partner_id

        # Activer la catégorie client si l'utilisateur a des UOs
        if partner.is_firebase_pompier or partner.uo_ids:
            values["portal_client_category_enable"] = True

        # Ajouter le compteur de UOs
        if "uo_count" in counters:
            values["uo_count"] = len(partner.uo_ids) if partner.uo_ids else 0

        return values

    @http.route(["/", "/home", "/firebase/home"], type="http", auth="public", website=True, sitemap=True)
    def firebase_home(self, **kw):
        """Page d'accueil de l'application Inventory Fireman"""
        return request.render("inventory_fireman.firebase_home_page_redesign", {})

    @http.route(["/policies", "/terms"], type="http", auth="public", website=True, sitemap=True)
    def policies_page(self, **kw):
        """Page des politiques et conditions d'utilisation"""
        return request.render("inventory_fireman.portal_policies", {})

    @http.route(["/my/firebase/register"], type="http", auth="public", website=True, sitemap=False)
    def firebase_register(self, **kw):
        """Page d'inscription Firebase"""
        return request.render("inventory_fireman.firebase_register_page", {})

    @http.route(
        ["/my/firebase/login", "/firebase/login", "/login/firebase"],
        type="http",
        auth="public",
        website=True,
        sitemap=False,
    )
    def firebase_login(self, **kw):
        """Page de connexion Firebase - Accessible via plusieurs URLs"""
        user = request.env.user

        # Si l'utilisateur est déjà connecté, rediriger vers ses UOs
        if not user._is_public():
            return request.redirect("/my/firebase/uo")

        # Sinon, afficher la page de connexion
        return request.render("inventory_fireman.firebase_login_page", {})

    @http.route(["/firebase/dashboard", "/my/firebase"], type="http", auth="user", website=True, sitemap=False)
    def firebase_dashboard(self, **kw):
        """Dashboard Firebase - Redirige vers la liste des UO"""
        return request.redirect("/my/firebase/uo")

    @http.route(
        ["/pricing", "/my/pricing", "/abonnement", "/pricing"],
        type="http",
        auth="public",
        website=True,
        sitemap=False,
    )
    def firebase_subscription_info(self, **kw):
        """Page d'information sur les abonnements et le paiement"""
        return request.render("inventory_fireman.firebase_subscription_info", {})

    # ─────────────────────────────────────────────────────────────────────────
    # PAGE CONTACT — crée un ticket Helpdesk Odoo
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        ["/contact", "/contactus", "/contactus-thank-you"],
        type="http",
        auth="public",
        website=True,
        sitemap=True,
        methods=["GET", "POST"],
    )
    def contact_page(self, **kw):
        CATEGORY_LABELS = {
            "support_technique": "🔧 Support technique",
            "facturation": "💳 Facturation / Abonnement",
            "compte": "👤 Compte utilisateur",
            "application": "📱 Application mobile",
            "fonctionnalite": "💡 Demande de fonctionnalité",
            "autre": "📌 Autre",
        }

        # GET avec succès en session (PRG pattern — évite re-soumission au refresh)
        if request.httprequest.method == "GET":
            ticket_info = request.session.pop("contact_ticket_success", None)
            if ticket_info:
                return request.render(
                    "inventory_fireman.portal_contact",
                    {
                        "success": True,
                        "ticket_id": ticket_info["ticket_id"],
                        "ticket_email": ticket_info["ticket_email"],
                        "form_values": {},
                    },
                )
            return request.render(
                "inventory_fireman.portal_contact",
                {"form_values": {}},
            )

        if request.httprequest.method == "POST":
            contact_name = (kw.get("contact_name") or "").strip()
            contact_email = (kw.get("contact_email") or "").strip().lower()
            contact_subject = (kw.get("contact_subject") or "").strip()
            contact_message = (kw.get("contact_message") or "").strip()
            contact_category = (kw.get("contact_category") or "autre").strip()
            contact_uo = (kw.get("contact_uo") or "").strip().upper()

            # Validation basique
            if not contact_name or not contact_email or not contact_subject or not contact_message:
                return request.render(
                    "inventory_fireman.portal_contact",
                    {
                        "error": "Veuillez remplir tous les champs obligatoires.",
                        "form_values": kw,
                    },
                )
            if "@" not in contact_email:
                return request.render(
                    "inventory_fireman.portal_contact",
                    {
                        "error": "Adresse email invalide.",
                        "form_values": kw,
                    },
                )

            try:
                category_label = CATEGORY_LABELS.get(contact_category, "📌 Autre")

                # Construire le corps HTML du ticket
                uo_line = f"<li><strong>Code UO :</strong> {contact_uo}</li>" if contact_uo else ""
                description_html = Markup(f"""
                    <ul>
                        <li><strong>Nom :</strong> {contact_name}</li>
                        <li><strong>Email :</strong> {contact_email}</li>
                        <li><strong>Catégorie :</strong> {category_label}</li>
                        {uo_line}
                    </ul>
                    <hr/>
                    <p><strong>Message :</strong></p>
                    <p>{contact_message.replace(chr(10), "<br/>")}</p>
                    <hr/>
                    <small style="color:#888;">Ticket soumis depuis le formulaire /contact du site web.</small>
                """)

                # Trouver la première équipe helpdesk disponible
                helpdesk_team = request.env["helpdesk.team"].sudo().search([], limit=1)

                ticket_vals = {
                    "name": f"[{category_label}] {contact_subject}",
                    "partner_name": contact_name,
                    "partner_email": contact_email,
                    "description": description_html,
                    "user_id": 3,
                }
                if helpdesk_team:
                    ticket_vals["team_id"] = helpdesk_team.id

                ticket = request.env["helpdesk.ticket"].sudo().create(ticket_vals)

                _logger.info(
                    f"Ticket helpdesk #{ticket.id} créé depuis /contact — "
                    f"De: {contact_name} <{contact_email}> — Sujet: {contact_subject}"
                )

                # PRG : stocker le succès en session et rediriger en GET
                request.session["contact_ticket_success"] = {
                    "ticket_id": ticket.id,
                    "ticket_email": contact_email,
                }
                return request.redirect("/contact")

            except Exception as e:
                _logger.error(f"Erreur création ticket helpdesk depuis /contact: {e}", exc_info=True)
                return request.render(
                    "inventory_fireman.portal_contact",
                    {
                        "error": "Une erreur technique est survenue. Veuillez réessayer ou nous contacter directement par email.",
                        "form_values": kw,
                    },
                )

    @http.route(["/my/firebase/auth"], type="json", auth="public", methods=["POST"])
    def firebase_authenticate(self, id_token=None, **kw):
        """
        Authentifie un utilisateur Firebase et crée/lie son compte Odoo

        :param id_token: Token Firebase ID
        :return: dict avec success et redirect_url
        """
        if not id_token:
            return {"success": False, "error": "Token manquant"}

        try:
            # Vérifier le token Firebase avec l'API REST
            response = requests.post(FIREBASE_VERIFY_TOKEN_URL.format(FIREBASE_WEB_API_KEY), json={"idToken": id_token})

            if response.status_code != 200:
                _logger.error("Erreur Firebase API: %s", response.text)
                return {"success": False, "error": "Token Firebase invalide"}

            result = response.json()

            if "users" not in result or len(result["users"]) == 0:
                return {"success": False, "error": "Utilisateur Firebase non trouvé"}

            user_data = result["users"][0]
            firebase_uid = user_data.get("localId")
            email = user_data.get("email")
            email_verified = user_data.get("emailVerified", False)

            if not email:
                return {"success": False, "error": "Email non trouvé dans le token"}

            # Chercher ou créer l'utilisateur Odoo
            user = request.env["res.users"].sudo().search([("login", "=", email)], limit=1)

            if not user:
                # Créer un nouvel utilisateur portail
                partner = request.env["res.partner"].sudo().search([("firebase_uid", "=", firebase_uid)], limit=1)

                if not partner:
                    # Créer le partner pompier
                    partner = (
                        request.env["res.partner"]
                        .sudo()
                        .create(
                            {
                                "name": user_data.get("displayName", email),
                                "email": email,
                                "firebase_uid": firebase_uid,
                                "is_firebase_pompier": True,
                            }
                        )
                    )

                # Créer l'utilisateur portail
                user = (
                    request.env["res.users"]
                    .sudo()
                    .create(
                        {
                            "login": email,
                            "partner_id": partner.id,
                            "groups_id": [(6, 0, [request.env.ref("base.group_portal").id])],
                        }
                    )
                )
            else:
                # Mettre à jour le partner avec les infos Firebase (seulement si firebase_uid est différent ou vide)
                partner = user.partner_id
                update_vals = {
                    "is_firebase_pompier": True,
                }

                # Ne mettre à jour firebase_uid que s'il est vide ou différent
                if not partner.firebase_uid or partner.firebase_uid != firebase_uid:
                    # Vérifier qu'aucun autre partner n'a déjà ce firebase_uid
                    existing_partner = (
                        request.env["res.partner"]
                        .sudo()
                        .search([("firebase_uid", "=", firebase_uid), ("id", "!=", partner.id)], limit=1)
                    )

                    if not existing_partner:
                        update_vals["firebase_uid"] = firebase_uid

                partner.sudo().write(update_vals)

            # Mettre à jour la dernière connexion
            partner.sudo().write({"last_login": fields.Datetime.now()})

            # Connecter l'utilisateur à la session Odoo
            # Note: On ne peut pas utiliser authenticate() avec un token Firebase
            # On doit créer la session manuellement
            request.session.uid = user.id
            request.session.login = user.login
            request.session.session_token = user._compute_session_token(request.session.sid)
            request.session.context = dict(request.env.context)
            request.env = request.env(user=user.id)

            return {"success": True, "redirect_url": "/my/firebase/uo"}

        except Exception as e:
            _logger.error(f"Erreur d'authentification Firebase: {e}")
            return {"success": False, "error": str(e)}

    @http.route(["/my/firebase/uo"], type="http", auth="user", website=True)
    def my_firebase_uo_list(self, **kw):
        """Liste des UO de l'utilisateur connecté"""
        user = request.env.user
        partner = user.partner_id

        # Récupérer les UO de l'utilisateur (où il est admin)
        uo_list = partner.uo_ids.filtered(lambda uo: uo.is_firebase_uo)

        values = {
            "partner": partner,
            "uo_list": uo_list,
            "page_name": "my_uo",
        }
        return request.render("inventory_fireman.portal_my_uo", values)

    @http.route(["/my/firebase/uo/create"], type="http", auth="user", website=True, methods=["GET", "POST"])
    def my_firebase_uo_create(self, **kw):
        """Créer une nouvelle UO"""
        user = request.env.user
        partner = user.partner_id

        if request.httprequest.method == "POST":
            uo_name = kw.get("uo_name", "").strip()
            trigramme = kw.get("uo_code", "").strip().upper()

            if not uo_name:
                return request.redirect("/my/firebase/uo/create?error=missing_name")

            if not trigramme:
                return request.redirect("/my/firebase/uo/create?error=missing_trigramme")

            # ── Vérifier l'unicité du trigramme ──────────────────────────────
            existing_uo = (
                request.env["res.partner"]
                .sudo()
                .search(
                    [
                        ("uo_code", "=", trigramme),
                        ("is_firebase_uo", "=", True),
                    ],
                    limit=1,
                )
            )
            if existing_uo:
                return request.redirect(f"/my/firebase/uo/create?error=trigramme_exists&code={trigramme}")

            partner_super_admins = request.env["res.partner"].sudo().search([("admin", "=", True)])
            all_admins = partner_super_admins | partner  # Utiliser | pour combiner les recordsets
            admin_ids = [(4, admin.id) for admin in all_admins]  # Format pour many2many

            try:
                # Créer la nouvelle UO
                uo = (
                    request.env["res.partner"]
                    .sudo()
                    .create(
                        {
                            "name": uo_name,
                            "uo_code": trigramme if trigramme else uo_name[:3].upper(),
                            "is_firebase_uo": True,
                            "is_company": True,
                            "company_type": "company",
                            "subscription_tier": "discovery",
                            "pompier_ids": admin_ids,
                        }
                    )
                )

                _logger.info(f"Nouvelle UO créée: {uo.name} (ID: {uo.id}) par {user.name}")

                # ── 1. Créer la relation pompier.uo.rel pour le créateur (admin) ──
                request.env["pompier.uo.rel"].sudo().create(
                    {
                        "pompier_id": partner.id,
                        "uo_id": uo.id,
                        "uo_admin": True,
                        "uo_pharmacist": False,
                        "verified": True,
                    }
                )
                _logger.info(f"✅ Relation pompier.uo.rel créée pour {partner.name} → {uo.name} (admin)")

                # ── 2. Créer les relations pour les super admins ──────────────
                for admin in partner_super_admins:
                    existing_rel = (
                        request.env["pompier.uo.rel"]
                        .sudo()
                        .search(
                            [
                                ("pompier_id", "=", admin.id),
                                ("uo_id", "=", uo.id),
                            ],
                            limit=1,
                        )
                    )
                    if not existing_rel:
                        request.env["pompier.uo.rel"].sudo().create(
                            {
                                "pompier_id": admin.id,
                                "uo_id": uo.id,
                                "uo_admin": True,
                                "uo_pharmacist": False,
                                "verified": True,
                            }
                        )
                        _logger.info(f"✅ Relation pompier.uo.rel créée pour super admin {admin.name} → {uo.name}")

                # ── 3. Synchroniser l'UO vers Firebase (structure UO) ─────────
                sync_success = self._sync_uo_to_firebase(uo)
                if sync_success:
                    _logger.info(f"✅ UO {uo.name} synchronisée avec Firebase")
                else:
                    _logger.warning(f"⚠️ UO {uo.name} créée dans Odoo mais pas synchronisée avec Firebase")

                # ── 4. Ajouter l'UO dans les profils Firebase (créateur + super admins) ─
                for admin_partner in all_admins:
                    try:
                        self._add_uo_to_pompier_firebase(admin_partner, uo, True, False)
                        _logger.info(f"✅ UO {uo.uo_code} ajoutée au profil Firebase de {admin_partner.name}")
                    except Exception as fb_err:
                        _logger.warning(f"⚠️ Impossible de sync Firebase pour {admin_partner.name}: {fb_err}")

                return request.redirect(f"/my/firebase/uo/{uo.id}?success=created")

            except Exception as e:
                _logger.error(f"Erreur lors de la création de l'UO: {str(e)}", exc_info=True)
                return request.redirect("/my/firebase/uo/create?error=creation_failed")

        # Afficher le formulaire de création
        values = {
            "partner": partner,
            "page_name": "create_uo",
        }
        return request.render("inventory_fireman.portal_uo_create", values)

    # @http.route(["/my/firebase/subscriptions"], type="http", auth="user", website=True)
    # def my_firebase_subscriptions(self, **kw):
    #     """Page dédiée à la gestion des abonnements"""
    #     user = request.env.user
    #     partner = user.partner_id

    #     # Récupérer les UO de l'utilisateur
    #     uo_list = partner.uo_ids.filtered(lambda uo: uo.is_firebase_uo)

    #     # Récupérer tous les produits d'abonnement disponibles avec sudo()
    #     subscription_products = []

    #     product_refs = [
    #         "inventory_fireman.product_starter",
    #         "inventory_fireman.product_ready_monthly",
    #         "inventory_fireman.product_ready_yearly",
    #         "inventory_fireman.product_user_addon",
    #         "inventory_fireman.product_vehicle_addon",
    #     ]

    #     for ref in product_refs:
    #         product = request.env.ref(ref, False)
    #         if product and product.exists():
    #             # Utiliser sudo() pour permettre l'accès aux utilisateurs portal
    #             subscription_products.append(product.sudo())

    #     values = {
    #         "partner": partner,
    #         "uo_list": uo_list,
    #         "subscription_products": subscription_products,
    #         "page_name": "subscriptions",
    #     }
    #     return request.render("inventory_fireman.portal_subscriptions", values)

    @http.route(["/my/firebase/uo/<int:uo_id>"], type="http", auth="user", website=True)
    def my_firebase_uo_detail(self, uo_id, **kw):
        """Détails d'une UO avec possibilité de souscrire"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Récupérer le produit d'activation et les add-ons
        activation_product = request.env.ref("inventory_fireman.product_activation_monthly", False)
        user_addon = request.env.ref("inventory_fireman.product_user_addon", False)
        vehicle_addon = request.env.ref("inventory_fireman.product_vehicle_addon", False)

        values = {
            "partner": partner,
            "uo": uo,
            "activation_product": activation_product.sudo() if activation_product else None,
            "user_addon": user_addon.sudo() if user_addon else None,
            "vehicle_addon": vehicle_addon.sudo() if vehicle_addon else None,
            "page_name": "uo_detail",
        }
        return request.render("inventory_fireman.portal_uo_detail", values)

    @http.route(["/my/firebase/uo/<int:uo_id>/subscription/choose"], type="http", auth="user", website=True)
    def my_firebase_uo_subscription_choose(self, uo_id, **kw):
        """Page de choix du palier d'abonnement pour une UO (souscription ou changement de plan)"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Récupérer les 3 produits d'abonnement
        product_essentiel = request.env.ref(
            "inventory_fireman.product_subscription_essentiel", raise_if_not_found=False
        )
        product_caserne = request.env.ref("inventory_fireman.product_subscription_caserne", raise_if_not_found=False)
        product_flotte = request.env.ref("inventory_fireman.product_subscription_flotte", raise_if_not_found=False)

        values = {
            "partner": partner,
            "uo": uo,
            "product_essentiel": product_essentiel.sudo() if product_essentiel else None,
            "product_caserne": product_caserne.sudo() if product_caserne else None,
            "product_flotte": product_flotte.sudo() if product_flotte else None,
            "current_tier": uo.subscription_tier or "discovery",
            "page_name": "subscription_choose",
            "error": kw.get("error"),
        }
        return request.render("inventory_fireman.portal_subscription_choose", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/subscription/activate"],
        type="http",
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def my_firebase_uo_subscription_activate(self, uo_id, tier=None, **kw):
        """Page de vérification d'email et activation de l'abonnement"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Vérifier que le palier est valide
        if tier not in ["essentiel", "caserne", "flotte"]:
            return request.redirect(f"/my/firebase/uo/{uo_id}/subscription/choose?error=invalid_tier")

        # Si c'est une requête POST, on traite l'activation
        if request.httprequest.method == "POST":
            billing_email = kw.get("billing_email", "").strip()

            if not billing_email or "@" not in billing_email:
                return request.redirect(
                    f"/my/firebase/uo/{uo_id}/subscription/activate?tier={tier}&error=invalid_email"
                )

            try:
                # Calculer la prochaine date de facturation
                today = fields.Date.today()
                if today.day >= 20:
                    invoice_date = (today + relativedelta(months=1)).replace(day=1) + relativedelta(months=1, days=-1)
                else:
                    invoice_date = today.replace(day=1) + relativedelta(months=1, days=-1)

                # Mettre à jour l'email de facturation de l'UO
                uo.sudo().write(
                    {
                        "billing_email": billing_email,
                        "billing_email_verified": True,
                        "email": billing_email,
                    }
                )

                # Récupérer le produit correspondant au palier
                product_ref = {
                    "essentiel": "inventory_fireman.product_subscription_essentiel",
                    "caserne": "inventory_fireman.product_subscription_caserne",
                    "flotte": "inventory_fireman.product_subscription_flotte",
                }

                product = request.env.ref(product_ref[tier], raise_if_not_found=False)
                if not product:
                    raise Exception(f"Produit d'abonnement {tier} non trouvé")

                # Toujours utiliser sudo() pour accéder aux champs produit (utilisateur portail)
                product = product.sudo()

                # Clôturer l'ancienne commande d'abonnement si elle existe
                old_order = uo.sudo().subscription_sale_order_id
                if old_order and old_order.state not in ["cancel", "done"]:
                    try:
                        if old_order.is_subscription and old_order.subscription_state in ["3_progress", "4_paused"]:
                            # Abonnement actif avec factures : utiliser set_close() (→ churn 6_churn)
                            # close_reason "unknown" = résiliation/changement de plan
                            close_reason = request.env.ref(
                                "sale_subscription.close_reason_unknown", raise_if_not_found=False
                            )
                            old_order.sudo().set_close(close_reason_id=close_reason.id if close_reason else None)
                        else:
                            # Devis ou abonnement non facturé : annulation simple
                            old_order.sudo().action_cancel()
                        _logger.info(f"Ancienne commande {old_order.name} clôturée pour changement de palier")
                    except Exception as cancel_err:
                        _logger.warning(f"Impossible de clôturer l'ancienne commande: {cancel_err}")

                # Détacher l'ancienne commande de l'UO avant d'en créer une nouvelle
                uo.sudo().write({"subscription_sale_order_id": False})

                # Créer la nouvelle commande de vente avec l'UO comme partner_id
                sale_order = (
                    request.env["sale.order"]
                    .sudo()
                    .create(
                        {
                            "partner_id": uo.id,
                            "partner_invoice_id": uo.id,
                            "partner_shipping_id": uo.id,
                            "user_id": 3,
                            "firebase_uo_id": uo.id,
                            "plan_id": 1,
                            "note": (
                                f"Abonnement {tier} pour UO: {uo.name}\n"
                                f"Souscrit par: {partner.name} ({user.login})\n"
                                f"Email de facturation: {billing_email}"
                            ),
                            "order_line": [
                                (
                                    0,
                                    0,
                                    {
                                        "product_id": product.id,
                                        "name": product.name,
                                        "product_uom_qty": 1,
                                        "price_unit": product.list_price,
                                    },
                                )
                            ],
                        }
                    )
                )

                # Confirmer la commande (active l'UO et pousse vers Firebase via action_confirm)
                sale_order.action_confirm()

                # Mettre à jour la date de facturation si le champ existe
                try:
                    sale_order.sudo().write({"next_invoice_date": invoice_date})
                except Exception as e:
                    _logger.debug(f"next_invoice_date non applicable: {e}")

                _logger.info(
                    f"Abonnement {tier} activé pour l'UO {uo.name} (ID: {uo.id}) - "
                    f"Commande: {sale_order.name} - Email: {billing_email}"
                )

                return request.redirect(f"/my/firebase/uo/{uo_id}?success=subscription_activated")

            except Exception as e:
                _logger.error(f"Erreur lors de l'activation de l'abonnement: {str(e)}", exc_info=True)
                return request.redirect(
                    f"/my/firebase/uo/{uo_id}/subscription/activate?tier={tier}&error=activation_failed"
                )

        product_ref = {
            "essentiel": "inventory_fireman.product_subscription_essentiel",
            "caserne": "inventory_fireman.product_subscription_caserne",
            "flotte": "inventory_fireman.product_subscription_flotte",
        }

        product = request.env.ref(product_ref[tier], raise_if_not_found=False)

        # Email par défaut: celui de l'UO ou celui de l'utilisateur
        default_email = uo.email or uo.billing_email or user.login or ""

        # Tier technique (pour affichage des limites)
        tier_labels = {
            "essentiel": {"users": "15", "vehicles": "3", "price": "5"},
            "caserne": {"users": "30", "vehicles": "10", "price": "10"},
            "flotte": {"users": "Illimités", "vehicles": "30", "price": "15"},
        }

        values = {
            "partner": partner,
            "uo": uo,
            "tier": tier,
            "tier_info": tier_labels.get(tier, {}),
            "product": product.sudo() if product else None,
            "default_email": default_email,
            "current_tier": uo.subscription_tier or "discovery",
            "is_upgrade": bool(uo.subscription_tier and uo.subscription_tier not in ["discovery", False]),
            "page_name": "subscription_activate",
            "error": kw.get("error"),
        }
        return request.render("inventory_fireman.portal_subscription_activate", values)

    # ─────────────────────────────────────────────────────────────────────────
    # RÉSILIATION D'ABONNEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/subscription/cancel"],
        type="http",
        auth="user",
        website=True,
        methods=["GET"],
    )
    def my_firebase_uo_subscription_cancel_wizard(self, uo_id, **kw):
        """Affiche le wizard de résiliation d'abonnement"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Pas d'abonnement actif à résilier
        if not uo.subscription_tier or uo.subscription_tier == "discovery":
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=no_active_subscription")

        tier_labels = {
            "essential": "Essentiel",
            "caserne": "Caserne",
            "flotte": "Flotte",
        }

        values = {
            "partner": partner,
            "uo": uo,
            "tier_label": tier_labels.get(uo.subscription_tier, uo.subscription_tier),
            "current_order": uo.subscription_sale_order_id,
            "page_name": "subscription_cancel",
            "error": kw.get("error"),
        }
        return request.render("inventory_fireman.portal_subscription_cancel_wizard", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/subscription/cancel/confirm"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def my_firebase_uo_subscription_cancel_confirm(self, uo_id, **kw):
        """Traitement de la résiliation : annule la commande et remet l'UO en découverte"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        if not uo.subscription_tier or uo.subscription_tier == "discovery":
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=no_active_subscription")

        reason = kw.get("cancel_reason", "").strip()
        if not reason:
            return request.redirect(f"/my/firebase/uo/{uo_id}/subscription/cancel?error=missing_reason")

        try:
            old_order = uo.sudo().subscription_sale_order_id

            # Clôturer la commande d'abonnement active
            if old_order and old_order.state not in ["cancel", "done"]:
                try:
                    if old_order.is_subscription and old_order.subscription_state in ["3_progress", "4_paused"]:
                        # Abonnement actif avec factures → set_close() (churn propre)
                        close_reason = request.env.ref(
                            "sale_subscription.close_reason_unknown", raise_if_not_found=False
                        )
                        old_order.sudo().set_close(close_reason_id=close_reason.id if close_reason else None)
                    else:
                        # Devis ou abonnement non facturé → annulation simple
                        old_order.sudo().action_cancel()
                    _logger.info(f"Commande {old_order.name} clôturée suite à résiliation par {partner.name}")
                except Exception as cancel_err:
                    _logger.warning(f"Impossible de clôturer la commande {old_order.name}: {cancel_err}")

            # Remettre l'UO en mode Découverte
            uo.sudo().write(
                {
                    "subscription_tier": "discovery",
                    "activation_status": "discovery",
                    "subscription_sale_order_id": False,
                    "max_users": 1,
                    "max_vehicles": 1,
                }
            )

            # Pousser le statut discovery vers Firebase
            try:
                uo.sudo()._push_subscription_to_firebase()
            except Exception as fb_err:
                _logger.warning(f"Impossible de pousser la résiliation vers Firebase: {fb_err}")

            # Ajouter une note sur l'UO
            uo.sudo().message_post(
                body=(
                    f"🚫 <strong>Résiliation abonnement</strong> par {partner.name} ({user.login})<br/>"
                    f"Raison : {reason}<br/>"
                    f"L'UO est repassée en mode <strong>Découverte</strong>."
                ),
                message_type="notification",
                subtype_xmlid="mail.mt_note",
            )

            if old_order:
                old_order.sudo().message_post(
                    body=Markup(_(f"Abonnement résilié par {partner.name} ({user.login}).<br/>Raison : {reason}")),
                    message_type="comment",
                )

            _logger.info(f"Résiliation abonnement UO {uo.name} (ID: {uo.id}) par {partner.name} — Raison: {reason}")

            return request.redirect(f"/my/firebase/uo/{uo_id}?success=subscription_cancelled")

        except Exception as e:
            _logger.error(f"Erreur lors de la résiliation de l'abonnement UO {uo_id}: {e}", exc_info=True)
            return request.redirect(f"/my/firebase/uo/{uo_id}/subscription/cancel?error=cancel_failed")

    # ─────────────────────────────────────────────────────────────────────────
    # PERSONNEL (pompiers)
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(["/my/firebase/uo/<int:uo_id>/personnel"], type="http", auth="user", website=True)
    def my_firebase_uo_personnel(self, uo_id, **kw):
        """Liste des pompiers d'une UO"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))
        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer le personnel de cette UO"))

        # Récupérer toutes les relations pompier-UO avec les informations
        rel_ids = request.env["pompier.uo.rel"].sudo().search([("uo_id", "=", uo.id), ("pompier_id.admin", "=", False)])

        values = {
            "partner": partner,
            "uo": uo,
            "rel_ids": rel_ids,
            "page_name": "uo_personnel",
        }
        return request.render("inventory_fireman.portal_uo_personnel", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/personnel/invite"],
        type="http",
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def my_firebase_uo_personnel_invite(self, uo_id, **kw):
        """Inviter / ajouter un pompier à une UO par son email Firebase"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        if request.httprequest.method == "POST":
            email = (kw.get("email") or "").strip().lower()
            uo_admin = bool(kw.get("uo_admin"))
            uo_pharmacist = bool(kw.get("uo_pharmacist"))

            if not email:
                return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=missing_email")

            try:
                # Vérifier la limite utilisateurs
                uo.check_user_limit()

                # Chercher le pompier par email
                pompier = (
                    request.env["res.partner"]
                    .sudo()
                    .search([("email", "=", email), ("is_firebase_pompier", "=", True)], limit=1)
                )

                if not pompier:
                    return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=not_found")

                # Vérifier qu'il n'est pas déjà dans l'UO
                existing = (
                    request.env["pompier.uo.rel"]
                    .sudo()
                    .search([("pompier_id", "=", pompier.id), ("uo_id", "=", uo.id)], limit=1)
                )
                if existing:
                    return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=already_member")

                try:
                    self._add_uo_to_pompier_firebase(pompier, uo, uo_admin, uo_pharmacist)
                except Exception as fb_err:
                    _logger.warning(f"⚠️ Impossible de synchroniser l'UO vers Firebase pour {pompier.name}: {fb_err}")
                    return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=invite_failed")

                # Créer la relation
                request.env["pompier.uo.rel"].sudo().create(
                    {
                        "pompier_id": pompier.id,
                        "uo_id": uo.id,
                        "uo_admin": uo_admin,
                        "uo_pharmacist": uo_pharmacist,
                        "verified": True,
                    }
                )

                _logger.info(f"Pompier {pompier.name} ajouté à l'UO {uo.name} par {partner.name}")

                return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?success=invited")

            except ValidationError as e:
                _logger.warning(f"Limite utilisateurs atteinte UO {uo_id}: {e}")
                return request.redirect(f"/my/firebase/uo/{uo_id}/subscription/choose?error=limit_reached_user")
            except Exception as e:
                _logger.error(f"Erreur ajout pompier: {e}", exc_info=True)
                return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=invite_failed")

        # GET: afficher le formulaire
        values = {
            "partner": partner,
            "uo": uo,
            "page_name": "personnel_invite",
        }
        return request.render("inventory_fireman.portal_personnel_invite", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/personnel/<int:rel_id>/remove"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def my_firebase_uo_personnel_remove(self, uo_id, rel_id, **kw):
        """Retirer un pompier d'une UO"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        rel = request.env["pompier.uo.rel"].sudo().browse(rel_id)
        if not rel.exists() or rel.uo_id.id != uo.id:
            raise AccessError(_("Relation introuvable"))

        # Empêcher de se retirer soi-même si on est le seul admin
        rel.sudo().unlink()
        _logger.info(f"Pompier {rel.pompier_name} retiré de l'UO {uo.name} par {partner.name}")
        return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?success=removed")

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/personnel/<int:rel_id>/update"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def my_firebase_uo_personnel_update(self, uo_id, rel_id, **kw):
        """Modifier les rôles d'un pompier dans l'UO"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        rel = request.env["pompier.uo.rel"].sudo().browse(rel_id)
        if not rel.exists() or rel.uo_id.id != uo.id:
            raise AccessError(_("Relation introuvable"))

        try:
            rel.sudo().write(
                {
                    "uo_admin": bool(kw.get("uo_admin")),
                    "uo_pharmacist": bool(kw.get("uo_pharmacist")),
                    "verified": bool(kw.get("verified")),
                }
            )
            return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?success=updated")
        except Exception as e:
            _logger.error(f"Erreur mise à jour rôle pompier: {e}", exc_info=True)
            return request.redirect(f"/my/firebase/uo/{uo_id}/personnel?error=update_failed")

    # ─────────────────────────────────────────────────────────────────────────
    # REPORTING & EXPORTS — accessible à partir du palier Caserne
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(["/my/firebase/uo/<int:uo_id>/reporting"], type="http", auth="user", website=True)
    def my_firebase_uo_reporting(self, uo_id, **kw):
        """Page de reporting — disponible à partir du palier Caserne"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel.exists() or not rel.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour accéder à cette page"))

        # Seuls les paliers caserne et flotte ont accès au reporting
        if uo.subscription_tier not in ("caserne", "flotte"):
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=reporting_not_available")

        # ── Statistiques de base ──────────────────────────────────────────────
        inventories = (
            request.env["firebase.inventory.history"].sudo().search([("uo_id", "=", uo.id)], order="date desc")
        )
        vehicles = uo.vehicle_ids
        pompiers = uo.pompier_uo_rel_ids.filtered(lambda r: not r.pompier_id.admin and r.verified)

        # Inventaires complets vs incomplets
        complete_inventories = inventories.filtered(lambda i: i.inventor_full)
        incomplete_inventories = inventories.filtered(lambda i: not i.inventor_full)

        # Dernières manques signalées (inventaires avec lack non vide)
        lacks = [
            {"vehicle": inv.vehicle, "inventor": inv.inventor_name, "date": inv.date, "lack": inv.lack}
            for inv in inventories
            if inv.lack and inv.lack.strip()
        ][:20]

        # Inventaires par véhicule
        vehicle_stats = []
        for vehicle in vehicles:
            v_inventories = inventories.filtered(lambda i, v=vehicle: i.vehicle == v.label)
            vehicle_stats.append(
                {
                    "vehicle": vehicle,
                    "total": len(v_inventories),
                    "complete": len(v_inventories.filtered(lambda i: i.inventor_full)),
                    "last_date": v_inventories[0].date if v_inventories else False,
                }
            )

        values = {
            "partner": partner,
            "uo": uo,
            "inventories": inventories,
            "vehicles": vehicles,
            "pompiers": pompiers,
            "complete_count": len(complete_inventories),
            "incomplete_count": len(incomplete_inventories),
            "total_inventories": len(inventories),
            "lacks": lacks,
            "vehicle_stats": vehicle_stats,
            "is_flotte": uo.subscription_tier == "flotte",
            "page_name": "uo_reporting",
        }
        return request.render("inventory_fireman.portal_uo_reporting", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/reporting/pdf"],
        type="http",
        auth="user",
        website=True,
    )
    def my_firebase_uo_reporting_pdf(self, uo_id, **kw):
        """Export PDF du rapport d'inventaire — palier Caserne et Flotte"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel.exists() or not rel.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour générer ce rapport"))

        if uo.subscription_tier not in ("caserne", "flotte"):
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=reporting_not_available")

        inventories = (
            request.env["firebase.inventory.history"].sudo().search([("uo_id", "=", uo.id)], order="date desc")
        )
        vehicles = uo.vehicle_ids
        pompiers = uo.pompier_uo_rel_ids.filtered(lambda r: not r.pompier_id.admin and r.verified)

        complete_inventories = inventories.filtered(lambda i: i.inventor_full)
        incomplete_inventories = inventories.filtered(lambda i: not i.inventor_full)

        lacks = [
            {"vehicle": inv.vehicle, "inventor": inv.inventor_name, "date": inv.date, "lack": inv.lack}
            for inv in inventories
            if inv.lack and inv.lack.strip()
        ][:50]

        vehicle_stats = []
        for vehicle in vehicles:
            v_inventories = inventories.filtered(lambda i, v=vehicle: i.vehicle == v.label)
            vehicle_stats.append(
                {
                    "vehicle": vehicle,
                    "total": len(v_inventories),
                    "complete": len(v_inventories.filtered(lambda i: i.inventor_full)),
                    "last_date": v_inventories[0].date if v_inventories else False,
                }
            )

        pdf_values = {
            "uo": uo,
            "partner": partner,
            "inventories": inventories,
            "vehicles": vehicles,
            "pompiers": pompiers,
            "complete_count": len(complete_inventories),
            "incomplete_count": len(incomplete_inventories),
            "total_inventories": len(inventories),
            "lacks": lacks,
            "vehicle_stats": vehicle_stats,
            "is_flotte": uo.subscription_tier == "flotte",
            "generated_by": partner.name,
            "generated_at": fields.Datetime.now(),
        }

        pdf_content, _ = (
            request.env["ir.actions.report"]
            .sudo()
            ._render_qweb_pdf(
                "inventory_fireman.action_report_uo_inventory",
                res_ids=uo.ids,
                data=pdf_values,
            )
        )

        filename = f"rapport_inventaire_{uo.uo_code or uo.id}.pdf"
        return request.make_response(
            pdf_content,
            headers=[
                ("Content-Type", "application/pdf"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
            ],
        )

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/reporting/export/csv"],
        type="http",
        auth="user",
        website=True,
    )
    def my_firebase_uo_reporting_export_csv(self, uo_id, **kw):
        """Export CSV des inventaires — palier Flotte uniquement"""
        import csv
        import io

        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel.exists() or not rel.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions"))

        if uo.subscription_tier != "flotte":
            return request.redirect(f"/my/firebase/uo/{uo_id}/reporting?error=flotte_only")

        inventories = (
            request.env["firebase.inventory.history"].sudo().search([("uo_id", "=", uo.id)], order="date desc")
        )

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Date", "Véhicule", "Inventoriste", "Grade", "Complet", "Manques", "Commentaires"])
        for inv in inventories:
            writer.writerow(
                [
                    inv.date.strftime("%d/%m/%Y %H:%M") if inv.date else inv.inventory_date_str or "",
                    inv.vehicle or "",
                    inv.inventor_name or "",
                    inv.rank or "",
                    "Oui" if inv.inventor_full else "Non",
                    (inv.lack or "").replace("\n", " | "),
                    (inv.more_description or "").replace("\n", " | "),
                ]
            )

        csv_content = "\ufeff" + output.getvalue()  # BOM UTF-8 pour Excel
        filename = f"inventaires_{uo.uo_code or uo.id}.csv"
        return request.make_response(
            csv_content.encode("utf-8"),
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
            ],
        )

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/reporting/api"],
        type="json",
        auth="user",
        methods=["GET"],
    )
    def my_firebase_uo_reporting_api(self, uo_id, **kw):
        """API JSON des données d'inventaire — palier Flotte uniquement"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"error": "Accès refusé", "code": 403}

        rel = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel.exists() or not rel.uo_admin:
            return {"error": "Permissions insuffisantes", "code": 403}

        if uo.subscription_tier != "flotte":
            return {"error": "Cette fonctionnalité est réservée au plan Flotte", "code": 403}

        inventories = (
            request.env["firebase.inventory.history"]
            .sudo()
            .search([("uo_id", "=", uo.id)], order="date desc", limit=200)
        )
        vehicles = uo.vehicle_ids

        return {
            "uo": {
                "id": uo.id,
                "name": uo.name,
                "code": uo.uo_code,
                "tier": uo.subscription_tier,
            },
            "summary": {
                "total_inventories": len(inventories),
                "complete": len(inventories.filtered(lambda i: i.inventor_full)),
                "incomplete": len(inventories.filtered(lambda i: not i.inventor_full)),
                "total_vehicles": len(vehicles),
                "total_pompiers": len(uo.pompier_uo_rel_ids.filtered(lambda r: not r.pompier_id.admin and r.verified)),
            },
            "inventories": [
                {
                    "id": inv.id,
                    "date": inv.date.isoformat() if inv.date else None,
                    "vehicle": inv.vehicle,
                    "inventor_name": inv.inventor_name,
                    "rank": inv.rank,
                    "complete": inv.inventor_full,
                    "lack": inv.lack,
                    "description": inv.more_description,
                }
                for inv in inventories
            ],
            "vehicles": [
                {
                    "id": v.id,
                    "label": v.label,
                    "license_plate": v.license_plate,
                    "status": v.status,
                }
                for v in vehicles
            ],
        }

    @http.route(["/my/firebase/uo/<int:uo_id>/vehicles"], type="http", auth="user", website=True)
    def my_firebase_uo_vehicles(self, uo_id, **kw):
        """Gestion des véhicules d'une UO"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        vehicles = uo.vehicle_ids

        values = {
            "partner": partner,
            "uo": uo,
            "vehicles": vehicles,
            "page_name": "uo_vehicles",
        }
        return request.render("inventory_fireman.portal_uo_vehicles", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/create"], type="http", auth="user", website=True, methods=["GET", "POST"]
    )
    def my_firebase_vehicle_create(self, uo_id, **kw):
        """Créer un nouveau véhicule pour une UO"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Traitement du formulaire (POST)
        if request.httprequest.method == "POST":
            try:
                # Vérifier les limites d'abonnement (lève ValidationError si dépassé)
                uo.check_vehicle_limit()

                # Générer un firebase_uid unique
                import secrets as _secrets

                firebase_uid = f"veh_{_secrets.randbelow(90000000) + 10000000}"

                # Vérifier l'unicité du firebase_uid
                while request.env["firebase.vehicle"].sudo().search_count([("firebase_uid", "=", firebase_uid)]) > 0:
                    firebase_uid = f"veh_{_secrets.randbelow(90000000) + 10000000}"

                # Créer le véhicule
                vehicle_vals = {
                    "uo_id": uo.id,
                    "firebase_uid": firebase_uid,
                    "label": kw.get("label", ""),
                    "license_plate": kw.get("license_plate", ""),
                    "status": kw.get("status", "nothing"),
                    "notes": kw.get("notes", ""),
                    "verified": False,  # Nouveau véhicule pas encore vérifié
                }

                vehicle = request.env["firebase.vehicle"].sudo().create(vehicle_vals)

                _logger.info(f"Nouveau véhicule créé: {vehicle.id} - {vehicle.label} pour UO {uo.name}")

                # Synchroniser le véhicule vers Firebase
                sync_success = self._sync_vehicle_to_firebase(uo.uo_code, vehicle)
                if sync_success:
                    _logger.info(f"✅ Véhicule {vehicle.label} synchronisé avec Firebase")
                else:
                    _logger.warning(f"⚠️ Véhicule {vehicle.label} créé dans Odoo mais pas synchronisé avec Firebase")

                # Rediriger vers la gestion des produits du nouveau véhicule
                return request.redirect(f"/my/firebase/uo/{uo_id}/vehicle/{vehicle.id}/products/manage")

            except Exception as e:
                if isinstance(e, ValidationError):
                    _logger.warning(f"Limite abonnement atteinte pour UO {uo_id}: {e}")
                    return request.redirect(f"/my/firebase/uo/{uo_id}/subscription/choose?error=limit_reached_vehicle")
                _logger.error(f"Erreur lors de la création du véhicule: {str(e)}")
                return request.redirect(f"/my/firebase/uo/{uo_id}/vehicle/create?error=create_failed")

        # Affichage du formulaire (GET)
        values = {
            "partner": partner,
            "uo": uo,
            "page_name": "vehicle_create",
        }
        return request.render("inventory_fireman.portal_vehicle_create", values)

    @http.route(["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>"], type="http", auth="user", website=True)
    def my_firebase_vehicle_detail(self, uo_id, vehicle_id, **kw):
        """Détails d'un véhicule avec tous ses produits"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        # Récupérer le véhicule
        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)

        if not vehicle.exists() or vehicle.uo_id != uo:
            raise AccessError(_("Vous n'avez pas accès à ce véhicule"))

        # Récupérer tous les produits du véhicule
        products = vehicle.product_ids

        values = {
            "partner": partner,
            "uo": uo,
            "vehicle": vehicle,
            "products": products,
            "page_name": "vehicle_detail",
        }
        return request.render("inventory_fireman.portal_vehicle_detail", values)

    @http.route(["/my/firebase/uo/<int:uo_id>/activate"], type="http", auth="user", website=True, methods=["POST"])
    def activate_uo(self, uo_id, payment_method=None, **kw):
        """
        Activer une UO avec un abonnement récurrent
        - Le partner de la commande est l'UO (pas l'utilisateur)
        - Vérifie l'email de l'utilisateur
        - Crée une commande récurrente (abonnement)
        - Confirme la commande
        - Crée et envoie la facture à l'email de l'utilisateur
        """
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Vérifier que l'utilisateur a un email valide
        if not user.login or "@" not in user.login:
            _logger.error(f"L'utilisateur {user.id} n'a pas d'email valide: {user.login}")
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=invalid_email")

        try:
            # Récupérer le produit d'activation avec sudo()
            activation_product = request.env.ref("inventory_fireman.product_activation_monthly").sudo()

            if not activation_product or not activation_product.exists():
                _logger.error("Produit d'activation non trouvé")
                return request.redirect(f"/my/firebase/uo/{uo_id}?error=product_not_found")

            # Créer la commande avec l'UO comme partner (pas l'utilisateur)
            today = fields.Date.today()

            # Si on est le 20 ou plus, on facture fin du mois suivant
            if today.day >= 20:
                invoice_date = (today + relativedelta(months=1)).replace(day=1) + relativedelta(months=1, days=-1)
            else:
                # Sinon, fin du mois courant
                invoice_date = today.replace(day=1) + relativedelta(months=1, days=-1)

            sale_order_vals = {
                "partner_id": uo.id,  # LE PARTNER EST L'UO, PAS L'UTILISATEUR
                "partner_invoice_id": uo.id,
                "partner_shipping_id": uo.id,
                "plan_id": 1,  # Plan par défaut
                "user_id": 3,  # L'utilisateur qui a créé la commande
                "state": "draft",
                "note": (
                    f"Activation abonnement mensuel pour UO: {uo.name}\n"
                    f"Souscrit par: {partner.name} ({user.login})\n"
                    f"Email de facturation: {user.login}"
                ),
            }

            # Vérifier si on doit créer un abonnement récurrent
            # Pour cela, il faut que le produit soit configuré comme récurrent
            if hasattr(activation_product, "recurring_invoice") and activation_product.recurring_invoice:
                sale_order_vals["is_subscription"] = True

            sale_order = request.env["sale.order"].sudo().create(sale_order_vals)

            _logger.info(f"Commande créée: {sale_order.id} pour UO {uo.name} (ID: {uo.id})")

            # Ajouter la ligne de commande
            order_line_vals = {
                "order_id": sale_order.id,
                "product_id": activation_product.product_variant_id.id,
                "name": activation_product.name,
                "product_uom_qty": 1,
                "price_unit": activation_product.list_price,
            }

            request.env["sale.order.line"].sudo().create(order_line_vals)

            _logger.info(f"Ligne de commande ajoutée: {activation_product.name}")

            # Confirmer la commande
            sale_order.action_confirm()
            today = fields.Date.today()

            # Si on est le 20 ou plus, on facture fin du mois suivant
            if today.day >= 20:
                invoice_date = (today + relativedelta(months=1)).replace(day=1) + relativedelta(months=1, days=-1)
            else:
                # Sinon, fin du mois courant
                invoice_date = today.replace(day=1) + relativedelta(months=1, days=-1)
            sale_order.write({"next_invoice_date": invoice_date})
            _logger.info(f"Commande confirmée: {sale_order.name}")

            # Créer la facture
            invoice = sale_order._create_invoices()

            if invoice:
                _logger.info(f"Facture créée: {invoice.id} ({invoice.name})")

                # S'assurer que l'email de facturation est celui de l'utilisateur
                # Mettre à jour l'email de l'UO si nécessaire
                if not uo.email or uo.email != user.login:
                    uo.sudo().write({"email": user.login})
                    _logger.info(f"Email de l'UO mis à jour: {user.login}")

                # Envoyer la facture par email à l'utilisateur
                try:
                    # Forcer l'envoi à l'email de l'utilisateur
                    invoice.sudo().with_context(
                        mail_post_autofollow=True, partner_email=user.login
                    ).action_invoice_sent()
                    _logger.info(f"Facture envoyée à {user.login}")
                except Exception as email_error:
                    _logger.error(f"Erreur lors de l'envoi de l'email: {email_error}", exc_info=True)

                # Ajouter une note sur la commande
                sale_order.sudo().message_post(
                    body=f"✅ Facture {invoice.name} créée et envoyée par email à {user.login}.<br/>"
                    f"En attente du paiement par {payment_method or 'virement bancaire'}.",
                    message_type="comment",
                )

                # Ajouter une note sur l'UO également
                uo.sudo().message_post(
                    body=f"🎯 Activation demandée par {partner.name} ({user.login})<br/>"
                    f"Commande: {sale_order.name}<br/>"
                    f"Facture: {invoice.name}<br/>"
                    f"Montant: {activation_product.list_price} {activation_product.currency_id.symbol}",
                    message_type="notification",
                )

            return request.redirect(f"/my/firebase/uo/{uo_id}?success=invoice_sent")

        except Exception as e:
            _logger.error(f"Erreur lors de l'activation de l'UO {uo_id}: {e}", exc_info=True)
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=activation_failed")

    @http.route(["/my/firebase/uo/<int:uo_id>/add_addon"], type="http", auth="user", website=True, methods=["POST"])
    def add_addon(self, uo_id, addon_type=None, quantity=1, **kw):
        """
        Ajouter des add-ons (utilisateurs ou véhicules supplémentaires)
        """
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        if not addon_type or addon_type not in ["user", "vehicle"]:
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=invalid_addon")

        try:
            # Récupérer le produit addon avec sudo()
            if addon_type == "user":
                addon_product = request.env.ref("inventory_fireman.product_user_addon").sudo()
            else:
                addon_product = request.env.ref("inventory_fireman.product_vehicle_addon").sudo()

            # Récupérer ou créer le panier du site web
            sale_order = request.website.sale_get_order(force_create=True)

            # IMPORTANT: Modifier le partner_id pour que ce soit l'UO et non l'utilisateur
            # ET sauvegarder l'UO dans le champ dédié pour protection
            sale_order.sudo().write(
                {
                    "partner_id": uo.id,
                    "partner_invoice_id": uo.id,
                    "partner_shipping_id": uo.id,
                    "firebase_uo_id": uo.id,  # Protection contre les changements
                }
            )

            # Ajouter le produit au panier
            sale_order._cart_update(
                product_id=addon_product.product_variant_id.id,
                line_id=None,
                add_qty=int(quantity),
                set_qty=0,
            )

            # Ajouter un message pour tracer qui a souscrit
            addon_name = "utilisateurs" if addon_type == "user" else "véhicules"
            sale_order.sudo().message_post(
                body=f"Add-on {quantity} {addon_name} souscrit par {partner.name} ({user.login}) pour l'UO: {uo.name}",
                message_type="comment",
            )

        except Exception as e:
            _logger.error(f"Erreur lors de l'ajout d'add-on: {e}", exc_info=True)
            return request.redirect(f"/my/firebase/uo/{uo_id}?error=addon_failed")

    @http.route(["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/edit"], type="http", auth="user", website=True)
    def my_firebase_vehicle_edit(self, uo_id, vehicle_id, **kw):
        """Page d'édition d'un véhicule"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Récupérer le véhicule
        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)

        if not vehicle.exists() or vehicle.uo_id != uo:
            raise AccessError(_("Vous n'avez pas accès à ce véhicule"))

        # Préparer les options de statut
        status_options = vehicle._fields["status"].selection

        values = {
            "partner": partner,
            "uo": uo,
            "vehicle": vehicle,
            "status_options": status_options,
            "page_name": "vehicle_edit",
        }
        return request.render("inventory_fireman.portal_vehicle_edit", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/update"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
        csrf=True,
    )
    def my_firebase_vehicle_update(self, uo_id, vehicle_id, **kw):
        """Mise à jour d'un véhicule"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Récupérer le véhicule
        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)

        if not vehicle.exists() or vehicle.uo_id != uo:
            raise AccessError(_("Vous n'avez pas accès à ce véhicule"))

        try:
            # Préparer les valeurs à mettre à jour
            update_vals = {}

            if "label" in kw and kw["label"]:
                update_vals["label"] = kw["label"]

            if "license_plate" in kw:
                update_vals["license_plate"] = kw["license_plate"]

            if "vehicle_id" in kw:
                update_vals["vehicle_id"] = kw["vehicle_id"]

            if "status" in kw and kw["status"]:
                update_vals["status"] = kw["status"]

            if "notes" in kw:
                update_vals["notes"] = kw["notes"]

            if "verified" in kw:
                update_vals["verified"] = bool(kw["verified"])

            # Mettre à jour le véhicule
            if update_vals:
                vehicle.sudo().write(update_vals)
                _logger.info(f"Véhicule {vehicle_id} mis à jour: {update_vals}")

                # Synchroniser avec Firebase
                self._sync_vehicle_to_firebase(vehicle.uo_id.name, vehicle)

                return request.redirect(f"/my/firebase/uo/{uo_id}/vehicle/{vehicle_id}?success=updated")
            else:
                return request.redirect(f"/my/firebase/uo/{uo_id}/vehicle/{vehicle_id}/edit?error=no_changes")

        except Exception as e:
            _logger.error(f"Erreur lors de la mise à jour du véhicule: {e}", exc_info=True)
            return request.redirect(f"/my/firebase/uo/{uo_id}/vehicle/{vehicle_id}/edit?error=update_failed")

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/products/manage"], type="http", auth="user", website=True
    )
    def my_firebase_vehicle_products_manage(self, uo_id, vehicle_id, **kw):
        """Page de gestion des produits/catégories et items du véhicule"""
        user = request.env.user
        partner = user.partner_id

        # Vérifier que l'utilisateur a accès à cette UO
        uo = request.env["res.partner"].sudo().browse(uo_id)

        if not uo.exists() or uo not in partner.uo_ids:
            raise AccessError(_("Vous n'avez pas accès à cette UO"))

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        # Récupérer le véhicule
        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)

        if not vehicle.exists() or vehicle.uo_id != uo:
            raise AccessError(_("Vous n'avez pas accès à ce véhicule"))

        # Récupérer tous les produits du véhicule
        products = vehicle.product_ids.sorted(key=lambda p: p.sequence)

        values = {
            "partner": partner,
            "uo": uo,
            "vehicle": vehicle,
            "products": products,
            "page_name": "vehicle_products_manage",
        }
        return request.render("inventory_fireman.portal_vehicle_products_manage", values)

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/product/add"], type="json", auth="user", methods=["POST"]
    )
    def my_firebase_vehicle_product_add(self, uo_id, vehicle_id, label, **kw):
        """Ajouter une catégorie de produit"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        try:
            # Calculer la prochaine séquence
            max_seq = max([p.sequence for p in vehicle.product_ids], default=0)

            # Créer la catégorie
            product = (
                request.env["firebase.vehicle.product"]
                .sudo()
                .create(
                    {
                        "vehicle_id": vehicle_id,
                        "label": label,
                        "sequence": max_seq + 10,
                    }
                )
            )

            # NE PAS synchroniser automatiquement avec Firebase
            # La synchro se fera uniquement lors du clic sur "Synchroniser avec Firebase"

            return {
                "success": True,
                "product_id": product.id,
                "label": product.label,
                "sequence": product.sequence,
            }
        except Exception as e:
            _logger.error(f"Erreur lors de l'ajout de catégorie: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/product/<int:product_id>/update"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_product_update(self, uo_id, vehicle_id, product_id, label=None, sequence=None, **kw):
        """Mettre à jour une catégorie de produit"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        product = request.env["firebase.vehicle.product"].sudo().browse(product_id)
        if not product.exists() or product.vehicle_id != vehicle:
            return {"success": False, "error": "Catégorie non trouvée"}

        try:
            update_vals = {}
            if label is not None:
                update_vals["label"] = label
            if sequence is not None:
                update_vals["sequence"] = int(sequence)

            if update_vals:
                product.write(update_vals)

                # NE PAS synchroniser automatiquement avec Firebase
                # La synchro se fera uniquement lors du clic sur "Synchroniser avec Firebase"

            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la mise à jour de catégorie: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/product/<int:product_id>/delete"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_product_delete(self, uo_id, vehicle_id, product_id, **kw):
        """Supprimer une catégorie de produit"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        product = request.env["firebase.vehicle.product"].sudo().browse(product_id)
        if not product.exists() or product.vehicle_id != vehicle:
            return {"success": False, "error": "Catégorie non trouvée"}

        try:
            # NE PAS supprimer de Firebase automatiquement
            # La synchro se fera lors du clic sur "Synchroniser avec Firebase"

            # Supprimer d'Odoo
            product.unlink()

            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la suppression de catégorie: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/product/<int:product_id>/item/add"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_item_add(self, uo_id, vehicle_id, product_id, description, quantity=1, **kw):
        """Ajouter un item dans une catégorie"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        product = request.env["firebase.vehicle.product"].sudo().browse(product_id)
        if not product.exists() or product.vehicle_id != vehicle:
            return {"success": False, "error": "Catégorie non trouvée"}

        try:
            # Calculer la prochaine séquence
            max_seq = max([i.sequence for i in product.item_ids], default=0)

            # Créer l'item
            item = (
                request.env["firebase.vehicle.item"]
                .sudo()
                .create(
                    {
                        "product_id": product_id,
                        "description": description,
                        "quantity": int(quantity) if quantity else 1,
                        "sequence": max_seq + 10,
                    }
                )
            )

            # NE PAS synchroniser automatiquement avec Firebase
            # La synchro se fera uniquement lors du clic sur "Synchroniser avec Firebase"

            return {
                "success": True,
                "item_id": item.id,
                "description": item.description,
                "quantity": item.quantity,
            }
        except Exception as e:
            _logger.error(f"Erreur lors de l'ajout d'item: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/item/<int:item_id>/update"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_item_update(
        self, uo_id, vehicle_id, item_id, description=None, quantity=None, sequence=None, **kw
    ):
        """Mettre à jour un item"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        item = request.env["firebase.vehicle.item"].sudo().browse(item_id)
        if not item.exists() or item.vehicle_id != vehicle:
            return {"success": False, "error": "Item non trouvé"}

        try:
            update_vals = {}
            if description is not None:
                update_vals["description"] = description
            if quantity is not None:
                update_vals["quantity"] = int(quantity)
            if sequence is not None:
                update_vals["sequence"] = int(sequence)

            if update_vals:
                item.write(update_vals)

                # NE PAS synchroniser automatiquement avec Firebase
                # La synchro se fera uniquement lors du clic sur "Synchroniser avec Firebase"

            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la mise à jour d'item: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/item/<int:item_id>/delete"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_item_delete(self, uo_id, vehicle_id, item_id, **kw):
        """Supprimer un item"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        item = request.env["firebase.vehicle.item"].sudo().browse(item_id)
        if not item.exists() or item.vehicle_id != vehicle:
            return {"success": False, "error": "Item non trouvé"}

        try:
            product = item.product_id

            # NE PAS supprimer de Firebase automatiquement
            # La synchro se fera lors du clic sur "Synchroniser avec Firebase"

            # Supprimer d'Odoo
            item.unlink()

            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la suppression d'item: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ========== GESTION DES GROUPES (SOUS-SECTIONS) ==========

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/product/<int:product_id>/group/add"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_group_add(self, uo_id, vehicle_id, product_id, label, **kw):
        """Ajouter un groupe (sous-section) dans une catégorie"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        product = request.env["firebase.vehicle.product"].sudo().browse(product_id)
        if not product.exists() or product.vehicle_id != vehicle:
            return {"success": False, "error": "Catégorie non trouvée"}

        try:
            max_seq = max([i.sequence for i in product.item_ids if not i.parent_item_id], default=0)
            group = (
                request.env["firebase.vehicle.item"]
                .sudo()
                .create(
                    {
                        "product_id": product_id,
                        "is_group": True,
                        "group_label": label,
                        "description": label,
                        "sequence": max_seq + 10,
                    }
                )
            )
            return {"success": True, "group_id": group.id, "label": group.group_label}
        except Exception as e:
            _logger.error(f"Erreur lors de l'ajout du groupe: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/group/<int:group_id>/update"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_group_update(self, uo_id, vehicle_id, group_id, label=None, **kw):
        """Mettre à jour un groupe (sous-section)"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        group = request.env["firebase.vehicle.item"].sudo().browse(group_id)
        if not group.exists() or group.vehicle_id != vehicle or not group.is_group:
            return {"success": False, "error": "Groupe non trouvé"}

        try:
            if label:
                group.write({"group_label": label, "description": label})
            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la mise à jour du groupe: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/group/<int:group_id>/delete"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_group_delete(self, uo_id, vehicle_id, group_id, **kw):
        """Supprimer un groupe et ses sous-items"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        group = request.env["firebase.vehicle.item"].sudo().browse(group_id)
        if not group.exists() or group.vehicle_id != vehicle or not group.is_group:
            return {"success": False, "error": "Groupe non trouvé"}

        try:
            group.unlink()  # cascade supprime aussi les sub_item_ids
            return {"success": True}
        except Exception as e:
            _logger.error(f"Erreur lors de la suppression du groupe: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/group/<int:group_id>/item/add"],
        type="json",
        auth="user",
        methods=["POST"],
    )
    def my_firebase_vehicle_subitem_add(self, uo_id, vehicle_id, group_id, description, quantity=1, **kw):
        """Ajouter un sous-item dans un groupe"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        group = request.env["firebase.vehicle.item"].sudo().browse(group_id)
        if not group.exists() or group.vehicle_id != vehicle or not group.is_group:
            return {"success": False, "error": "Groupe non trouvé"}

        try:
            max_seq = max([i.sequence for i in group.sub_item_ids], default=0)
            sub_item = (
                request.env["firebase.vehicle.item"]
                .sudo()
                .create(
                    {
                        "product_id": group.product_id.id,
                        "parent_item_id": group_id,
                        "is_group": False,
                        "description": description,
                        "quantity": int(quantity) if quantity else 1,
                        "sequence": max_seq + 10,
                    }
                )
            )
            return {
                "success": True,
                "item_id": sub_item.id,
                "description": sub_item.description,
                "quantity": sub_item.quantity,
            }
        except Exception as e:
            _logger.error(f"Erreur lors de l'ajout de sous-item: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @http.route(
        ["/my/firebase/uo/<int:uo_id>/vehicle/<int:vehicle_id>/sync"], type="json", auth="user", methods=["POST"]
    )
    def my_firebase_vehicle_sync(self, uo_id, vehicle_id, **kw):
        """Synchroniser tout le véhicule avec Firebase"""
        user = request.env.user
        partner = user.partner_id

        uo = request.env["res.partner"].sudo().browse(uo_id)
        if not uo.exists() or uo not in partner.uo_ids:
            return {"success": False, "error": "Accès refusé"}

        rel_id = (
            request.env["pompier.uo.rel"]
            .sudo()
            .search([("uo_id", "=", uo.id), ("pompier_id", "=", partner.id)], limit=1)
        )
        if not rel_id.exists() or not rel_id.uo_admin:
            raise AccessError(_("Vous n'avez pas les permissions pour gérer cette UO"))

        vehicle = request.env["firebase.vehicle"].sudo().browse(vehicle_id)
        if not vehicle.exists() or vehicle.uo_id != uo:
            return {"success": False, "error": "Accès refusé"}

        try:
            _logger.info(f"=== DÉBUT SYNCHRONISATION VÉHICULE {vehicle_id} ===")
            _logger.info(f"Véhicule firebase_uid: {vehicle.firebase_uid}")
            _logger.info(f"Nombre de catégories: {len(vehicle.product_ids)}")

            # VERROUILLER le véhicule pour éviter les conflits avec le cron
            vehicle.lock_for_sync()

            # Synchroniser le véhicule
            self._sync_vehicle_to_firebase(vehicle.uo_id.name, vehicle)

            # Vérifier que toutes les catégories ont au moins un item
            categories_vides = []
            for product in vehicle.product_ids:
                if not product.item_ids:
                    categories_vides.append(product.label)

            if categories_vides:
                error_msg = f"Les catégories suivantes n'ont pas d'équipement : {', '.join(categories_vides)}. Veuillez ajouter au moins un équipement dans chaque catégorie avant de synchroniser."
                _logger.error(error_msg)
                return {"success": False, "error": error_msg}

            # Synchroniser toutes les catégories et items (directs + groupes)
            for product in vehicle.product_ids:
                _logger.info(f"Sync catégorie: {product.label} (ID: {product.id})")
                self._sync_product_to_firebase(vehicle.uo_id.uo_code, vehicle, product)
                # Seuls les items de premier niveau (sans parent)
                top_level_items = product.item_ids.filtered(lambda i: not i.parent_item_id).sorted(
                    key=lambda i: i.sequence
                )
                for item in top_level_items:
                    _logger.info(f"  Sync item: {item.description} (ID: {item.id})")
                    self._sync_item_to_firebase(vehicle.uo_id.uo_code, vehicle, product, item)

            # DÉVERROUILLER le véhicule après synchronisation
            vehicle.unlock_sync()

            _logger.info(f"=== FIN SYNCHRONISATION VÉHICULE {vehicle_id} ===")
            return {"success": True, "message": "Synchronisation réussie"}
        except Exception as e:
            # DÉVERROUILLER même en cas d'erreur
            vehicle.unlock_sync()
            _logger.error(f"Erreur lors de la synchronisation Firebase: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _sync_product_to_firebase(self, unit_operation, vehicle, product):
        """Synchroniser une catégorie vers Firebase"""
        _logger.info(f"=== DÉBUT SYNC CATÉGORIE {product.id} ({product.label}) ===")
        try:
            # Vérifier si db est disponible
            if db is None:
                _logger.error("Firebase Admin SDK non disponible (db is None)")
                return

            # Récupérer le connecteur Firebase
            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                _logger.error("Aucun connecteur Firebase configuré")
                return

            _logger.info(f"Connecteur trouvé: {connector.id}")

            # Obtenir l'app Firebase
            app = connector._get_firebase_app()
            _logger.info(f"Firebase app obtenue: {app}")

            # Utiliser la séquence comme index dans Firebase
            products_sorted = vehicle.product_ids.sorted(key=lambda p: p.sequence)
            product_index = None
            for idx, p in enumerate(products_sorted):
                if p.id == product.id:
                    product_index = idx
                    break

            if product_index is None:
                _logger.error(f"Impossible de trouver l'index du produit {product.id}")
                return

            firebase_path = f"{unit_operation}/vehicle/{vehicle.firebase_uid}/product/{product_index}"
            _logger.info(f"Chemin Firebase: {firebase_path}")

            db_ref = db.reference(firebase_path, app=app)

            # Préparer les données
            product_data = {
                "id": product.firebase_uid or str(product.id),
                "label": product.label,
            }

            _logger.info(f"Données à envoyer: {product_data}")

            # Mettre à jour Firebase
            db_ref.set(product_data)
            _logger.info("db_ref.set() exécuté avec succès")

            # Sauvegarder le firebase_uid si nouveau
            if not product.firebase_uid:
                product.sudo().write({"firebase_uid": str(product.id)})
                _logger.info(f"firebase_uid sauvegardé: {product.id}")

            _logger.info(f"=== Catégorie {product.label} synchronisée avec Firebase ===")
        except Exception as e:
            _logger.error(f"=== ERREUR sync Firebase catégorie: {e} ===", exc_info=True)

    def _delete_product_from_firebase(self, unit_operation, vehicle, product):
        """Supprimer une catégorie de Firebase"""
        try:
            if not product.firebase_uid:
                return

            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                return

            app = connector._get_firebase_app()
            db_ref = db.reference(
                f"{unit_operation}/vehicle/{vehicle.firebase_uid}/product/{product.firebase_uid}", app=app
            )
            db_ref.delete()

            _logger.info(f"Catégorie {product.label} supprimée de Firebase")
        except Exception as e:
            _logger.error(f"Erreur suppression Firebase catégorie: {e}", exc_info=True)

    def _sync_item_to_firebase(self, unit_operation, vehicle, product, item):
        """Synchroniser un item (direct ou groupe avec sous-items) vers Firebase"""
        _logger.info(f"=== DÉBUT SYNC ITEM {item.id} ({item.description}) ===")
        try:
            if db is None:
                _logger.error("Firebase Admin SDK non disponible (db is None)")
                return

            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                _logger.error("Aucun connecteur Firebase configuré")
                return

            app = connector._get_firebase_app()

            # Trouver l'index du produit
            products_sorted = vehicle.product_ids.sorted(key=lambda p: p.sequence)
            product_index = None
            for idx, p in enumerate(products_sorted):
                if p.id == product.id:
                    product_index = idx
                    break

            if product_index is None:
                _logger.error(f"Impossible de trouver l'index du produit {product.id}")
                return

            # Trouver l'index de l'item parmi les items de premier niveau
            top_level_items = product.item_ids.filtered(lambda i: not i.parent_item_id).sorted(key=lambda i: i.sequence)
            item_index = None
            for idx, it in enumerate(top_level_items):
                if it.id == item.id:
                    item_index = idx
                    break

            if item_index is None:
                _logger.error(f"Impossible de trouver l'index de l'item {item.id}")
                return

            firebase_path = (
                f"{unit_operation}/vehicle/{vehicle.firebase_uid}/product/{product_index}/items/{item_index}"
            )
            db_ref = db.reference(firebase_path, app=app)

            if item.is_group:
                # Format groupe : { id, label, sub_items: [...] }
                sub_items_data = []
                for sub in item.sub_item_ids.sorted(key=lambda s: s.sequence):
                    sub_items_data.append(
                        {
                            "id": sub.firebase_uid or str(sub.id),
                            "description": sub.description,
                            "quantity": sub.quantity,
                        }
                    )
                    if not sub.firebase_uid:
                        sub.sudo().write({"firebase_uid": str(sub.id)})
                item_data = {
                    "id": item.firebase_uid or str(item.id),
                    "label": item.group_label or item.description,
                    "sub_items": sub_items_data,
                }
            else:
                # Format item direct : { id, description, quantity }
                item_data = {
                    "id": item.firebase_uid or str(item.id),
                    "description": item.description,
                    "quantity": item.quantity,
                }

            _logger.info(f"Données à envoyer: {item_data}")
            db_ref.set(item_data)

            if not item.firebase_uid:
                item.sudo().write({"firebase_uid": str(item.id)})
                _logger.info(f"firebase_uid sauvegardé: {item.id}")

            _logger.info(f"=== Item {item.description} synchronisé avec Firebase ===")
        except Exception as e:
            _logger.error(f"=== ERREUR sync Firebase item: {e} ===", exc_info=True)

    def _delete_item_from_firebase(self, unit_operation, vehicle, product, item):
        """Supprimer un item de Firebase"""
        try:
            if not item.firebase_uid:
                return

            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                return

            app = connector._get_firebase_app()
            db_ref = db.reference(
                f"{unit_operation}/vehicle/{vehicle.firebase_uid}/product/{product.firebase_uid or product.id}/items/{item.firebase_uid}",
                app=app,
            )
            db_ref.delete()

            _logger.info(f"Item {item.description} supprimé de Firebase")
        except Exception as e:
            _logger.error(f"Erreur suppression Firebase item: {e}", exc_info=True)

    def _sync_vehicle_to_firebase(self, unit_operation, vehicle):
        """Synchroniser un véhicule vers Firebase

        Returns:
            bool: True si la synchronisation a réussi, False sinon
        """
        try:
            if not vehicle.firebase_uid:
                _logger.warning(f"Véhicule {vehicle.id} n'a pas de firebase_uid")
                return False

            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                _logger.warning("Aucun connecteur Firebase configuré")
                return False

            if not db:
                _logger.warning("Firebase Admin SDK non disponible")
                return False

            app = connector._get_firebase_app()
            db_ref = db.reference(f"unitOperation/{unit_operation}/vehicle/{vehicle.firebase_uid}", app=app)

            vehicle_data = {
                "id": vehicle.firebase_uid,
                "label": vehicle.label or "",
                "licensePlate": vehicle.license_plate or "",
                "vehicleId": vehicle.vehicle_id or vehicle.firebase_uid,
                "status": vehicle.status or "nothing",
                "notes": vehicle.notes or "",
                "verified": vehicle.verified or False,
                "position": vehicle.position or 0,
                "product": [],  # Initialiser avec un tableau vide pour les produits
            }

            db_ref.set(vehicle_data)

            _logger.info(f"✅ Véhicule {vehicle.label} synchronisé avec Firebase")
            return True

        except Exception as e:
            _logger.error(f"❌ Erreur sync Firebase véhicule: {e}", exc_info=True)
            return False

    def _add_uo_to_pompier_firebase(self, pompier, uo, uo_admin=False, uo_pharmacist=False):
        """Ajouter une UO dans le profil Firestore d'un pompier (après invitation).

        Le champ 'uo' dans Firestore peut être une liste (array) ou un dict (map),
        selon la façon dont il a été créé depuis l'application mobile.
        Cette méthode gère les deux cas.
        """
        if not pompier.firebase_uid:
            _logger.warning(
                f"⚠️ Le pompier {pompier.name} n'a pas de firebase_uid, impossible de synchroniser vers Firebase"
            )
            return False

        if not uo.uo_code:
            _logger.warning(f"⚠️ L'UO {uo.name} n'a pas de uo_code, impossible de synchroniser vers Firebase")
            return False

        connector = request.env["firebase.connector"].sudo().search([], limit=1)
        if not connector:
            _logger.warning("Aucun connecteur Firebase configuré")
            return False

        if not firestore:
            _logger.warning("Firestore non disponible")
            return False

        try:
            app = connector._get_firebase_app()
            firestore_client = firestore.client(app=app)

            user_doc_ref = firestore_client.collection("users").document(pompier.firebase_uid)
            user_doc = user_doc_ref.get()

            new_uo_data = {
                "last_connection": fields.Datetime.now(),
                "uo_admin": uo_admin,
                "uo_name": uo.uo_code,
                "uo_pharmacist": uo_pharmacist,
                "verified": True,
            }

            if user_doc.exists:
                user_data = user_doc.to_dict()
                existing_uos = user_data.get("uo", None)

                # ── Cas 1 : 'uo' est une liste (array) ──────────────────────
                if isinstance(existing_uos, list):
                    for entry in existing_uos:
                        if isinstance(entry, dict) and entry.get("uo_name") == uo.uo_code:
                            # L'UO est déjà présente dans la liste — on la met à jour
                            # en reconstruisant la liste complète
                            _logger.info(
                                f"✓ UO {uo.uo_code} déjà dans la liste Firestore de {pompier.name}, mise à jour..."
                            )
                            updated_list = [
                                new_uo_data if (isinstance(e, dict) and e.get("uo_name") == uo.uo_code) else e
                                for e in existing_uos
                            ]
                            user_doc_ref.update({"uo": updated_list})
                            return True

                    # L'UO n'est pas encore dans la liste → ArrayUnion
                    user_doc_ref.update({"uo": firestore.ArrayUnion([new_uo_data])})
                    _logger.info(f"✅ UO {uo.uo_code} ajoutée (ArrayUnion) au profil Firestore de {pompier.name}")

                # ── Cas 2 : 'uo' est un dict/map (clés numériques) ──────────
                elif isinstance(existing_uos, dict):
                    for uo_index, uo_data in existing_uos.items():
                        if isinstance(uo_data, dict) and uo_data.get("uo_name") == uo.uo_code:
                            _logger.info(
                                f"✓ UO {uo.uo_code} déjà dans le map Firestore de {pompier.name} (index {uo_index}), mise à jour..."
                            )
                            user_doc_ref.update({f"uo.{uo_index}": new_uo_data})
                            return True

                    # Trouver le prochain index disponible
                    indices = [int(k) for k in existing_uos.keys() if k.isdigit()]
                    next_index = max(indices) + 1 if indices else 0
                    user_doc_ref.update({f"uo.{next_index}": new_uo_data})
                    _logger.info(f"✅ UO {uo.uo_code} ajoutée au map Firestore de {pompier.name} (index {next_index})")

                # ── Cas 3 : le champ 'uo' n'existe pas encore ───────────────
                else:
                    user_doc_ref.update({"uo": [new_uo_data]})
                    _logger.info(f"✅ Champ 'uo' créé (liste) pour {pompier.name} avec UO {uo.uo_code}")

            else:
                # Le document utilisateur n'existe pas — on le crée avec une liste
                user_doc_ref.set({"uo": [new_uo_data]})
                _logger.info(f"✅ Document Firestore créé pour {pompier.name} avec UO {uo.uo_code}")

            return True

        except Exception as e:
            _logger.error(
                f"❌ Erreur lors de l'ajout de l'UO {uo.uo_code} au profil Firebase de {pompier.name}: {e}",
                exc_info=True,
            )
            return False

    def _sync_uo_to_firebase(self, uo):
        """Synchroniser une UO vers Firebase et l'ajouter aux profils des pompiers"""
        try:
            if not uo.uo_code:
                _logger.warning(f"UO {uo.id} n'a pas de code UO (trigramme)")
                return False

            connector = request.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                _logger.warning("Aucun connecteur Firebase configuré")
                return False

            if not db:
                _logger.warning("Firebase Admin SDK non disponible")
                return False

            app = connector._get_firebase_app()

            # 1. Créer la structure de l'UO dans Firebase
            # Path: /{uo_code}
            uo_ref = db.reference(f"{uo.uo_code}", app=app)

            # Vérifier si l'UO existe déjà
            existing_uo = uo_ref.get()

            if existing_uo:
                _logger.info(f"UO {uo.uo_code} existe déjà dans Firebase, mise à jour...")
                # Mettre à jour uniquement certains champs sans écraser toute la structure
                uo_ref.update(
                    {
                        "name": uo.name or "",
                        "verified": uo.uo_verified or False,
                    }
                )
                # Stocker l'ID Firebase (clé du nœud Realtime DB) dans Odoo si pas déjà fait
                if not uo.firebase_uid:
                    uo.sudo().write({"firebase_uid": uo.uo_code})
                    _logger.info(f"✅ firebase_uid '{uo.uo_code}' enregistré dans Odoo pour l'UO {uo.name}")
            else:
                _logger.info(f"Création de l'UO {uo.uo_code} dans Firebase...")
                # Créer la structure complète de l'UO
                uo_data = {
                    "name": uo.name or "",
                    "verified": True,
                    "prefillTheQty": uo.uo_prefill_qty or False,
                    "sendInventoryToAll": uo.uo_send_inventory_to_all or False,
                    "vehicle": {},  # Structure vide pour les véhicules
                    "inventor_history": {},  # Structure vide pour l'historique des inventaires
                    "created_at": fields.Datetime.now().isoformat(),
                }
                uo_ref.set(uo_data)
                # Stocker l'ID Firebase (clé du nœud Realtime DB = uo_code) dans Odoo
                uo.sudo().write({"firebase_uid": uo.uo_code})
                _logger.info(f"✅ firebase_uid '{uo.uo_code}' enregistré dans Odoo pour l'UO {uo.name}")

            # 2. Ajouter l'UO dans le profil Firebase de chaque pompier (créateur + super admins)
            all_pompiers = uo.pompier_ids
            pompiers_with_uid = all_pompiers.filtered(lambda p: p.firebase_uid)

            _logger.info(
                f"📋 Pompiers de l'UO {uo.uo_code}: {len(all_pompiers)} total, {len(pompiers_with_uid)} avec firebase_uid"
            )
            for p in all_pompiers:
                _logger.info(
                    f"   - {p.name}: firebase_uid={p.firebase_uid or 'AUCUN'}, admin={p.admin}, is_uo_admin={p.is_uo_admin}"
                )

            if pompiers_with_uid:
                _logger.info(f"🔄 Ajout de l'UO {uo.uo_code} aux profils de {len(pompiers_with_uid)} pompier(s)")

                # Obtenir le client Firestore
                if not firestore:
                    _logger.warning("Firestore non disponible")
                else:
                    firestore_client = firestore.client(app=app)

                    for pompier in pompiers_with_uid:
                        try:
                            # Déterminer si le pompier est admin de l'UO
                            is_admin = pompier.admin or pompier.is_uo_admin
                            is_pharmacist = pompier.is_pharmacist

                            _logger.info(f"🔍 Vérification UO pour {pompier.name} (uid: {pompier.firebase_uid})")

                            # Path Firestore: users/{firebase_uid} - uo est un champ du document user
                            user_doc_ref = firestore_client.collection("users").document(pompier.firebase_uid)

                            # Récupérer le document utilisateur
                            user_doc = user_doc_ref.get()

                            if user_doc.exists:
                                user_data = user_doc.to_dict()
                                existing_uos = user_data.get("uo", {})

                                # Vérifier si l'UO existe déjà (list ou dict)
                                uo_exists = False
                                if isinstance(existing_uos, list):
                                    uo_exists = any(
                                        isinstance(e, dict) and e.get("uo_name") == uo.uo_code for e in existing_uos
                                    )
                                    if not uo_exists:
                                        new_uo_data = {
                                            "last_connection": fields.Datetime.now(),
                                            "uo_admin": is_admin,
                                            "uo_name": uo.uo_code,
                                            "uo_pharmacist": is_pharmacist,
                                            "verified": True,
                                        }
                                        user_doc_ref.update({"uo": firestore.ArrayUnion([new_uo_data])})
                                        _logger.info(
                                            f"✅ UO {uo.uo_code} ajoutée (ArrayUnion) au profil Firestore de {pompier.name}"
                                        )
                                    else:
                                        _logger.info(f"✓ UO {uo.uo_code} existe déjà pour {pompier.name}")
                                elif isinstance(existing_uos, dict):
                                    for uo_index, uo_data in existing_uos.items():
                                        if isinstance(uo_data, dict) and uo_data.get("uo_name") == uo.uo_code:
                                            uo_exists = True
                                            _logger.info(
                                                f"✓ UO {uo.uo_code} existe déjà pour {pompier.name} (index {uo_index})"
                                            )
                                            break
                                    if not uo_exists:
                                        indices = [int(k) for k in existing_uos.keys() if k.isdigit()]
                                        next_index = max(indices) + 1 if indices else 0
                                        new_uo_data = {
                                            "last_connection": fields.Datetime.now(),
                                            "uo_admin": is_admin,
                                            "uo_name": uo.uo_code,
                                            "uo_pharmacist": is_pharmacist,
                                            "verified": True,
                                        }
                                        user_doc_ref.update({f"uo.{next_index}": new_uo_data})
                                        _logger.info(
                                            f"✅ UO {uo.uo_code} ajoutée au profil Firestore de {pompier.name} (index {next_index})"
                                        )
                                else:
                                    # Champ uo absent ou None → créer comme liste
                                    new_uo_data = {
                                        "last_connection": fields.Datetime.now(),
                                        "uo_admin": is_admin,
                                        "uo_name": uo.uo_code,
                                        "uo_pharmacist": is_pharmacist,
                                        "verified": True,
                                    }
                                    user_doc_ref.update({"uo": [new_uo_data]})
                                    _logger.info(f"✅ Champ 'uo' créé pour {pompier.name} avec UO {uo.uo_code}")
                            else:
                                # Le document utilisateur n'existe pas, le créer avec l'UO
                                _logger.info(f"⚠️ Document utilisateur {pompier.firebase_uid} n'existe pas, création...")

                                new_uo_data = {
                                    "last_connection": fields.Datetime.now(),
                                    "uo_admin": is_admin,
                                    "uo_name": uo.uo_code,
                                    "uo_pharmacist": is_pharmacist,
                                    "verified": True,
                                }

                                user_doc_ref.set({"uo": {"0": new_uo_data}})
                                _logger.info(f"✅ Document utilisateur créé avec UO {uo.uo_code} pour {pompier.name}")

                        except Exception as e:
                            _logger.error(
                                f"❌ Erreur lors de l'ajout de l'UO au profil Firestore de {pompier.name}: {e}",
                                exc_info=True,
                            )
                            # Continue avec les autres pompiers même en cas d'erreur
                            continue
            else:
                _logger.warning(f"⚠️ Aucun pompier avec firebase_uid trouvé pour l'UO {uo.uo_code}")

            _logger.info(f"✅ UO {uo.name} ({uo.uo_code}) synchronisée avec Firebase")
            return True

        except Exception as e:
            _logger.error(f"❌ Erreur sync Firebase UO: {e}", exc_info=True)
            return False
