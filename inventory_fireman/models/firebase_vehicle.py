# -*- coding: utf-8 -*-

from datetime import timedelta
from odoo import models, fields, api


class FirebaseVehicle(models.Model):
    """Véhicules Firebase pour chaque UO"""

    _name = "firebase.vehicle"
    _description = "Véhicule Firebase"
    _inherit = ["firebase.sync.mixin"]
    _order = "label"

    name = fields.Char(string="Nom", compute="_compute_name", store=True)

    # Relation avec l'UO
    uo_id = fields.Many2one(
        "res.partner",
        string="UO",
        required=True,
        ondelete="cascade",
        domain=[("is_firebase_uo", "=", True)],
    )
    uo_name = fields.Char(related="uo_id.name", string="Nom UO", store=True)

    # Identifiant Firebase du véhicule
    firebase_uid = fields.Char(string="Firebase UID", index=True, copy=False)

    # Informations du véhicule
    label = fields.Char(string="Label/Nom")
    license_plate = fields.Char(string="Plaque d'immatriculation")
    vehicle_id = fields.Char(string="ID Véhicule")
    notes = fields.Text(string="Notes")
    position = fields.Integer(string="Position", default=0)
    status = fields.Selection(
        [
            ("nothing", "En état"),
            ("broken", "Cassé"),
            ("repair", "En réparation"),
        ],
        string="Statut",
        default="nothing",
        help="Statut du véhicule selon les données Firebase",
    )

    verified = fields.Boolean(string="Vérifié", default=False)

    # Verrou de synchronisation (pour éviter les conflits entre cron et modifications manuelles)
    is_sync_locked = fields.Boolean(string="Verrouillé pour synchronisation", default=False, copy=False)
    sync_lock_time = fields.Datetime(string="Heure du verrouillage", copy=False)

    # Relations avec les produits et items
    product_ids = fields.One2many("firebase.vehicle.product", "vehicle_id", string="Catégories de produits")
    product_count = fields.Integer(string="Nombre de catégories", compute="_compute_product_count")

    # Produits du véhicule (stockés en JSON)
    products_data = fields.Text(string="Produits (JSON)")

    # Métadonnées
    firebase_data = fields.Text(string="Données Firebase (JSON)")
    last_sync_date = fields.Datetime(string="Dernière synchronisation")

    _sql_constraints = [
        ("firebase_uid_uo_unique", "unique(firebase_uid, uo_id)", "L'UID Firebase du véhicule doit être unique par UO!")
    ]

    @api.depends("label", "license_plate")
    def _compute_name(self):
        for record in self:
            if record.label and record.license_plate:
                record.name = f"{record.label} ({record.license_plate})"
            elif record.label:
                record.name = record.label
            elif record.license_plate:
                record.name = record.license_plate
            else:
                record.name = "Véhicule sans nom"

    @api.depends("product_ids")
    def _compute_product_count(self):
        for record in self:
            record.product_count = len(record.product_ids)

    # ──────────────────────────────────────────────────────────────────────
    #  Synchronisation temps réel (firebase.sync.mixin)
    # ──────────────────────────────────────────────────────────────────────

    def _firebase_rtdb_path(self):
        """Noeud Realtime Database : {UO_CODE}/vehicle/{firebase_uid}."""
        self.ensure_one()
        uo_code = self.uo_id.uo_code if self.uo_id else False
        if not uo_code or not self.firebase_uid:
            return None
        return f"{uo_code}/vehicle/{self.firebase_uid}"

    def _firebase_payload(self):
        """Métadonnées du véhicule poussées vers Firebase (merge, sans toucher
        au sous-arbre 'product' géré par l'application mobile)."""
        self.ensure_one()
        return {
            "label": self.label or "",
            "licensePlate": self.license_plate or "",
            "status": self.status or "nothing",
            "notes": self.notes or "",
            "position": self.position or 0,
            "verified": bool(self.verified),
        }

    def sync_vehicle_from_firebase(self, uo_id, vehicle_data):
        """
        Synchronise un véhicule depuis Firebase

        :param uo_id: ID de l'UO (res.partner)
        :param vehicle_data: Dictionnaire contenant les données du véhicule depuis Firebase
        :return: Enregistrement firebase.vehicle créé/mis à jour
        """
        import json
        import logging

        _logger = logging.getLogger(__name__)

        vehicle_id = vehicle_data.get("id")
        if not vehicle_id:
            return False

        # VÉRIFIER SI LE VÉHICULE EST VERROUILLÉ (en cours de modification sur le portail)
        existing_vehicle = self.search([("firebase_uid", "=", vehicle_id), ("uo_id", "=", uo_id)], limit=1)

        if existing_vehicle and existing_vehicle.is_sync_locked:
            _logger.info(f"Véhicule {vehicle_id} verrouillé - synchronisation ignorée")
            return existing_vehicle

        # Déverrouiller les anciens verrous (sécurité)
        self.unlock_old_locks()

        firebase_status = vehicle_data.get("status", "nothing")

        # Préparer les valeurs du véhicule
        vehicle_vals = {
            "uo_id": uo_id,
            "firebase_uid": vehicle_id,
            "label": vehicle_data.get("label", ""),
            "license_plate": vehicle_data.get("licensePlate", ""),
            "vehicle_id": vehicle_id,
            "notes": vehicle_data.get("notes", ""),
            "position": vehicle_data.get("position", 0),
            "status": firebase_status,
            "verified": True,
            "products_data": json.dumps(vehicle_data.get("product", []), ensure_ascii=False, indent=2),
            "firebase_data": json.dumps(vehicle_data, ensure_ascii=False, indent=2),
            "last_sync_date": fields.Datetime.now(),
        }

        if existing_vehicle:
            existing_vehicle.write(vehicle_vals)
            vehicle = existing_vehicle
        else:
            vehicle = self.create(vehicle_vals)

        # Synchroniser les produits/catégories
        self._sync_vehicle_products(vehicle, vehicle_data.get("product", []))

        return vehicle

    def _sync_vehicle_products(self, vehicle, products_data):
        """
        Synchronise les catégories de produits et leurs items pour un véhicule

        :param vehicle: Enregistrement firebase.vehicle
        :param products_data: Liste des produits depuis Firebase
        """
        ProductModel = self.env["firebase.vehicle.product"]
        ItemModel = self.env["firebase.vehicle.item"]

        # Garder trace des IDs Firebase pour supprimer ceux qui ne sont plus présents
        existing_product_uids = set()

        for idx, product_data in enumerate(products_data):
            product_id = product_data.get("id")
            if not product_id:
                continue

            existing_product_uids.add(product_id)

            # Préparer les valeurs de la catégorie
            product_vals = {
                "vehicle_id": vehicle.id,
                "firebase_uid": product_id,
                "label": product_data.get("label", ""),
                "sequence": idx * 10,
                "firebase_data": str(product_data),
            }

            # Chercher si la catégorie existe
            existing_product = ProductModel.search(
                [("firebase_uid", "=", product_id), ("vehicle_id", "=", vehicle.id)], limit=1
            )

            if existing_product:
                existing_product.write(product_vals)
                product = existing_product
            else:
                product = ProductModel.create(product_vals)

            # Synchroniser les items de cette catégorie
            self._sync_product_items(product, product_data.get("items", []))

        # Supprimer les produits qui ne sont plus dans Firebase
        products_to_delete = ProductModel.search(
            [("vehicle_id", "=", vehicle.id), ("firebase_uid", "not in", list(existing_product_uids))]
        )
        if products_to_delete:
            products_to_delete.unlink()

    def _sync_product_items(self, product, items_data):
        """
        Synchronise les items d'une catégorie de produits.
        Supporte les items directs {id, description, quantity} et les groupes
        {id, label, sub_items: [{id, description, quantity}]}.
        """
        ItemModel = self.env["firebase.vehicle.item"]

        # Garder trace des IDs Firebase (top-level uniquement)
        existing_item_uids = set()

        for idx, item_data in enumerate(items_data):
            item_id = item_data.get("id")
            if not item_id:
                continue

            existing_item_uids.add(item_id)

            if "sub_items" in item_data:
                # C'est un groupe
                group_vals = {
                    "product_id": product.id,
                    "firebase_uid": item_id,
                    "is_group": True,
                    "group_label": item_data.get("label", ""),
                    "description": item_data.get("label", ""),
                    "sequence": idx * 10,
                    "firebase_data": str(item_data),
                }
                existing_group = ItemModel.search(
                    [
                        ("firebase_uid", "=", item_id),
                        ("product_id", "=", product.id),
                    ],
                    limit=1,
                )
                if existing_group:
                    existing_group.write(group_vals)
                    group = existing_group
                else:
                    group = ItemModel.create(group_vals)

                # Synchroniser les sous-items
                existing_sub_uids = set()
                for sidx, sub_data in enumerate(item_data.get("sub_items", [])):
                    sub_id = sub_data.get("id")
                    if not sub_id:
                        continue
                    existing_sub_uids.add(sub_id)
                    sub_vals = {
                        "product_id": product.id,
                        "parent_item_id": group.id,
                        "firebase_uid": sub_id,
                        "is_group": False,
                        "description": sub_data.get("description", ""),
                        "quantity": sub_data.get("quantity", 1),
                        "sequence": sidx * 10,
                    }
                    existing_sub = ItemModel.search(
                        [
                            ("firebase_uid", "=", sub_id),
                            ("parent_item_id", "=", group.id),
                        ],
                        limit=1,
                    )
                    if existing_sub:
                        existing_sub.write(sub_vals)
                    else:
                        ItemModel.create(sub_vals)

                # Supprimer les sous-items disparus
                subs_to_delete = ItemModel.search(
                    [
                        ("parent_item_id", "=", group.id),
                        ("firebase_uid", "not in", list(existing_sub_uids)),
                    ]
                )
                if subs_to_delete:
                    subs_to_delete.unlink()
            else:
                # Item direct
                item_vals = {
                    "product_id": product.id,
                    "firebase_uid": item_id,
                    "is_group": False,
                    "description": item_data.get("description", ""),
                    "quantity": item_data.get("quantity", 1),
                    "sequence": idx * 10,
                    "firebase_data": str(item_data),
                }
                existing_item = ItemModel.search(
                    [
                        ("firebase_uid", "=", item_id),
                        ("product_id", "=", product.id),
                        ("parent_item_id", "=", False),
                    ],
                    limit=1,
                )
                if existing_item:
                    existing_item.write(item_vals)
                else:
                    ItemModel.create(item_vals)

        # Supprimer les items top-level qui ne sont plus dans Firebase
        items_to_delete = ItemModel.search(
            [
                ("product_id", "=", product.id),
                ("parent_item_id", "=", False),
                ("firebase_uid", "not in", list(existing_item_uids)),
            ]
        )
        if items_to_delete:
            items_to_delete.unlink()

    def lock_for_sync(self):
        """Verrouiller le véhicule pour synchronisation (éviter les conflits)"""
        self.ensure_one()
        self.write({"is_sync_locked": True, "sync_lock_time": fields.Datetime.now()})

    def unlock_sync(self):
        """Déverrouiller le véhicule après synchronisation"""
        self.ensure_one()
        self.write({"is_sync_locked": False, "sync_lock_time": False})

    def unlock_old_locks(self):
        """Déverrouiller les véhicules verrouillés depuis plus de 5 minutes (sécurité)"""
        timeout = fields.Datetime.now() - timedelta(minutes=5)
        old_locked = self.search([("is_sync_locked", "=", True), ("sync_lock_time", "<", timeout)])
        if old_locked:
            old_locked.write({"is_sync_locked": False, "sync_lock_time": False})
