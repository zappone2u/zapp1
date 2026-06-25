# -*- coding: utf-8 -*-

from odoo import models, fields, api


class FirebaseInventoryHistory(models.Model):
    """Historique des inventaires Firebase pour chaque UO"""

    _name = "firebase.inventory.history"
    _description = "Historique Inventaire Firebase"
    _inherit = ["firebase.sync.mixin"]
    _order = "date desc"

    name = fields.Char(string="Nom de l'inventaire", compute="_compute_name", store=True)

    # Relation avec l'UO
    uo_id = fields.Many2one(
        "res.partner",
        string="UO",
        required=True,
        ondelete="cascade",
        domain=[("is_firebase_uo", "=", True)],
    )
    uo_name = fields.Char(related="uo_id.name", string="Nom UO", store=True)

    # Identifiant Firebase de l'inventaire
    firebase_uid = fields.Char(string="Firebase UID", index=True, copy=False)

    # Informations de l'inventaire (depuis Firebase)
    inventory_date_str = fields.Char(
        string="Date (texte)", help="Date au format texte depuis Firebase (ex: 11/10/2024 7:34)"
    )
    date = fields.Datetime(string="Date de l'inventaire", compute="_compute_date_from_str", store=True)
    inventor_name = fields.Char(string="Nom de l'inventeur", help="Nom du pompier qui a fait l'inventaire")
    rank = fields.Char(string="Grade", help="Grade du pompier (ex: SGT, CPL, etc.)")
    inventor_full = fields.Boolean(string="Inventaire complet", default=False, help="True si l'inventaire est complet")
    vehicle = fields.Char(string="Véhicule", help="Nom du véhicule inventorié (ex: VSAV 1)")
    lack = fields.Text(string="Manques", help="Liste des éléments manquants")
    more_description = fields.Text(
        string="Description supplémentaire", help="Commentaires additionnels (moreDescription)"
    )

    # Métadonnées
    firebase_created_at = fields.Datetime(string="Créé sur Firebase")
    firebase_data = fields.Text(string="Données Firebase (JSON)")
    last_sync_date = fields.Datetime(string="Dernière synchronisation")

    _sql_constraints = [
        ("firebase_uid_unique", "unique(firebase_uid)", "L'UID Firebase de l'inventaire doit être unique!")
    ]

    # ──────────────────────────────────────────────────────────────────────
    #  Synchronisation temps réel (firebase.sync.mixin)
    # ──────────────────────────────────────────────────────────────────────

    def _firebase_rtdb_path(self):
        """Noeud Realtime Database : {UO_CODE}/inventor_history/{firebase_uid}."""
        self.ensure_one()
        uo_code = self.uo_id.uo_code if self.uo_id else False
        if not uo_code or not self.firebase_uid:
            return None
        return f"{uo_code}/inventor_history/{self.firebase_uid}"

    def _firebase_payload(self):
        """Données de l'inventaire poussées vers Firebase (merge)."""
        self.ensure_one()
        return {
            "date": self.inventory_date_str or "",
            "name": self.inventor_name or "",
            "rank": self.rank or "",
            "inventor_full": bool(self.inventor_full),
            "vehicle": self.vehicle or "",
            "lack": self.lack or "",
            "moreDescription": self.more_description or "",
        }

    @api.depends("uo_id.name", "date", "inventor_name")
    def _compute_name(self):
        for record in self:
            parts = []
            if record.uo_id:
                parts.append(record.uo_id.name)
            if record.inventor_name:
                parts.append(record.inventor_name)
            if record.date:
                parts.append(fields.Datetime.to_string(record.date))
            record.name = " - ".join(parts) if parts else "Inventaire"

    @api.depends("inventory_date_str")
    def _compute_date_from_str(self):
        """Convertir la date texte en datetime Odoo"""
        from datetime import datetime

        for record in self:
            if record.inventory_date_str:
                try:
                    # Format: "11/10/2024 7:34" ou "11/10/2024 07:34"
                    # Essayer différents formats
                    for fmt in ["%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M"]:
                        try:
                            dt = datetime.strptime(record.inventory_date_str, fmt)
                            record.date = dt
                            break
                        except:
                            continue
                    else:
                        record.date = False
                except:
                    record.date = False
            else:
                record.date = False
