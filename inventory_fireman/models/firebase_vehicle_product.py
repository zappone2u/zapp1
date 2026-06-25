# -*- coding: utf-8 -*-

from odoo import models, fields, api


class FirebaseVehicleProduct(models.Model):
    """Catégories de produits/équipements pour les véhicules Firebase"""

    _name = "firebase.vehicle.product"
    _description = "Catégorie de Produits Véhicule"
    _order = "vehicle_id, sequence, label"

    name = fields.Char(string="Nom", compute="_compute_name", store=True)
    
    # Relation avec le véhicule
    vehicle_id = fields.Many2one(
        "firebase.vehicle",
        string="Véhicule",
        required=True,
        ondelete="cascade",
    )
    
    # Identifiant Firebase
    firebase_uid = fields.Char(string="Firebase UID", index=True, copy=False)
    
    # Informations de la catégorie
    label = fields.Char(string="Label", required=True)
    sequence = fields.Integer(string="Séquence", default=10)
    
    # Items de cette catégorie
    item_ids = fields.One2many(
        "firebase.vehicle.item",
        "product_id",
        string="Items/Équipements"
    )
    item_count = fields.Integer(
        string="Nombre d'items",
        compute="_compute_item_count"
    )
    
    # Métadonnées
    firebase_data = fields.Text(string="Données Firebase (JSON)")
    
    _sql_constraints = [
        ("firebase_uid_vehicle_unique", 
         "unique(firebase_uid, vehicle_id)", 
         "L'UID Firebase de la catégorie doit être unique par véhicule!")
    ]
    
    @api.depends("label", "vehicle_id.label")
    def _compute_name(self):
        for record in self:
            if record.vehicle_id and record.label:
                record.name = f"{record.vehicle_id.label} - {record.label}"
            elif record.label:
                record.name = record.label
            else:
                record.name = "Catégorie sans nom"
    
    @api.depends("item_ids")
    def _compute_item_count(self):
        for record in self:
            record.item_count = len(record.item_ids)
