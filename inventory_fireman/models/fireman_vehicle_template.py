# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .utils import as_int, as_list, new_uid

TEMPLATES_NODE = "vehicle_templates"


def normalize_structure(raw):
    """Sections Firebase d'un modèle → `_structure()` : [{label, items: [{description, quantity} | {label, sub_items}]}].

    Les entrées sans nom sont ignorées ; une quantité absente ou négative vaut 1 (zéro conservé).
    """

    def quantity(value):
        number = as_int(value, 1)
        return number if number >= 0 else 1

    def leaf(node):
        description = str(node.get("description") or "").strip()
        return {"description": description, "quantity": quantity(node.get("quantity"))} if description else None

    sections = []
    for section in as_list(raw):
        label = str(section.get("label") or "").strip() if isinstance(section, dict) else ""
        if not label:
            continue
        items = []
        for node in as_list(section.get("items")):
            if not isinstance(node, dict):
                continue
            if "sub_items" in node or ("label" in node and "description" not in node):
                group = str(node.get("label") or "").strip()
                if group:
                    subs = [leaf(s) for s in as_list(node.get("sub_items")) if isinstance(s, dict)]
                    items.append({"label": group, "sub_items": [s for s in subs if s]})
            elif (item := leaf(node)) is not None:
                items.append(item)
        sections.append({"label": label, "items": items})
    return sections


class FiremanVehicleTemplate(models.Model):
    """Véhicule type (compartiments, matériel, quantités) à partir duquel les UO créent un véhicule.

    Sans UO, le modèle est partagé avec toutes les UO ; avec une UO, il lui est réservé.
    Firebase (`vehicle_templates/{uid}`) est la source de l'application : Odoo le relit et y renvoie ses modifications.
    """

    _name = "fireman.vehicle.template"
    _description = "Modèle de véhicule"
    _inherit = ["fireman.sync.mixin"]
    _order = "sequence, name, id"
    _push_fields = ("name", "description", "active", "uo_id", "uid")

    uid = fields.Char(string="Clé Firebase", copy=False, index=True, readonly=True)
    in_firebase = fields.Boolean(
        string="Présent dans Firebase",
        copy=False,
        readonly=True,
        help="Un modèle absent de Firebase n'est supprimé d'Odoo que s'il y avait déjà été envoyé ou lu.",
    )

    name = fields.Char(string="Nom", required=True)
    description = fields.Text(string="Description")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    uo_id = fields.Many2one(
        "fireman.uo",
        string="Réservé à l'UO",
        ondelete="cascade",
        index=True,
        help="Vide : le modèle est proposé à toutes les UO.",
    )
    section_ids = fields.One2many("fireman.vehicle.template.section", "template_id", string="Catégories")
    section_count = fields.Integer(compute="_compute_counts")
    item_count = fields.Integer(compute="_compute_counts")

    _sql_constraints = [("uid_unique", "unique(uid)", "Cette clé Firebase est déjà utilisée par un autre modèle.")]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("uid", new_uid())
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Odoo → Firebase
    # ------------------------------------------------------------------
    def _push_target_key(self):
        self.ensure_one()
        return f"{TEMPLATES_NODE}/{self.uid}" if self.uid else None

    def _push_uo_code(self):
        return self.uo_id.code or ""

    def _firebase_payload(self):
        self.ensure_one()
        payload = {"label": self.name, "sections": self._structure()}
        if self.description:
            payload["description"] = self.description
        if self.uo_id:
            payload["uo"] = self.uo_id.code
        return payload

    def _push_upsert(self, connector):
        self.ensure_one()
        ref = connector.rtdb(f"{TEMPLATES_NODE}/{self.uid}")
        if self.active:
            ref.set(self._firebase_payload())
        else:
            ref.delete()
        self.sudo().with_context(fireman_no_push=True).in_firebase = self.active

    def _push_delete_payload(self):
        self.ensure_one()
        return {"uid": self.uid} if self.uid else None

    @api.model
    def _push_delete(self, connector, payload):
        connector.rtdb(f"{TEMPLATES_NODE}/{payload['uid']}").delete()

    @api.model
    def action_pull_from_firebase(self):
        self.env["fireman.sync.service"].pull_templates()

    @api.depends("section_ids.item_ids")
    def _compute_counts(self):
        for template in self:
            template.section_count = len(template.section_ids)
            template.item_count = len(template.section_ids.item_ids.filtered(lambda i: not i.is_group))

    @api.model
    def _available_for(self, uo):
        """Modèles proposés à une UO : les modèles partagés et ceux qui lui sont réservés."""
        return self.search([("uo_id", "in", [False, uo.id])])

    def _structure(self):
        """Contenu neutre : [{label, items: [{description, quantity} | {label, sub_items: [...]}]}]."""
        self.ensure_one()
        sections = []
        for section in self.section_ids.sorted("sequence"):
            items = []
            for item in section.item_ids.filtered(lambda i: not i.parent_id).sorted("sequence"):
                if item.is_group:
                    items.append(
                        {
                            "label": item.name,
                            "sub_items": [
                                {"description": c.name, "quantity": c.quantity}
                                for c in item.child_ids.sorted("sequence")
                            ],
                        }
                    )
                else:
                    items.append({"description": item.name, "quantity": item.quantity})
            sections.append({"label": section.name, "items": items})
        return sections

    def _api_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "label": self.name,
            "description": self.description or "",
            "shared": not self.uo_id,
            "sections": self._structure(),
        }

    def create_vehicle(self, uo, name):
        """Crée un véhicule de l'UO à partir du modèle (le quota de véhicules est contrôlé)."""
        self.ensure_one()
        if self.uo_id and self.uo_id != uo:
            raise ValidationError(_("Ce modèle est réservé à une autre UO."))
        return self.env["fireman.vehicle"].create_from_structure(uo, name, self._structure())

    @api.model
    def from_structure(self, name, structure, uo=None):
        template = self.create({"name": name, "uo_id": uo.id if uo else False})
        template._write_structure(structure)
        return template

    def _write_structure(self, structure):
        self.ensure_one()
        Section = self.env["fireman.vehicle.template.section"]
        Item = self.env["fireman.vehicle.template.item"]
        for s_index, node in enumerate(structure):
            section = Section.create({"template_id": self.id, "name": node["label"], "sequence": s_index * 10})
            for i_index, item in enumerate(node["items"]):
                is_group = "sub_items" in item
                parent = Item.create(
                    {
                        "section_id": section.id,
                        "name": item["label"] if is_group else item["description"],
                        "quantity": 1 if is_group else as_int(item["quantity"], 1),
                        "is_group": is_group,
                        "sequence": i_index * 10,
                    }
                )
                for c_index, child in enumerate(item.get("sub_items", [])):
                    Item.create(
                        {
                            "section_id": section.id,
                            "parent_id": parent.id,
                            "name": child["description"],
                            "quantity": as_int(child["quantity"], 1),
                            "sequence": c_index * 10,
                        }
                    )


class FiremanVehicleTemplateSection(models.Model):
    _name = "fireman.vehicle.template.section"
    _description = "Catégorie d'un modèle de véhicule"
    _inherit = ["fireman.sync.mixin"]
    _order = "template_id, sequence, id"

    template_id = fields.Many2one("fireman.vehicle.template", required=True, ondelete="cascade", index=True)
    name = fields.Char(string="Nom", required=True)
    sequence = fields.Integer(default=10)
    item_ids = fields.One2many("fireman.vehicle.template.item", "section_id", string="Équipements")

    def _push_owner(self):
        return self.template_id


class FiremanVehicleTemplateItem(models.Model):
    _name = "fireman.vehicle.template.item"
    _description = "Équipement d'un modèle de véhicule"
    _inherit = ["fireman.sync.mixin"]
    _order = "section_id, sequence, id"

    section_id = fields.Many2one("fireman.vehicle.template.section", required=True, ondelete="cascade", index=True)
    parent_id = fields.Many2one(
        "fireman.vehicle.template.item", string="Groupe", ondelete="cascade", domain="[('is_group', '=', True)]"
    )
    child_ids = fields.One2many("fireman.vehicle.template.item", "parent_id", string="Équipements du groupe")
    name = fields.Char(string="Désignation", required=True)
    is_group = fields.Boolean(string="Est un groupe")
    quantity = fields.Integer(string="Quantité", default=1)
    sequence = fields.Integer(default=10)

    @api.constrains("parent_id", "is_group", "section_id")
    def _check_group(self):
        for item in self:
            if item.parent_id and (not item.parent_id.is_group or item.is_group):
                raise ValidationError(_("Un groupe ne peut contenir que des équipements simples."))
            if item.parent_id and item.parent_id.section_id != item.section_id:
                raise ValidationError(_("Le groupe doit appartenir à la même catégorie."))

    @api.constrains("quantity")
    def _check_quantity(self):
        for item in self:
            if item.quantity < 0:
                raise ValidationError(_("La quantité ne peut pas être négative."))

    def _push_owner(self):
        return self.section_id.template_id
