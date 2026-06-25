# -*- coding: utf-8 -*-

from odoo import models, fields, api


class FirebaseVehicleItem(models.Model):
    """Items/Équipements individuels dans les véhicules Firebase"""

    _name = "firebase.vehicle.item"
    _description = "Item/Équipement Véhicule"
    _order = "product_id, sequence, description"

    name = fields.Char(string="Nom", compute="_compute_name", store=True)

    # Relation avec la catégorie de produit
    product_id = fields.Many2one(
        "firebase.vehicle.product",
        string="Catégorie",
        required=True,
        ondelete="cascade",
    )

    # Relation indirecte avec le véhicule
    vehicle_id = fields.Many2one(
        "firebase.vehicle",
        related="product_id.vehicle_id",
        string="Véhicule",
        store=True,
    )

    # Identifiant Firebase
    firebase_uid = fields.Char(string="Firebase UID", index=True, copy=False)

    # Sous-section / groupe
    is_group = fields.Boolean(
        string="Est un groupe",
        default=False,
        help="Si coché, cet enregistrement est un en-tête de sous-section, pas un item direct.",
    )
    group_label = fields.Char(string="Label du groupe")
    parent_item_id = fields.Many2one(
        "firebase.vehicle.item",
        string="Groupe parent",
        ondelete="cascade",
        domain="[('is_group', '=', True), ('product_id', '=', product_id)]",
    )
    sub_item_ids = fields.One2many(
        "firebase.vehicle.item",
        "parent_item_id",
        string="Sous-items",
    )

    # Informations de l'item (optionnel si is_group=True)
    description = fields.Char(string="Description")
    quantity = fields.Integer(string="Quantité", default=1)
    sequence = fields.Integer(string="Séquence", default=10)

    # Informations optionnelles
    notes = fields.Text(string="Notes")
    is_checked = fields.Boolean(string="Vérifié", default=False)
    last_check_date = fields.Datetime(string="Dernière vérification")

    # Métadonnées
    firebase_data = fields.Text(string="Données Firebase (JSON)")

    _sql_constraints = [
        (
            "firebase_uid_product_unique",
            "unique(firebase_uid, product_id)",
            "L'UID Firebase de l'item doit être unique par catégorie!",
        )
    ]

    @api.depends("description", "quantity", "is_group", "group_label")
    def _compute_name(self):
        for record in self:
            if record.is_group:
                record.name = f"[Groupe] {record.group_label or 'Sans nom'}"
            elif record.description and record.quantity:
                record.name = f"{record.description} (x{record.quantity})"
            elif record.description:
                record.name = record.description
            else:
                record.name = "Item sans description"
