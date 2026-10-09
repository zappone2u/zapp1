# -*- coding: utf-8 -*-
from odoo import fields, models

from .constants import DEFAULT_QUOTA_GRACE_DAYS, QUOTA_GRACE_PARAM


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    fireman_quota_grace_days = fields.Integer(
        string="Délai avant archivage automatique (jours)",
        config_parameter=QUOTA_GRACE_PARAM,
        default=DEFAULT_QUOTA_GRACE_DAYS,
        help="Après une baisse de palier, délai laissé à l'UO pour choisir les véhicules et utilisateurs qu'elle "
        "conserve. Passé ce délai, l'excédent est archivé automatiquement.",
    )
