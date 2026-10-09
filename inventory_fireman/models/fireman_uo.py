# -*- coding: utf-8 -*-
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .constants import (
    ACTIVE_SUBSCRIPTION_STATES,
    DEFAULT_QUOTA_GRACE_DAYS,
    DISCOVERY_MAX_MEMBERS,
    DISCOVERY_MAX_VEHICLES,
    EXPORT_TIERS,
    PAID_TIERS,
    QUOTA_GRACE_PARAM,
    REPORTING_TIERS,
    TIER_DISCOVERY,
    TIER_RANK,
    TIERS,
    UNLIMITED,
)
from .utils import QuotaExceeded

_logger = logging.getLogger(__name__)

# Caractères interdits dans une clé Firebase Realtime Database (+ espaces)
FORBIDDEN_CODE_CHARS = re.compile(r"[.$#\[\]/\s]")
MAX_CODE_LENGTH = 32


def is_valid_code(code):
    return (
        isinstance(code, str) and bool(code) and len(code) <= MAX_CODE_LENGTH and not FORBIDDEN_CODE_CHARS.search(code)
    )


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

    # Baisse de palier : appliquée à la fin de la période payée
    pending_tier = fields.Selection(PAID_TIERS, string="Palier à venir", copy=False, tracking=True)
    pending_tier_date = fields.Date(string="Date du changement de palier", copy=False)
    # Choix de l'administrateur : ce qui est conservé si les quotas sont dépassés
    keep_vehicle_ids = fields.Many2many(
        "fireman.vehicle",
        "fireman_uo_keep_vehicle_rel",
        "uo_id",
        "vehicle_id",
        string="Véhicules à conserver",
        copy=False,
    )
    keep_member_ids = fields.Many2many(
        "fireman.member", "fireman_uo_keep_member_rel", "uo_id", "member_id", string="Membres à conserver", copy=False
    )
    quota_grace_until = fields.Date(
        string="Archivage automatique le",
        copy=False,
        help="Quotas dépassés : sans choix de l'administrateur, l'excédent est archivé à cette date.",
    )
    vehicle_excess = fields.Integer(string="Véhicules en trop", compute="_compute_excess")
    member_excess = fields.Integer(string="Utilisateurs en trop", compute="_compute_excess")

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
    @api.depends("vehicle_ids", "member_ids.verified", "max_vehicles", "max_members")
    def _compute_excess(self):
        for uo in self:
            uo.vehicle_excess = 0 if uo.max_vehicles == UNLIMITED else max(0, len(uo.vehicle_ids) - uo.max_vehicles)
            uo.member_excess = (
                0 if uo.max_members == UNLIMITED else max(0, len(uo._billable_members()) - uo.max_members)
            )

    @api.constrains("code")
    def _check_code(self):
        for uo in self:
            if not is_valid_code(uo.code):
                raise ValidationError(
                    _(
                        "Trigramme invalide « %(code)s » : il doit faire au plus %(max)s caractères et ne contenir "
                        "ni espace ni . $ # [ ] /",
                        code=uo.code or "",
                        max=MAX_CODE_LENGTH,
                    )
                )

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
        self.write({"pending_tier": False, "pending_tier_date": False})
        self._enforce_quotas()
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
    # Baisse de palier et dépassement des quotas
    #
    # * une baisse de palier est planifiée à la fin de la période payée ;
    # * si l'UO dépasse alors les quotas, l'administrateur choisit ce qu'il conserve ;
    # * sans choix, l'excédent est archivé automatiquement après le délai de grâce (paramétrable) ;
    # * rien n'est supprimé : véhicules archivés, membres repassés « en attente », réactivés automatiquement
    #   si un palier supérieur est souscrit.
    # ------------------------------------------------------------------
    def _quota_grace_days(self):
        value = self.env["ir.config_parameter"].sudo().get_param(QUOTA_GRACE_PARAM, str(DEFAULT_QUOTA_GRACE_DAYS))
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return DEFAULT_QUOTA_GRACE_DAYS

    def _tier_limits(self, tier):
        product = self._tier_product(tier)
        return product.fireman_max_vehicles, product.fireman_max_members

    @staticmethod
    def _excess(records, keep, limit, key):
        """Enregistrements à retirer pour tenir dans `limit` : on garde d'abord le choix de l'administrateur,
        puis on complète selon `key`."""
        if limit == UNLIMITED or len(records) <= limit:
            return records.browse()
        chosen = (records & keep).sorted(key)[:limit]
        rest = (records - chosen).sorted(key)
        chosen |= rest[: limit - len(chosen)]
        return records - chosen

    def _vehicles_over_quota(self):
        self.ensure_one()
        return self._excess(self.vehicle_ids, self.keep_vehicle_ids, self.max_vehicles, lambda v: (v.sequence, v.id))

    def _members_over_quota(self):
        self.ensure_one()
        return self._excess(
            self._billable_members(), self.keep_member_ids, self.max_members, lambda m: (not m.is_admin, m.id)
        )

    def _validated_keep(self, tier, vehicle_ids, member_ids):
        max_vehicles, max_members = self._tier_limits(tier) if tier else (self.max_vehicles, self.max_members)
        vehicle_ids, member_ids = set(vehicle_ids or []), set(member_ids or [])
        vehicles = self.vehicle_ids.filtered(lambda v: v.id in vehicle_ids)
        members = self._billable_members().filtered(lambda m: m.id in member_ids)
        if max_vehicles != UNLIMITED and len(vehicles) > max_vehicles:
            raise UserError(_("Ce palier permet de conserver %s véhicule(s) au maximum.", max_vehicles))
        if max_members != UNLIMITED and len(members) > max_members:
            raise UserError(_("Ce palier permet de conserver %s utilisateur(s) au maximum.", max_members))
        return vehicles, members

    def schedule_downgrade(self, tier, billing_email=None, vehicle_ids=None, member_ids=None):
        """Planifie une baisse de palier à la prochaine échéance (fin de la période déjà payée)."""
        self.ensure_one()
        if tier not in dict(PAID_TIERS):
            raise UserError(_("Palier inconnu."))
        sub = self.subscription_id.sudo()
        if not sub or TIER_RANK[tier] >= TIER_RANK[self.tier]:
            raise UserError(_("Ce changement de palier ne peut pas être planifié."))
        if billing_email:
            self.partner_id.sudo().email = billing_email
        elif not self.partner_id.email:
            raise UserError(_("Renseignez un email de facturation."))
        vehicles, members = self._validated_keep(tier, vehicle_ids, member_ids)
        date = sub.next_invoice_date or fields.Date.context_today(self)
        self.write(
            {
                "pending_tier": tier,
                "pending_tier_date": date,
                "keep_vehicle_ids": [(6, 0, vehicles.ids)],
                "keep_member_ids": [(6, 0, members.ids)],
            }
        )
        self.message_post(
            body=_(
                "Passage au palier %(tier)s planifié le %(date)s (fin de la période payée).",
                tier=dict(TIERS)[tier],
                date=date,
            )
        )
        return date

    def cancel_pending_downgrade(self):
        self.ensure_one()
        self.write(
            {
                "pending_tier": False,
                "pending_tier_date": False,
                "keep_vehicle_ids": [(5, 0, 0)],
                "keep_member_ids": [(5, 0, 0)],
            }
        )
        self.message_post(body=_("Passage au palier inférieur annulé."))

    def set_keep_selection(self, vehicle_ids, member_ids):
        """Choix de l'administrateur quand les quotas sont déjà dépassés."""
        self.ensure_one()
        vehicles, members = self._validated_keep(None, vehicle_ids, member_ids)
        self.write({"keep_vehicle_ids": [(6, 0, vehicles.ids)], "keep_member_ids": [(6, 0, members.ids)]})
        self._enforce_quotas()

    def _enforce_quotas(self):
        today = fields.Date.context_today(self)
        for uo in self:
            vehicles, members = uo._vehicles_over_quota(), uo._members_over_quota()
            if not (vehicles or members):
                uo._restore_quota_items()
                if uo.quota_grace_until or uo.keep_vehicle_ids or uo.keep_member_ids:
                    uo._clear_quota_state()
                continue
            expired = bool(uo.quota_grace_until and uo.quota_grace_until <= today)
            archive_vehicles = bool(vehicles and (uo.keep_vehicle_ids or expired))
            suspend_members = bool(members and (uo.keep_member_ids or expired))
            if archive_vehicles:
                vehicles.write({"active": False, "quota_archived": True})
                uo.message_post(
                    body=_(
                        "%(count)s véhicule(s) archivé(s) (palier %(tier)s) : %(names)s",
                        count=len(vehicles),
                        tier=dict(TIERS)[uo.tier],
                        names=", ".join(vehicles.mapped("name")),
                    )
                )
            if suspend_members:
                members.with_context(fireman_skip_quota=True).write({"verified": False, "quota_suspended": True})
                uo.message_post(
                    body=_(
                        "%(count)s utilisateur(s) repassé(s) en attente (palier %(tier)s) : %(names)s",
                        count=len(members),
                        tier=dict(TIERS)[uo.tier],
                        names=", ".join(members.mapped("name")),
                    )
                )
            still_over = (vehicles and not archive_vehicles) or (members and not suspend_members)
            if not still_over:
                uo._clear_quota_state()
            elif not uo.quota_grace_until:
                until = today + timedelta(days=uo._quota_grace_days())
                uo.quota_grace_until = until
                uo._notify_quota_exceeded(until)

    def _clear_quota_state(self):
        self.write({"quota_grace_until": False, "keep_vehicle_ids": [(5, 0, 0)], "keep_member_ids": [(5, 0, 0)]})

    def _notify_quota_exceeded(self, until):
        self.ensure_one()
        try:
            self.message_post(
                body=_(
                    "Le palier %(tier)s autorise %(max_v)s véhicule(s) et %(max_m)s utilisateur(s) ; l'UO en compte "
                    "%(cur_v)s et %(cur_m)s. Choisissez ce que vous conservez avant le %(date)s dans l'espace client : "
                    "%(url)s. Sans choix, l'excédent sera archivé automatiquement (rien n'est supprimé).",
                    tier=dict(TIERS)[self.tier],
                    max_v="∞" if self.max_vehicles == UNLIMITED else self.max_vehicles,
                    max_m="∞" if self.max_members == UNLIMITED else self.max_members,
                    cur_v=len(self.vehicle_ids),
                    cur_m=len(self._billable_members()),
                    date=until,
                    url=f"{self.get_base_url()}/my/uo/{self.id}/quota",
                ),
                partner_ids=self.partner_id.ids,
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        except Exception:
            _logger.exception("Notification de dépassement de quota de %s impossible", self.code)

    def _restore_quota_items(self):
        """Réactive, dans la limite du palier, ce qu'une baisse de palier avait archivé ou suspendu."""
        self.ensure_one()
        if self.max_vehicles == UNLIMITED:
            free_vehicles = None
        else:
            free_vehicles = max(self.max_vehicles - len(self.vehicle_ids), 0)
        archived = self.with_context(active_test=False).vehicle_ids.filtered(
            lambda v: not v.active and v.quota_archived
        )
        archived = archived.sorted(lambda v: (v.sequence, v.id))
        restore = archived if free_vehicles is None else archived[:free_vehicles]
        if restore:
            restore.write({"active": True, "quota_archived": False})
            self.message_post(body=_("%s véhicule(s) réactivé(s).", len(restore)))

        if self.max_members == UNLIMITED:
            free_members = None
        else:
            free_members = max(self.max_members - len(self._billable_members()), 0)
        suspended = self.member_ids.filtered(lambda m: m.quota_suspended and not m.verified)
        suspended = suspended.sorted(lambda m: (not m.is_admin, m.id))
        restore = suspended if free_members is None else suspended[:free_members]
        if restore:
            restore.with_context(fireman_skip_quota=True).write({"verified": True})
            self.message_post(body=_("%s utilisateur(s) réactivé(s).", len(restore)))

    def _apply_pending_tier(self):
        self.ensure_one()
        tier, due, sub = self.pending_tier, self.pending_tier_date, self.subscription_id.sudo()
        if not sub or self.tier == tier:
            self.write({"pending_tier": False, "pending_tier_date": False})
            return
        if sub.next_invoice_date and due and sub.next_invoice_date > due:
            # Odoo a déjà facturé la période suivante au palier actuel : on attend l'échéance d'après.
            self.pending_tier_date = sub.next_invoice_date
            return
        self.subscribe(tier)

    @api.model
    def _cron_quotas(self):
        today = fields.Date.context_today(self)
        due = self.search([("pending_tier", "!=", False), ("pending_tier_date", "<=", today)])
        for uo in due:
            try:
                with self.env.cr.savepoint():
                    uo._apply_pending_tier()
            except Exception:
                _logger.exception("Changement de palier planifié de %s impossible", uo.code)
        for uo in self.search([]):
            try:
                with self.env.cr.savepoint():
                    uo._enforce_quotas()
            except Exception:
                _logger.exception("Contrôle des quotas de %s impossible", uo.code)

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
