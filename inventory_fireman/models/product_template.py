# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .constants import PAID_TIERS, UNLIMITED

# Produits livrés avec le module : xmlid → (palier, véhicules max., utilisateurs max.)
TIER_PRODUCTS = {
    "inventory_fireman.product_subscription_essentiel": ("essential", 3, 15),
    "inventory_fireman.product_subscription_caserne": ("caserne", 10, 30),
    "inventory_fireman.product_subscription_flotte": ("flotte", 30, UNLIMITED),
}


class ProductTemplate(models.Model):
    _inherit = "product.template"

    fireman_tier = fields.Selection(PAID_TIERS, string="Palier Inventory Fireman", copy=False)
    fireman_max_vehicles = fields.Integer(string="Véhicules max.", default=1, help="-1 = illimité")
    fireman_max_members = fields.Integer(string="Utilisateurs max.", default=1, help="-1 = illimité")

    @api.constrains("fireman_tier")
    def _check_fireman_tier_unique(self):
        for product in self.filtered("fireman_tier"):
            if self.search_count([("fireman_tier", "=", product.fireman_tier), ("id", "!=", product.id)]):
                raise ValidationError(_("Un seul produit peut porter le palier %s.", product.fireman_tier))

    @api.constrains("fireman_max_vehicles", "fireman_max_members")
    def _check_fireman_limits(self):
        for product in self.filtered("fireman_tier"):
            if min(product.fireman_max_vehicles, product.fireman_max_members) < UNLIMITED:
                raise ValidationError(_("Les limites doivent être positives, ou -1 pour illimité."))

    @api.model
    def _fireman_setup_subscription_products(self):
        """Fait des produits de palier de vrais produits récurrents mensuels (idempotent)."""
        for xmlid, (tier, max_vehicles, max_members) in TIER_PRODUCTS.items():
            product = self.env.ref(xmlid, raise_if_not_found=False)
            if (
                product
                and not product.product_tmpl_id.fireman_tier
                and not self.search_count([("fireman_tier", "=", tier)])
            ):
                product.product_tmpl_id.write(
                    {"fireman_tier": tier, "fireman_max_vehicles": max_vehicles, "fireman_max_members": max_members}
                )
        plan = self.env.ref("sale_subscription.subscription_plan_month", raise_if_not_found=False)
        for product in self.search([("fireman_tier", "!=", False)]):
            if not product.recurring_invoice:
                product.recurring_invoice = True
            if plan and not product.product_subscription_pricing_ids.filtered(lambda p: p.plan_id == plan):
                self.env["sale.subscription.pricing"].create(
                    {"product_template_id": product.id, "plan_id": plan.id, "price": product.list_price}
                )
