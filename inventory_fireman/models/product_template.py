import logging
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    max_user = fields.Integer(string="Max user")
    max_vehicle = fields.Integer(string="Max vehicle")
