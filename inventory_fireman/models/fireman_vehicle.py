# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .constants import VEHICLE_STATUSES
from .utils import as_int, as_list, new_uid


def normalize_products(raw):
    """Arbre `product` de Firebase → liste canonique de sections.

    [{"id", "label", "items": [{"id", "description", "quantity"}
                              | {"id", "label", "sub_items": [{"id", "description", "quantity"}]}]}]
    """

    def leaf(node):
        return {
            "id": str(node.get("id") or new_uid()),
            "description": str(node.get("description") or ""),
            "quantity": as_int(node.get("quantity"), 1),
        }

    sections = []
    for section in as_list(raw):
        if not isinstance(section, dict):
            continue
        items = []
        for node in as_list(section.get("items")):
            if not isinstance(node, dict):
                continue
            # Un groupe vide perd sa clé `sub_items` dans Firebase : on le reconnaît à son label.
            if "sub_items" in node or ("label" in node and "description" not in node):
                items.append(
                    {
                        "id": str(node.get("id") or new_uid()),
                        "label": str(node.get("label") or ""),
                        "sub_items": [leaf(sub) for sub in as_list(node.get("sub_items")) if isinstance(sub, dict)],
                    }
                )
            else:
                items.append(leaf(node))
        sections.append(
            {"id": str(section.get("id") or new_uid()), "label": str(section.get("label") or ""), "items": items}
        )
    return sections


class FiremanVehicle(models.Model):
    _name = "fireman.vehicle"
    _description = "Véhicule"
    _inherit = ["fireman.sync.mixin"]
    _order = "uo_id, sequence, id"
    _push_fields = ("name", "license_plate", "status", "notes", "sequence", "verified", "uo_id", "active")

    active = fields.Boolean(default=True, help="Décoché : archivé, retiré de l'application mais conservé dans Odoo.")
    quota_archived = fields.Boolean(
        string="Archivé par le palier",
        help="Archivé automatiquement après une baisse de palier ; réactivé si le palier le permet de nouveau.",
    )

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, copy=False, index=True, default=lambda s: new_uid())
    name = fields.Char(string="Nom", required=True)
    license_plate = fields.Char(string="Immatriculation")
    status = fields.Selection(VEHICLE_STATUSES, string="État", default="nothing", required=True)
    notes = fields.Text(string="Notes")
    sequence = fields.Integer(string="Position", default=10)
    verified = fields.Boolean(string="Vérifié")
    section_ids = fields.One2many("fireman.vehicle.section", "vehicle_id", string="Catégories")
    section_count = fields.Integer(compute="_compute_counts")
    item_count = fields.Integer(compute="_compute_counts")

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Identifiant de véhicule déjà utilisé dans cette UO.")]

    @api.depends("section_ids.item_ids")
    def _compute_counts(self):
        for vehicle in self:
            vehicle.section_count = len(vehicle.section_ids)
            vehicle.item_count = len(vehicle.section_ids.item_ids.filtered(lambda i: not i.is_group))

    @api.model_create_multi
    def create(self, vals_list):
        if self._push_enabled() and not self.env.context.get("fireman_skip_quota"):
            for uo, count in self._count_by_uo(vals_list).items():
                uo._check_vehicle_quota(count)
        return super().create(vals_list)

    def _count_by_uo(self, vals_list):
        counts = {}
        for vals in vals_list:
            uo = self.env["fireman.uo"].browse(vals.get("uo_id"))
            counts[uo] = counts.get(uo, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Firebase → Odoo
    # ------------------------------------------------------------------
    @api.model
    def _vals_from_firebase(self, data):
        status = data.get("status")
        return {
            "name": str(data.get("label") or _("Véhicule")),
            "license_plate": str(data.get("licensePlate") or ""),
            "status": status if status in dict(VEHICLE_STATUSES) else "nothing",
            "notes": str(data.get("notes") or ""),
            "sequence": as_int(data.get("position")),
            "verified": bool(data.get("verified")),
        }

    def _apply_firebase_products(self, raw):
        """Aligne les catégories/équipements sur l'arbre Firebase (uniquement s'il diffère)."""
        self.ensure_one()
        wanted = normalize_products(raw)
        if wanted == self._firebase_products():
            return
        Section = self.env["fireman.vehicle.section"]
        Item = self.env["fireman.vehicle.item"]
        sections = {s.uid: s for s in self.section_ids}
        kept_sections = set()
        for s_index, node in enumerate(wanted):
            vals = {"name": node["label"], "sequence": s_index * 10}
            section = sections.get(node["id"])
            if section:
                section.write(vals)
            else:
                section = Section.create({**vals, "vehicle_id": self.id, "uid": node["id"]})
            kept_sections.add(node["id"])

            items = {i.uid: i for i in section.item_ids.filtered(lambda i: not i.parent_id)}
            kept_items = set()
            for i_index, item_node in enumerate(node["items"]):
                is_group = "sub_items" in item_node
                vals = {
                    "name": item_node["label"] if is_group else item_node["description"],
                    "quantity": 1 if is_group else item_node["quantity"],
                    "is_group": is_group,
                    "sequence": i_index * 10,
                }
                item = items.get(item_node["id"])
                if item:
                    item.write(vals)
                else:
                    item = Item.create({**vals, "section_id": section.id, "uid": item_node["id"]})
                kept_items.add(item_node["id"])
                if is_group:
                    self._apply_group_children(item, item_node["sub_items"])
            section.item_ids.filtered(lambda i: not i.parent_id and i.uid not in kept_items).unlink()
        self.section_ids.filtered(lambda s: s.uid not in kept_sections).unlink()

    def _apply_group_children(self, group, children):
        Item = self.env["fireman.vehicle.item"]
        existing = {c.uid: c for c in group.child_ids}
        kept = set()
        for index, node in enumerate(children):
            vals = {"name": node["description"], "quantity": node["quantity"], "sequence": index * 10}
            child = existing.get(node["id"])
            if child:
                child.write(vals)
            else:
                Item.create({**vals, "section_id": group.section_id.id, "parent_id": group.id, "uid": node["id"]})
            kept.add(node["id"])
        group.child_ids.filtered(lambda c: c.uid not in kept).unlink()

    # ------------------------------------------------------------------
    # Odoo → Firebase
    # ------------------------------------------------------------------
    def _firebase_products(self):
        """Arbre `product` au format canonique (identique à `normalize_products`)."""
        self.ensure_one()
        sections = []
        for section in self.section_ids.sorted("sequence"):
            items = []
            for item in section.item_ids.filtered(lambda i: not i.parent_id).sorted("sequence"):
                if item.is_group:
                    items.append(
                        {
                            "id": item.uid,
                            "label": item.name or "",
                            "sub_items": [
                                {"id": c.uid, "description": c.name or "", "quantity": c.quantity}
                                for c in item.child_ids.sorted("sequence")
                            ],
                        }
                    )
                else:
                    items.append({"id": item.uid, "description": item.name or "", "quantity": item.quantity})
            sections.append({"id": section.uid, "label": section.name or "", "items": items})
        return sections

    def _push_target_key(self):
        self.ensure_one()
        return f"{self.uo_id.code}/{self.uid}"

    def _push_uo_code(self):
        return self.uo_id.code

    def _push_upsert(self, connector):
        self.ensure_one()
        ref = connector.rtdb(f"{self.uo_id.code}/vehicle/{self.uid}")
        if not self.active:
            ref.delete()
            return
        ref.update(
            {
                "id": self.uid,
                "vehicleId": self.uid,
                "label": self.name,
                "licensePlate": self.license_plate or "",
                "status": self.status,
                "notes": self.notes or "",
                "position": self.sequence,
                "verified": bool(self.verified),
            }
        )
        products = self._firebase_products()
        if products:
            ref.child("product").set(products)
        else:
            ref.child("product").delete()

    def _push_delete_payload(self):
        self.ensure_one()
        return {"code": self.uo_id.code, "uid": self.uid}

    @api.model
    def _push_delete(self, connector, payload):
        connector.rtdb(f"{payload['code']}/vehicle/{payload['uid']}").delete()


class FiremanVehicleSection(models.Model):
    _name = "fireman.vehicle.section"
    _description = "Catégorie d'équipement"
    _inherit = ["fireman.sync.mixin"]
    _order = "vehicle_id, sequence, id"

    vehicle_id = fields.Many2one("fireman.vehicle", string="Véhicule", required=True, ondelete="cascade", index=True)
    uid = fields.Char(required=True, copy=False, default=lambda s: new_uid())
    name = fields.Char(string="Nom", required=True)
    sequence = fields.Integer(default=10)
    item_ids = fields.One2many("fireman.vehicle.item", "section_id", string="Équipements")

    def _push_owner(self):
        return self.vehicle_id


class FiremanVehicleItem(models.Model):
    """Équipement, ou groupe d'équipements (un groupe contient des `child_ids`)."""

    _name = "fireman.vehicle.item"
    _description = "Équipement de véhicule"
    _inherit = ["fireman.sync.mixin"]
    _order = "section_id, sequence, id"

    section_id = fields.Many2one(
        "fireman.vehicle.section", string="Catégorie", required=True, ondelete="cascade", index=True
    )
    vehicle_id = fields.Many2one(related="section_id.vehicle_id", store=True, index=True)
    parent_id = fields.Many2one(
        "fireman.vehicle.item", string="Groupe", ondelete="cascade", domain="[('is_group', '=', True)]"
    )
    child_ids = fields.One2many("fireman.vehicle.item", "parent_id", string="Équipements du groupe")
    uid = fields.Char(required=True, copy=False, default=lambda s: new_uid())
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

    def _push_owner(self):
        return self.section_id.vehicle_id
