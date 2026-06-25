# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    """Extension du modèle Partner pour les UO Firebase"""

    _inherit = "res.partner"

    # Identifiant Firebase
    firebase_uid = fields.Char(string="Firebase UID", index=True, copy=False, help="Identifiant unique Firebase")
    is_firebase_uo = fields.Boolean(string="Est une UO Firebase", default=False, index=True)

    # Code UO spécifique
    uo_code = fields.Char(string="Code UO")

    # Relations pompier-UO via table intermédiaire
    pompier_uo_rel_ids = fields.One2many(
        "pompier.uo.rel",
        "uo_id",
        string="Relations Pompiers",
        help="Relations avec les pompiers de cette UO",
    )
    pompier_ids = fields.Many2many(
        "res.partner",
        string="Pompiers",
        compute="_compute_pompier_ids",
        store=False,
        help="Liste des pompiers appartenant à cette UO",
    )
    pompier_count = fields.Integer(string="Nombre de pompiers", compute="_compute_pompier_count")

    # Relations UO (pour les pompiers) - via table intermédiaire
    pompier_rel_ids = fields.One2many(
        "pompier.uo.rel",
        "pompier_id",
        string="Relations UO",
        help="Relations avec les UO de ce pompier",
    )
    uo_ids = fields.Many2many(
        "res.partner",
        string="Unités Organisationnelles",
        compute="_compute_uo_ids",
        store=False,
        help="Liste des UO auxquelles ce pompier appartient",
    )
    uo_names = fields.Char(string="Noms UO", compute="_compute_uo_names", store=True)
    is_firebase_pompier = fields.Boolean(string="Est un Pompier Firebase", default=False, index=True)
    multiple_uo = fields.Boolean(string="Plusieurs UO", compute="_compute_multiple_uo", store=True)
    admin = fields.Boolean(string="Super Administrateur", default=False)

    # Informations pompier
    matricule = fields.Char(string="Matricule")
    grade = fields.Char(string="Grade")
    fonction = fields.Char(string="Fonction")
    date_entree = fields.Date(string="Date d'entrée")
    date_sortie = fields.Date(string="Date de sortie")
    pompier_status = fields.Selection(
        [("active", "Actif"), ("inactive", "Inactif"), ("suspended", "Suspendu")],
        string="Statut Pompier",
        default="active",
    )

    # Champs calculés depuis les relations (dépréciés - à utiliser les relations directes)
    is_uo_admin = fields.Boolean(
        string="Administrateur d'au moins une UO",
        compute="_compute_uo_roles",
        store=True,
        help="Le pompier est administrateur d'au moins une UO",
    )
    is_pharmacist = fields.Boolean(
        string="Pharmacien d'au moins une UO",
        compute="_compute_uo_roles",
        store=True,
        help="Le pompier est pharmacien d'au moins une UO",
    )

    # Paramètres UO (depuis Realtime Database)
    uo_verified = fields.Boolean(string="UO Vérifiée", default=False, help="L'UO est vérifiée dans Firebase")
    uo_prefill_qty = fields.Boolean(
        string="Pré-remplir les quantités", default=False, help="prefillTheQty depuis Firebase"
    )
    uo_send_inventory_to_all = fields.Boolean(
        string="Envoyer l'inventaire à tous", default=False, help="sendInventoryToAll depuis Firebase"
    )

    # Relations avec les inventaires et véhicules (pour les UO uniquement)
    inventory_history_ids = fields.One2many(
        "firebase.inventory.history",
        "uo_id",
        string="Historique des inventaires",
        help="Historique des inventaires de cette UO",
    )
    inventory_count = fields.Integer(string="Nombre d'inventaires", compute="_compute_inventory_count")

    vehicle_ids = fields.One2many(
        "firebase.vehicle", "uo_id", string="Véhicules", help="Liste des véhicules de cette UO"
    )
    vehicle_count = fields.Integer(string="Nombre de véhicules", compute="_compute_vehicle_count")

    subscription_tier = fields.Selection(
        [
            ("discovery", "Découverte - Gratuit"),
            ("essential", "Essentiel - 5€/mois"),
            ("caserne", "Caserne - 10€/mois"),
            ("flotte", "Flotte - 15€/mois"),
        ],
        string="Palier d'abonnement",
        help="Le palier d'abonnement choisi par l'UO",
    )

    # Statut d'activation (ce qui est poussé dans Firebase)
    activation_status = fields.Selection(
        [
            ("discovery", "discovery"),
            ("essential", "essential"),
            ("caserne", "caserne"),
            ("flotte", "flotte"),
        ],
        string="Statut d'activation Firebase",
        default="discovery",
        help="Statut envoyé vers Firebase pour contrôler les accès",
    )

    # Limites selon le palier
    max_vehicles = fields.Integer(string="Max. Véhicules", compute="_compute_subscription_limits", store=True)
    max_users = fields.Integer(string="Max. Utilisateurs", compute="_compute_subscription_limits", store=True)

    # Relation avec la commande d'abonnement active
    subscription_sale_order_id = fields.Many2one(
        "sale.order",
        string="Commande d'abonnement active",
        help="La commande d'abonnement active pour cette UO",
        domain=[("is_firebase_subscription", "=", True)],
    )

    # Relation avec toutes les commandes
    sale_order_ids = fields.One2many(
        "sale.order",
        "partner_id",
        string="Toutes les commandes",
        help="Toutes les commandes pour cette UO",
    )

    # Email de facturation (peut être différent de l'email principal)
    billing_email = fields.Char(
        string="Email de facturation",
        help="Email où seront envoyées les factures. Si vide, utilise l'email principal.",
    )
    billing_email_verified = fields.Boolean(
        string="Email de facturation vérifié",
        default=False,
        help="Indique si l'email de facturation a été vérifié",
    )

    # Informations de paiement
    last_payment_date = fields.Date(string="Dernier paiement")
    last_payment_amount = fields.Float(string="Montant dernier paiement")

    # Authentification Firebase (pour pompiers)
    last_login = fields.Datetime(string="Dernière connexion")

    # Métadonnées Firebase
    firebase_created_at = fields.Datetime(string="Créé sur Firebase")
    firebase_updated_at = fields.Datetime(string="Mis à jour sur Firebase")

    # Synchronisation
    last_sync_date = fields.Datetime(string="Dernière synchronisation")
    sync_status = fields.Selection(
        [
            ("synced", "Synchronisé"),
            ("pending", "En attente"),
            ("error", "Erreur"),
        ],
        string="Statut de synchronisation",
        default="pending",
    )
    sync_error = fields.Text(string="Erreur de synchronisation")

    # Données JSON brutes de Firebase
    firebase_data = fields.Text(string="Données Firebase (JSON)")

    _sql_constraints = [("firebase_uid_unique", "unique(firebase_uid)", "L'UID Firebase doit être unique!")]

    @api.depends("pompier_uo_rel_ids")
    def _compute_pompier_ids(self):
        """Calcule la liste des pompiers depuis les relations"""
        for record in self:
            if record.is_firebase_uo:
                record.pompier_ids = record.pompier_uo_rel_ids.mapped("pompier_id")
            else:
                record.pompier_ids = False

    @api.depends("pompier_rel_ids")
    def _compute_uo_ids(self):
        """Calcule la liste des UO depuis les relations"""
        for record in self:
            if record.is_firebase_pompier:
                record.uo_ids = record.pompier_rel_ids.mapped("uo_id")
            else:
                record.uo_ids = False

    @api.depends("pompier_rel_ids.uo_admin", "pompier_rel_ids.uo_pharmacist")
    def _compute_uo_roles(self):
        """Calcule si le pompier a des rôles dans au moins une UO"""
        for record in self:
            if record.is_firebase_pompier:
                record.is_uo_admin = any(rel.uo_admin for rel in record.pompier_rel_ids)
                record.is_pharmacist = any(rel.uo_pharmacist for rel in record.pompier_rel_ids)
            else:
                record.is_uo_admin = False
                record.is_pharmacist = False

    @api.depends("pompier_uo_rel_ids")
    def _compute_pompier_count(self):
        for record in self:
            if record.is_firebase_uo:
                pompier = record.pompier_uo_rel_ids
                record.pompier_count = len(list(filter(lambda r: not r.pompier_id.admin and r.verified, pompier)))
            else:
                record.pompier_count = 0

    @api.depends("inventory_history_ids")
    def _compute_inventory_count(self):
        for record in self:
            if record.is_firebase_uo:
                record.inventory_count = len(record.inventory_history_ids)
            else:
                record.inventory_count = 0

    @api.depends("vehicle_ids")
    def _compute_vehicle_count(self):
        for record in self:
            if record.is_firebase_uo:
                record.vehicle_count = len(record.vehicle_ids)
            else:
                record.vehicle_count = 0

    @api.depends("pompier_rel_ids")
    def _compute_uo_names(self):
        for record in self:
            if record.is_firebase_pompier and record.pompier_rel_ids:
                record.uo_names = ", ".join(record.pompier_rel_ids.mapped("uo_id.name"))
            else:
                record.uo_names = ""

    @api.depends("pompier_rel_ids")
    def _compute_multiple_uo(self):
        for record in self:
            record.multiple_uo = len(record.pompier_rel_ids) > 1

    @api.depends("subscription_tier")
    def _compute_subscription_limits(self):
        """Calcule les limites selon le palier d'abonnement choisi"""
        tier_config = {
            "essential": {"max_vehicles": 3, "max_users": 15},
            "caserne": {"max_vehicles": 10, "max_users": 30},
            "flotte": {"max_vehicles": 30, "max_users": -1},  # Illimité
        }

        for record in self:
            if record.is_firebase_uo and record.subscription_tier and record.subscription_tier != "discovery":
                config = tier_config.get(record.subscription_tier, tier_config["essential"])
                record.max_vehicles = config["max_vehicles"]
                record.max_users = config["max_users"]
            else:
                record.max_vehicles = 1
                record.max_users = 1

    def check_vehicle_limit(self):
        """Vérifie si l'UO peut ajouter un nouveau véhicule selon son abonnement"""
        self.ensure_one()
        if not self.is_firebase_uo:
            return True
        max_v = self.max_vehicles
        if max_v == -1:
            return True  # Illimité
        current_count = len(self.vehicle_ids)
        if current_count >= max_v:
            tier_label = dict(self._fields["subscription_tier"].selection).get(self.subscription_tier, "actuel")
            raise ValidationError(
                f"Votre abonnement {tier_label} est limité à {max_v} véhicule(s). "
                f"Vous en avez déjà {current_count}. "
                "Veuillez passer à un abonnement supérieur pour en ajouter davantage."
            )
        return True

    def check_user_limit(self):
        """Vérifie si l'UO peut ajouter un nouveau pompier selon son abonnement"""
        self.ensure_one()
        if not self.is_firebase_uo:
            return True
        max_u = self.max_users
        if max_u == -1:
            return True  # Illimité
        current_count = self.pompier_count
        if current_count >= max_u:
            tier_label = dict(self._fields["subscription_tier"].selection).get(self.subscription_tier, "actuel")
            raise ValidationError(
                f"Votre abonnement {tier_label} est limité à {max_u} utilisateur(s). "
                f"Vous en avez déjà {current_count}. "
                "Veuillez passer à un abonnement supérieur pour en ajouter davantage."
            )
        return True

    def activate_subscription(self, tier, sale_order_id=None):
        """Active l'abonnement de l'UO et pousse vers Firebase

        Args:
            tier: str - 'essential', 'caserne' ou 'flotte'
            sale_order_id: int - ID de la commande d'abonnement (optionnel)
        """
        self.ensure_one()

        # Mapping tier -> activation_status Firebase
        tier_to_firebase = {
            "essential": "essential",
            "caserne": "caserne",
            "flotte": "flotte",
            "discovery": "discovery",
        }

        # Limites selon le palier
        tier_limits = {
            "essential": {"limit_user": 15, "limit_vehicle": 3},
            "caserne": {"limit_user": 30, "limit_vehicle": 10},
            "flotte": {"limit_user": -1, "limit_vehicle": 30},
            "discovery": {"limit_user": 1, "limit_vehicle": 1},
        }

        limits = tier_limits.get(tier, tier_limits["discovery"])
        activation_status = tier_to_firebase.get(tier, "discovery")

        vals = {
            "subscription_tier": tier,
            "activation_status": activation_status,
            "max_users": limits["limit_user"],
            "max_vehicles": limits["limit_vehicle"],
        }
        if sale_order_id:
            vals["subscription_sale_order_id"] = sale_order_id

        self.write(vals)

        # Pousser vers Firebase
        self._push_subscription_to_firebase()

        _logger.info(f"✅ UO {self.name} activée avec le palier {tier} (Firebase: {activation_status})")

    def _push_subscription_to_firebase(self):
        """Pousse les données d'abonnement vers Firebase Realtime Database

        Structure Firebase attendue dans /{uo_code}/:
            activation_status: "essential" | "caserne" | "flotte" | "discovery"
            limit_user: int (-1 = illimité)
            limit_vehicle: int
        """
        self.ensure_one()
        if not self.is_firebase_uo or not self.uo_code:
            return False

        try:
            from firebase_admin import db as fb_db
        except ImportError:
            _logger.warning("Firebase Admin SDK non disponible")
            return False

        try:
            connector = self.env["firebase.connector"].sudo().search([], limit=1)
            if not connector:
                _logger.warning("Aucun connecteur Firebase configuré")
                return False

            app = connector._get_firebase_app()

            # Limites à pousser
            limit_user = self.max_users if self.max_users != 0 else 1
            limit_vehicle = self.max_vehicles if self.max_vehicles != 0 else 1
            activation_status = self.activation_status or "discovery"

            uo_ref = fb_db.reference(self.uo_code, app=app)
            uo_ref.update(
                {
                    "activation_status": activation_status,
                    "limit_user": limit_user,
                    "limit_vehicle": limit_vehicle,
                }
            )

            _logger.info(
                f"✅ Abonnement UO {self.uo_code} poussé vers Firebase: "
                f"status={activation_status}, limit_user={limit_user}, limit_vehicle={limit_vehicle}"
            )
            return True

        except Exception as e:
            _logger.error(f"❌ Erreur push abonnement Firebase pour {self.uo_code}: {e}", exc_info=True)
            return False

    @api.model
    def cron_sync_subscriptions_to_firebase(self):
        """Cron: synchronise les abonnements actifs vers Firebase"""
        uo_list = self.search([("is_firebase_uo", "=", True), ("uo_code", "!=", False)])
        success = 0
        errors = 0
        for uo in uo_list:
            try:
                uo._push_subscription_to_firebase()
                success += 1
            except Exception as e:
                _logger.error(f"Erreur cron sync abonnement UO {uo.name}: {e}")
                errors += 1
        _logger.info(f"Cron sync abonnements Firebase: {success} OK, {errors} erreurs")
        return True

    def action_view_pompiers(self):
        """Action pour voir les pompiers de cette UO"""
        self.ensure_one()

        list_view = self.env.ref("inventory_fireman.view_partner_pompier_list", raise_if_not_found=False)
        form_view = self.env.ref("inventory_fireman.view_partner_pompier_form", raise_if_not_found=False)
        pompiers = self.pompier_uo_rel_ids.mapped("pompier_id").filtered(lambda r: not r.admin)

        action = {
            "name": f"Pompiers - {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "res.partner",
            "view_mode": "list,form",
            "domain": [("id", "in", pompiers.ids)],
            "context": {"default_uo_ids": [(6, 0, [self.id])], "default_is_firebase_pompier": True},
        }

        # Ajouter les vues spécifiques si elles existent
        if list_view and form_view:
            action["views"] = [(list_view.id, "list"), (form_view.id, "form")]

        return action

    def action_view_all_uo(self):
        """Action pour voir toutes les UO de ce pompier"""
        self.ensure_one()
        if self.is_firebase_pompier and self.uo_ids:
            return {
                "name": "Unités Organisationnelles",
                "type": "ir.actions.act_window",
                "res_model": "res.partner",
                "view_mode": "list,form",
                "domain": [("id", "in", self.uo_ids.ids), ("is_firebase_uo", "=", True)],
            }

    def action_view_inventories(self):
        """Action pour voir l'historique des inventaires de cette UO"""
        self.ensure_one()
        return {
            "name": f"Inventaires - {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "firebase.inventory.history",
            "view_mode": "list,form",
            "domain": [("uo_id", "=", self.id)],
            "context": {"default_uo_id": self.id},
        }

    def action_view_vehicles(self):
        """Action pour voir les véhicules de cette UO"""
        self.ensure_one()
        return {
            "name": f"Véhicules - {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "firebase.vehicle",
            "view_mode": "list,form",
            "domain": [("uo_id", "=", self.id)],
            "context": {"default_uo_id": self.id},
        }

    def action_sync_from_firebase(self):
        """Synchroniser manuellement depuis Firebase"""
        firebase_service = self.env["firebase.connector"]._get_default()
        if not firebase_service:
            _logger.warning("Aucun connecteur Firebase configuré : synchronisation ignorée.")
            return
        for record in self:
            if not record.firebase_uid:
                continue
            try:
                if record.is_firebase_uo:
                    firebase_service.sync_single_uo(record.firebase_uid)
                elif record.is_firebase_pompier:
                    firebase_service.sync_single_pompier(record.firebase_uid)
                record.write({"sync_status": "synced", "sync_error": False, "last_sync_date": fields.Datetime.now()})
            except Exception as e:
                _logger.error(f"Erreur lors de la synchronisation de {record.firebase_uid}: {str(e)}")
                record.write({"sync_status": "error", "sync_error": str(e)})

    def _check_and_activate_subscriptions(self):
        """
        Méthode automatique appelée par cron pour vérifier les paiements
        et activer les UO dont les factures sont payées ou commandes confirmées
        """
        # Produits d'abonnement et leur palier correspondant
        subscription_refs = {
            "inventory_fireman.product_subscription_essentiel": "essential",
            "inventory_fireman.product_subscription_caserne": "caserne",
            "inventory_fireman.product_subscription_flotte": "flotte",
        }

        # Construire le mapping produit_id -> tier
        product_tier_map = {}
        for ref_str, tier in subscription_refs.items():
            product = self.env.ref(ref_str, raise_if_not_found=False)
            if product:
                product_tier_map[product.id] = tier

        if not product_tier_map:
            _logger.warning("Aucun produit d'abonnement Firebase trouvé")
            return

        uo_list = self.search([("is_firebase_uo", "=", True)])

        for uo in uo_list:
            for order in uo.sale_order_ids.filtered(lambda o: o.state not in ["cancel", "draft"]):
                # Identifier le palier depuis le produit de la commande
                tier = None
                for line in order.order_line:
                    t = product_tier_map.get(line.product_id.id)
                    if t:
                        tier = t
                        break

                if not tier:
                    continue

                # Si la commande est confirmée mais n'a pas encore de facture, en créer une
                if order.state == "sale" and order.invoice_status == "to invoice":
                    try:
                        order._create_invoices()
                        _logger.info(f"Facture créée automatiquement pour la commande {order.name}")
                    except Exception as e:
                        _logger.error(f"Erreur lors de la création de facture pour {order.name}: {e}")

                # Activer si la commande est confirmée (état "sale") et que l'UO n'a pas encore ce palier
                if order.state == "sale" and uo.subscription_tier != tier:
                    try:
                        uo.activate_subscription(tier, sale_order_id=order.id)
                        uo.write(
                            {
                                "last_payment_date": order.date_order,
                                "last_payment_amount": order.amount_total,
                            }
                        )
                        uo.message_post(
                            body=(
                                f"Abonnement <strong>{tier}</strong> activé automatiquement "
                                f"suite à la confirmation de la commande {order.name}."
                            ),
                            message_type="notification",
                            subtype_xmlid="mail.mt_note",
                        )
                        _logger.info(f"UO {uo.name} activée automatiquement avec le palier {tier}")
                    except Exception as e:
                        _logger.error(f"Erreur activation UO {uo.name}: {e}")
                    break
