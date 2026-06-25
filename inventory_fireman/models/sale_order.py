# -*- coding: utf-8 -*-

import logging
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    is_firebase_subscription = fields.Boolean(
        string="Abonnement Firebase",
        compute="_compute_is_firebase_subscription",
        store=True,
        help="Indique si cette commande contient des produits d'abonnement Firebase",
    )

    firebase_uo_id = fields.Many2one(
        "res.partner",
        string="UO Firebase",
        help="L'Unité Opérationnelle pour laquelle cet abonnement est souscrit",
        domain=[("is_firebase_uo", "=", True)],
    )

    subscription_tier = fields.Selection(
        [
            ("essential", "Essentiel"),
            ("caserne", "Caserne"),
            ("flotte", "Flotte"),
        ],
        string="Palier d'abonnement",
        compute="_compute_subscription_tier",
        store=True,
        help="Le palier d'abonnement de cette commande",
    )

    @api.depends("order_line.product_id")
    def _compute_is_firebase_subscription(self):
        """Détermine si la commande contient des produits d'abonnement Firebase"""
        refs = [
            "inventory_fireman.product_subscription_essentiel",
            "inventory_fireman.product_subscription_caserne",
            "inventory_fireman.product_subscription_flotte",
        ]
        subscription_product_ids = set()
        for ref in refs:
            p = self.env.ref(ref, raise_if_not_found=False)
            if p:
                subscription_product_ids.add(p.id)

        for order in self:
            order.is_firebase_subscription = any(
                line.product_id.id in subscription_product_ids for line in order.order_line
            )

    @api.depends("order_line.product_id")
    def _compute_subscription_tier(self):
        """Détermine le palier d'abonnement basé sur le produit"""
        # Mapping ref -> tier (aligné avec activation_status Firebase)
        tier_map = {
            "inventory_fireman.product_subscription_essentiel": "essential",
            "inventory_fireman.product_subscription_caserne": "caserne",
            "inventory_fireman.product_subscription_flotte": "flotte",
        }
        for order in self:
            tier = False
            for ref_str, t in tier_map.items():
                product = self.env.ref(ref_str, raise_if_not_found=False)
                if product and any(line.product_id.id == product.id for line in order.order_line):
                    tier = t
                    break
            order.subscription_tier = tier

    @api.model_create_multi
    def create(self, vals_list):
        """Surcharge pour gérer les abonnements Firebase"""
        orders = super().create(vals_list)

        for order in orders:
            # Si c'est un abonnement Firebase et que le partner est une UO
            if order.is_firebase_subscription and order.partner_id.is_firebase_uo:
                # Sauvegarder l'UO dans le champ dédié
                order.firebase_uo_id = order.partner_id

                # S'assurer que les adresses de facturation et livraison sont correctes
                order.write(
                    {
                        "partner_invoice_id": order.partner_id.id,
                        "partner_shipping_id": order.partner_id.id,
                    }
                )

        return orders

    def write(self, vals):
        """Empêcher la modification du partner_id pour les abonnements Firebase"""
        for order in self:
            # Si on essaie de changer le partner_id d'un abonnement Firebase
            if "partner_id" in vals and order.is_firebase_subscription and order.firebase_uo_id:
                # Si le nouveau partner n'est pas l'UO sauvegardée, on restaure l'UO
                new_partner_id = vals.get("partner_id")
                if new_partner_id != order.firebase_uo_id.id:
                    _logger.warning(
                        f"Tentative de modification du partner_id de la commande {order.name} "
                        f"d'abonnement Firebase. Restauration de l'UO {order.firebase_uo_id.name}"
                    )
                    vals["partner_id"] = order.firebase_uo_id.id
                    vals["partner_invoice_id"] = order.firebase_uo_id.id
                    vals["partner_shipping_id"] = order.firebase_uo_id.id

        return super().write(vals)

    def action_confirm(self):
        """Surcharge pour activer l'UO lors de la confirmation de l'abonnement"""
        res = super().action_confirm()

        for order in self:
            if order.is_firebase_subscription and order.firebase_uo_id and order.subscription_tier:
                try:
                    # Utilise activate_subscription qui met à jour les limites ET pousse vers Firebase
                    order.firebase_uo_id.activate_subscription(
                        order.subscription_tier,
                        sale_order_id=order.id,
                    )
                    _logger.info(
                        f"UO {order.firebase_uo_id.name} activée avec le palier {order.subscription_tier} "
                        f"via la commande {order.name}"
                    )
                except Exception as e:
                    _logger.error(
                        f"Erreur lors de l'activation de l'UO {order.firebase_uo_id.name}: {e}",
                        exc_info=True,
                    )

        return res
