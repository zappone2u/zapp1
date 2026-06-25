# -*- coding: utf-8 -*-

from odoo import models, fields, api


class PompierUoRel(models.Model):
    """Table intermédiaire pour la relation Pompier-UO avec informations spécifiques"""

    _name = "pompier.uo.rel"
    _description = "Relation Pompier - Unité Organisationnelle"
    _rec_name = "display_name"

    # Relations
    pompier_id = fields.Many2one(
        "res.partner",
        string="Pompier",
        required=True,
        ondelete="cascade",
        domain=[("is_firebase_pompier", "=", True)],
    )
    uo_id = fields.Many2one(
        "res.partner",
        string="Unité Organisationnelle",
        required=True,
        ondelete="cascade",
        domain=[("is_firebase_uo", "=", True)],
    )

    # Informations spécifiques à cette UO pour ce pompier
    uo_admin = fields.Boolean(
        string="Administrateur UO",
        default=False,
        help="Le pompier est administrateur de cette UO",
    )
    uo_pharmacist = fields.Boolean(
        string="Pharmacien UO",
        default=False,
        help="Le pompier est pharmacien de cette UO",
    )
    verified = fields.Boolean(
        string="Vérifié",
        default=False,
        help="Le pompier est vérifié dans cette UO",
    )
    last_connection_date = fields.Datetime(
        string="Dernière connexion",
        help="Le pompier s'est connecté récemment à cette UO",
    )

    # Champs calculés pour affichage
    display_name = fields.Char(
        string="Nom",
        compute="_compute_display_name",
        store=True,
    )
    pompier_name = fields.Char(related="pompier_id.name", string="Nom du pompier", store=True)
    uo_name = fields.Char(related="uo_id.name", string="Nom de l'UO", store=True)
    pompier_email = fields.Char(related="pompier_id.email", string="Email", store=True)

    # Contrainte d'unicité
    _sql_constraints = [
        (
            "pompier_uo_unique",
            "UNIQUE(pompier_id, uo_id)",
            "Un pompier ne peut être lié qu'une seule fois à une UO !",
        )
    ]

    @api.depends("pompier_id.name", "uo_id.name")
    def _compute_display_name(self):
        """Calcul du nom d'affichage"""
        for rel in self:
            if rel.pompier_id and rel.uo_id:
                rel.display_name = f"{rel.pompier_id.name} - {rel.uo_id.name}"
            else:
                rel.display_name = "Nouvelle relation"

    def name_get(self):
        """Surcharge pour l'affichage du nom"""
        result = []
        for rel in self:
            name = f"{rel.pompier_id.name} - {rel.uo_id.name}"
            result.append((rel.id, name))
        return result
