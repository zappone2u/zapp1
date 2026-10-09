# -*- coding: utf-8 -*-
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .constants import (
    ACTIVE_SUBSCRIPTION_STATES,
    DISCOVERY_MAX_MEMBERS,
    DISCOVERY_MAX_VEHICLES,
    EXPORT_TIERS,
    PAID_TIERS,
    REPORTING_TIERS,
    TIER_DISCOVERY,
    TIERS,
    UNLIMITED,
)
from .utils import QuotaExceeded

_logger = logging.getLogger(__name__)

# Caractères interdits dans une clé Firebase Realtime Database (+ espaces)
FORBIDDEN_CODE_CHARS = re.compile(r"[.$#\[\]/\s]")


class FiremanUo(models.Model):
    """Unité opérationnelle (caserne). Son code est la clé du noeud Firebase."""

    _name = "fireman.uo"
    _description = "Unité opérationnelle"
    _inherit = ["fireman.sync.mixin", "mail.thread"]
    _order = "code"
    _rec_names_search = ["name", "code"]
    _push_fields = ("name", "verified", "prefill_qty", "send_inventory_to_all", "partner_id")

    name = fields.Char(string="Nom", required=True, tracking=True)
    code = fields.Char(string="Trigramme", required=True, index=True, copy=False, tracking=True)
    active = fields.Boolean(default=True)
    verified = fields.Boolean(
        string="Validée", default=True, tracking=True, help="Validation par un super administrateur."
    )
    prefill_qty = fields.Boolean(string="Pré-remplir les quantités")
    send_inventory_to_all = fields.Boolean(string="Envoyer l'inventaire à tous")
    last_pull_date = fields.Datetime(string="Dernière lecture Firebase", readonly=True, copy=False)

    partner_id = fields.Many2one(
        "res.partner", string="Contact de facturation", required=True, copy=False, ondelete="restrict"
    )
    billing_email = fields.Char(related="partner_id.email", readonly=False, string="Email de facturation")

    member_ids = fields.One2many("fireman.member", "uo_id", string="Membres")
    vehicle_ids = fields.One2many("fireman.vehicle", "uo_id", string="Véhicules")
    inventory_ids = fields.One2many("fireman.inventory", "uo_id", string="Inventaires")
    order_ids = fields.One2many("sale.order", "fireman_uo_id", string="Abonnements")

    member_count = fields.Integer(string="Membres (décomptés)", compute="_compute_counts")
    vehicle_count = fields.Integer(string="Nombre de véhicules", compute="_compute_counts")
    inventory_count = fields.Integer(string="Nombre d'inventaires", compute="_compute_counts")

    # Abonnement : tout est déduit de la commande d'abonnement en cours
    subscription_id = fields.Many2one("sale.order", string="Abonnement en cours", compute="_compute_subscription")
    tier = fields.Selection(TIERS, string="Palier", compute="_compute_subscription")
    max_vehicles = fields.Integer(string="Véhicules max.", compute="_compute_subscription")
    max_members = fields.Integer(string="Membres max.", compute="_compute_subscription")
    has_reporting = fields.Boolean(compute="_compute_subscription")
    has_export = fields.Boolean(compute="_compute_subscription")

    _sql_constraints = [("code_unique", "unique(code)", "Ce trigramme est déjà utilisé.")]

    # ------------------------------------------------------------------
    # Champs calculés
    # ------------------------------------------------------------------
    @api.depends("code", "name")
    def _compute_display_name(self):
        for uo in self:
            uo.display_name = f"[{uo.code}] {uo.name}" if uo.code else uo.name

    def _billable_members(self):
        """Membres décomptés dans la limite d'utilisateurs : vérifiés, hors super administrateurs."""
        self.ensure_one()
        return self.member_ids.filtered(lambda m: m.verified and not m.partner_id.fireman_super_admin)

    @api.depends("vehicle_ids", "inventory_ids", "member_ids.verified", "member_ids.partner_id.fireman_super_admin")
    def _compute_counts(self):
        for uo in self:
            uo.vehicle_count = len(uo.vehicle_ids)
            uo.inventory_count = len(uo.inventory_ids)
            uo.member_count = len(uo._billable_members())

    @api.depends(
        "order_ids.state",
        "order_ids.subscription_state",
        "order_ids.order_line.product_id",
    )
    def _compute_subscription(self):
        for uo in self:
            active = uo.order_ids.filtered(
                lambda o: o.state == "sale" and o.subscription_state in ACTIVE_SUBSCRIPTION_STATES
            ).sorted("id")[-1:]
            product = active.order_line.product_id.product_tmpl_id.filtered("fireman_tier")[:1]
            tier = product.fireman_tier or TIER_DISCOVERY
            uo.subscription_id = active if product else False
            uo.tier = tier
            uo.max_vehicles = product.fireman_max_vehicles if product else DISCOVERY_MAX_VEHICLES
            uo.max_members = product.fireman_max_members if product else DISCOVERY_MAX_MEMBERS
            uo.has_reporting = tier in REPORTING_TIERS
            uo.has_export = tier in EXPORT_TIERS

    # ------------------------------------------------------------------
    # Contraintes et quotas
    # ------------------------------------------------------------------
    @api.constrains("code")
    def _check_code(self):
        for uo in self:
            if not uo.code or FORBIDDEN_CODE_CHARS.search(uo.code) or len(uo.code) > 32:
                raise ValidationError(_("Le trigramme ne doit contenir ni espace ni . $ # [ ] /"))

    def _check_vehicle_quota(self, adding=1):
        for uo in self:
            if uo.max_vehicles != UNLIMITED and uo.vehicle_count + adding > uo.max_vehicles:
                raise QuotaExceeded(
                    _(
                        "Le palier %(tier)s de %(uo)s est limité à %(max)s véhicule(s).",
                        tier=dict(TIERS)[uo.tier],
                        uo=uo.name,
                        max=uo.max_vehicles,
                    )
                )

    def _check_member_quota(self, adding=1):
        for uo in self:
            if uo.max_members != UNLIMITED and uo.member_count + adding > uo.max_members:
                raise QuotaExceeded(
                    _(
                        "Le palier %(tier)s de %(uo)s est limité à %(max)s utilisateur(s).",
                        tier=dict(TIERS)[uo.tier],
                        uo=uo.name,
                        max=uo.max_members,
                    )
                )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Partner = self.env["res.partner"].sudo()
        for vals in vals_list:
            vals["code"] = (vals.get("code") or "").strip()
            if not vals.get("partner_id"):
                vals["partner_id"] = Partner.create(
                    {
                        "name": vals.get("name") or vals["code"],
                        "company_type": "company",
                        "email": vals.get("billing_email") or False,
                    }
                ).id
        uos = super().create(vals_list)
        if self._push_enabled():
            uos._join_super_admins()
        return uos

    def write(self, vals):
        if "code" in vals and any(uo.code != vals["code"] for uo in self):
            raise UserError(_("Le trigramme d'une UO ne peut pas être modifié."))
        return super().write(vals)

    def _join_super_admins(self):
        admins = self.env["res.partner"].sudo().search([("fireman_super_admin", "=", True)])
        Member = self.env["fireman.member"].sudo()
        for uo in self:
            for admin in admins - uo.member_ids.partner_id:
                Member.create(
                    {
                        "uo_id": uo.id,
                        "partner_id": admin.id,
                        "is_admin": True,
                        "is_pharmacist": True,
                        "verified": True,
                    }
                )

    # ------------------------------------------------------------------
    # Abonnement
    # ------------------------------------------------------------------
    def _tier_product(self, tier):
        product = self.env["product.template"].sudo().search([("fireman_tier", "=", tier)], limit=1)
        if not product:
            raise UserError(_("Aucun produit n'est configuré pour le palier %s.", tier))
        return product

    def subscribe(self, tier, billing_email=None):
        """Souscrit (ou change) le palier : la commande en cours est clôturée et remplacée
        par un nouvel abonnement mensuel facturé immédiatement."""
        self.ensure_one()
        if tier not in dict(PAID_TIERS):
            raise UserError(_("Palier inconnu."))
        if self.tier == tier:
            raise UserError(_("Cette UO est déjà au palier %s.", dict(TIERS)[tier]))
        product = self._tier_product(tier)
        if billing_email:
            self.partner_id.sudo().email = billing_email
        elif not self.partner_id.email:
            raise UserError(_("Renseignez un email de facturation."))

        plan = self.env.ref("sale_subscription.subscription_plan_month")
        previous = self.subscription_id.sudo()
        order = (
            self.env["sale.order"]
            .sudo()
            .create(
                {
                    "partner_id": self.partner_id.id,
                    "fireman_uo_id": self.id,
                    "plan_id": plan.id,
                    "origin": _("Portail UO %s", self.code),
                    "order_line": [(0, 0, {"product_id": product.product_variant_id.id, "product_uom_qty": 1})],
                }
            )
        )
        order.action_confirm()
        if previous:
            previous.set_close(close_reason_id=self.env.ref("sale_subscription.close_reason_renew").id)
            previous.message_post(body=_("Remplacé par %s (changement de palier).", order.name))
        self.message_post(body=_("Palier %(tier)s souscrit (%(order)s).", tier=dict(TIERS)[tier], order=order.name))
        self._invoice_and_send(order)
        return order

    def _invoice_and_send(self, order):
        """Facture immédiatement le premier mois ; l'envoi par email ne doit pas bloquer la souscription."""
        try:
            with self.env.cr.savepoint():
                invoice = order._create_invoices()
                invoice.action_post()
                invoice._generate_and_send()
        except Exception:
            _logger.exception("Facturation initiale de %s impossible", order.name)

    def cancel_subscription(self, reason):
        self.ensure_one()
        order = self.subscription_id.sudo()
        if not order:
            raise UserError(_("Aucun abonnement en cours."))
        order.set_close(close_reason_id=self.env.ref("sale_subscription.close_reason_cancel").id)
        order.message_post(body=_("Résiliation demandée : %s", reason or "-"))
        self.message_post(body=_("Abonnement %(order)s résilié : %(reason)s", order=order.name, reason=reason or "-"))

    # ------------------------------------------------------------------
    # Synchronisation Firebase
    # ------------------------------------------------------------------
    def _push_target_key(self):
        return self.code

    def _push_uo_code(self):
        return self.code

    def _firebase_payload(self):
        self.ensure_one()
        return {
            "name": self.name,
            "verified": bool(self.verified),
            "prefillTheQty": bool(self.prefill_qty),
            "sendInventoryToAll": bool(self.send_inventory_to_all),
            "activation_status": self.tier,
            "limit_user": self.max_members,
            "limit_vehicle": self.max_vehicles,
        }

    def _push_upsert(self, connector):
        self.ensure_one()
        connector.rtdb(self.code).update(self._firebase_payload())

    def action_pull(self):
        self.ensure_one()
        self.env["fireman.sync.service"].pull_uo(self.code)
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_push(self):
        """Renvoie toute l'UO (paramètres, véhicules, membres) vers Firebase."""
        for uo in self:
            uo._push_enqueue()
            uo.vehicle_ids._push_enqueue()
            uo.member_ids._push_enqueue()
        return True

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def _reporting_data(self, max_lacks=20):
        self.ensure_one()
        inventories = self.inventory_ids
        vehicles = []
        for vehicle in self.vehicle_ids:
            mine = inventories.filtered(lambda i: i.vehicle_name == vehicle.name)
            vehicles.append(
                {
                    "vehicle": vehicle,
                    "total": len(mine),
                    "complete": len(mine.filtered("is_complete")),
                    "last_date": mine[:1].date,
                }
            )
        return {
            "total": len(inventories),
            "complete": len(inventories.filtered("is_complete")),
            "incomplete": len(inventories.filtered(lambda i: not i.is_complete)),
            "lacks": inventories.filtered(lambda i: (i.lack or "").strip())[:max_lacks],
            "vehicles": vehicles,
            "generated_at": fields.Datetime.now(),
        }

    # ------------------------------------------------------------------
    # Actions backend
    # ------------------------------------------------------------------
    def _action_related(self, name, model, field="uo_id"):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": "list,form",
            "domain": [(field, "=", self.id)],
            "context": {f"default_{field}": self.id},
        }

    def action_open_vehicles(self):
        return self._action_related(_("Véhicules"), "fireman.vehicle")

    def action_open_members(self):
        return self._action_related(_("Membres"), "fireman.member")

    def action_open_inventories(self):
        return self._action_related(_("Inventaires"), "fireman.inventory")

    def action_open_orders(self):
        return self._action_related(_("Abonnements"), "sale.order", field="fireman_uo_id")
