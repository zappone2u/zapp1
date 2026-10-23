# -*- coding: utf-8 -*-
"""Lecture Firebase → Odoo.

Firebase reste la source de l'application ; Odoo en garde une copie à jour. Chaque lecture
est idempotente (on n'écrit que ce qui diffère), n'écrase jamais une modification Odoo
en attente de push et s'exécute avec ``fireman_no_push`` pour ne rien renvoyer vers Firebase.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .constants import (
    SCOPE_INTERVENTIONS,
    SCOPE_INVENTORIES,
    SCOPE_MEMBERS,
    SCOPE_PHARMACY,
    SCOPE_UO,
    SCOPE_VEHICLES,
    SCOPES,
)
from .fireman_uo import is_valid_code
from .fireman_vehicle_template import TEMPLATES_NODE, normalize_structure
from .utils import as_list, to_naive_utc

_logger = logging.getLogger(__name__)

_UNSET = object()
UO_NODE_MARKERS = {
    "name",
    "vehicle",
    "verified",
    "inventor_history",
    "pharmacy_items",
    "pharmacy_movements",
    "interventions",
    "settings",
    "activation_status",
}


def _changes(record, vals):
    """Sous-ensemble de `vals` qui diffère de l'enregistrement (faux et vide sont égaux)."""
    return {k: v for k, v in vals.items() if record[k] != v and (record[k] or v)}


def _name_from_email(email):
    parts = (email or "").split("@")[0].replace("_", ".").split(".")
    return " ".join(p.capitalize() for p in parts if p)


class FiremanSyncService(models.AbstractModel):
    _name = "fireman.sync.service"
    _description = "Lecture Firebase → Odoo"

    # ------------------------------------------------------------------
    # Points d'entrée
    # ------------------------------------------------------------------
    @api.model
    def pull_uo(self, code, scopes=None, connector=None):
        """Relit une UO (ou seulement certains périmètres) depuis Firebase."""
        connector = self._connector(connector)
        scopes = set(scopes or SCOPES) & set(SCOPES)
        users = list(connector.iter_users()) if SCOPE_MEMBERS in scopes else None
        self._pull(connector, code, scopes, users=users)

    @api.model
    def pull_templates(self, connector=None):
        """Relit les modèles de véhicules (`vehicle_templates`, communs à toutes les UO)."""
        connector = self._connector(connector)
        svc = self.sudo().with_context(fireman_no_push=True)
        blocked = svc.env["fireman.sync.event"]._blocked_targets()
        svc._apply_templates(connector.rtdb(TEMPLATES_NODE).get(), blocked)

    @api.model
    def pull_all(self, connector=None):
        """Relit toutes les UO connues de Firebase (cron de filet de sécurité)."""
        connector = self._connector(connector)
        try:
            with self.env.cr.savepoint():
                self.pull_templates(connector)
        except Exception:
            _logger.exception("Lecture Firebase des modèles de véhicules impossible")
        users = list(connector.iter_users())
        nodes = {}
        for key, flag in (connector.rtdb("/").get(shallow=True) or {}).items():
            if flag is True and key != TEMPLATES_NODE:
                node = connector.rtdb(key).get()
                if isinstance(node, dict) and UO_NODE_MARKERS & set(node):
                    nodes[key] = node
        codes = set(nodes)
        for _uid, data in users:
            codes.update(e.get("uo_name") for e in as_list(data.get("uo")) if isinstance(e, dict) and e.get("uo_name"))

        # Les uo_name des comptes Firestore sont libres : on ignore ceux qui ne peuvent pas
        # être un trigramme (espace, point, /…) au lieu de tenter de créer une UO invalide.
        valid = {c for c in codes if is_valid_code(c)}
        ignored = sorted(codes - valid, key=str)
        if ignored:
            _logger.warning("UO ignorées (trigramme invalide) : %s", ", ".join(repr(c) for c in ignored))

        stats = {"uo": 0, "errors": 0, "ignored": len(ignored)}
        for code in sorted(valid):
            try:
                with self.env.cr.savepoint():
                    self._pull(connector, code, set(SCOPES), users=users, node=nodes.get(code))
                stats["uo"] += 1
            except Exception:
                stats["errors"] += 1
                _logger.exception("Lecture Firebase de l'UO %s impossible", code)
        connector.sudo().last_full_pull = fields.Datetime.now()
        return stats

    def _connector(self, connector):
        connector = connector or self.env["firebase.connector"]._get_default()
        if not connector:
            raise UserError(_("Aucun connecteur Firebase configuré."))
        return connector

    # ------------------------------------------------------------------
    # Lecture d'une UO
    # ------------------------------------------------------------------
    def _pull(self, connector, code, scopes, users=None, node=_UNSET):
        svc = self.sudo().with_context(fireman_no_push=True)
        if node is _UNSET:
            needs_node = scopes & {SCOPE_UO, SCOPE_VEHICLES, SCOPE_INVENTORIES, SCOPE_PHARMACY, SCOPE_INTERVENTIONS}
            node = connector.rtdb(code).get() if needs_node else None
        Uo = svc.env["fireman.uo"].with_context(active_test=False)
        uo = Uo.search([("code", "=", code)], limit=1)
        known_in_users = users is not None and any(
            e.get("uo_name") == code for _uid, d in users for e in as_list(d.get("uo")) if isinstance(e, dict)
        )
        if not uo:
            if not isinstance(node, dict) and not known_in_users:
                return
            uo = Uo.create({"name": (node or {}).get("name") or code, "code": code})

        blocked = svc.env["fireman.sync.event"]._blocked_targets()
        if isinstance(node, dict):
            if SCOPE_UO in scopes and ("fireman.uo", code) not in blocked:
                svc._apply_uo(uo, node)
            if SCOPE_VEHICLES in scopes:
                svc._apply_vehicles(uo, node.get("vehicle"), blocked)
            if SCOPE_INVENTORIES in scopes:
                svc._apply_inventories(uo, node.get("inventor_history"))
            if SCOPE_PHARMACY in scopes:
                svc._apply_pharmacy(uo, node.get("pharmacy_items"))
                svc._apply_movements(uo, node.get("pharmacy_movements"))
            if SCOPE_INTERVENTIONS in scopes:
                svc._apply_interventions(uo, node.get("interventions"))
        if SCOPE_MEMBERS in scopes and users is not None:
            svc._apply_members(uo, users, blocked)
        uo.last_pull_date = fields.Datetime.now()

    def _apply_uo(self, uo, node):
        settings = node.get("settings") if isinstance(node.get("settings"), dict) else {}

        def flag(*names, default=False):
            for source in (node, settings):
                for name in names:
                    if name in source:
                        return bool(source[name])
            return default

        vals = {
            "verified": flag("verified", default=True),
            # Le champ est écrit « prefillTheqty » par certains écrans de l'application.
            "prefill_qty": flag("prefillTheQty", "prefillTheqty"),
            "send_inventory_to_all": flag("sendInventoryToAll"),
        }
        changes = _changes(uo, vals)
        if changes:
            uo.write(changes)

    def _apply_vehicles(self, uo, raw, blocked):
        Vehicle = self.env["fireman.vehicle"]
        remote = raw if isinstance(raw, dict) else {}
        local = {v.uid: v for v in uo.with_context(active_test=False).vehicle_ids}
        for uid, data in remote.items():
            if not isinstance(data, dict) or (Vehicle._name, f"{uo.code}/{uid}") in blocked:
                continue
            vals = Vehicle._vals_from_firebase(data)
            vehicle = local.get(uid)
            if vehicle and not vehicle.active:
                continue  # archivé dans Odoo : le retrait de Firebase est en cours
            if vehicle:
                changes = _changes(vehicle, vals)
                if changes:
                    vehicle.write(changes)
            else:
                vehicle = Vehicle.create({**vals, "uo_id": uo.id, "uid": uid})
            vehicle._apply_firebase_products(data.get("product"))
        for uid, vehicle in local.items():
            if vehicle.active and uid not in remote and (Vehicle._name, f"{uo.code}/{uid}") not in blocked:
                vehicle.unlink()

    def _apply_inventories(self, uo, raw):
        """Les inventaires sont immuables : on crée les nouveaux et on retire ceux supprimés."""
        Inventory = self.env["fireman.inventory"]
        remote = raw if isinstance(raw, dict) else {}
        local = {i.uid: i for i in uo.inventory_ids}
        Inventory.create(
            [
                {**Inventory._vals_from_firebase(data), "uo_id": uo.id, "uid": uid}
                for uid, data in remote.items()
                if uid not in local and isinstance(data, dict)
            ]
        )
        Inventory.browse([i.id for uid, i in local.items() if uid not in remote]).unlink()

    def _apply_pharmacy(self, uo, raw):
        """Le stock de pharmacie est modifié dans l'application : Odoo en garde une copie fidèle."""
        Item = self.env["fireman.pharmacy.item"]
        remote = raw if isinstance(raw, dict) else {}
        local = {i.uid: i for i in uo.pharmacy_item_ids}
        for uid, data in remote.items():
            if not isinstance(data, dict):
                continue
            vals = Item._vals_from_firebase(data)
            item = local.get(uid)
            if item:
                changes = _changes(item, vals)
                if changes:
                    item.write(changes)
            else:
                Item.create({**vals, "uo_id": uo.id, "uid": uid})
        Item.browse([i.id for uid, i in local.items() if uid not in remote]).unlink()

    def _apply_movements(self, uo, raw):
        """Les mouvements de pharmacie sont une trace immuable : on crée les nouveaux et on retire ceux supprimés."""
        Movement = self.env["fireman.pharmacy.movement"]
        remote = raw if isinstance(raw, dict) else {}
        local = {m.uid: m for m in uo.pharmacy_movement_ids}
        Movement.create(
            [
                {**Movement._vals_from_firebase(data), "uo_id": uo.id, "uid": uid}
                for uid, data in remote.items()
                if uid not in local and isinstance(data, dict)
            ]
        )
        Movement.browse([m.id for uid, m in local.items() if uid not in remote]).unlink()

    def _apply_interventions(self, uo, raw):
        """Un retour d'intervention évolue quand la pharmacie réarme : Odoo en garde une copie fidèle."""
        Intervention = self.env["fireman.intervention"]
        remote = raw if isinstance(raw, dict) else {}
        local = {i.uid: i for i in uo.intervention_ids}
        for uid, data in remote.items():
            if not isinstance(data, dict):
                continue
            vals = Intervention._vals_from_firebase(data)
            intervention = local.get(uid)
            if intervention:
                changes = _changes(intervention, vals)
                if changes:
                    intervention.write(changes)
            else:
                intervention = Intervention.create({**vals, "uo_id": uo.id, "uid": uid})
            intervention._apply_firebase_lines(data.get("lines"))
        Intervention.browse([i.id for uid, i in local.items() if uid not in remote]).unlink()

    def _apply_templates(self, raw, blocked):
        """Firebase fait foi : crée, met à jour et retire les modèles, sauf ceux dont l'envoi est en attente.

        Un modèle jamais présent dans Firebase (créé dans Odoo, pas encore envoyé) n'est jamais supprimé."""
        Template = self.env["fireman.vehicle.template"].with_context(active_test=False)
        Uo = self.env["fireman.uo"].with_context(active_test=False)
        remote = raw if isinstance(raw, dict) else {}
        local = {t.uid: t for t in Template.search([("uid", "!=", False)])}

        def is_blocked(uid):
            return (Template._name, f"{TEMPLATES_NODE}/{uid}") in blocked

        for uid, data in remote.items():
            if not isinstance(data, dict) or is_blocked(uid):
                continue
            name = str(data.get("label") or "").strip()
            code = str(data.get("uo") or "").strip()
            uo = Uo.search([("code", "=", code)], limit=1) if code else Uo
            if not name or (code and not uo):
                continue
            vals = {"name": name, "description": str(data.get("description") or ""), "in_firebase": True}
            template = local.get(uid)
            if template and not template.active:
                continue  # archivé dans Odoo : le retrait de Firebase est en cours
            if template:
                changes = _changes(template, vals)
                if template.uo_id != uo:
                    changes["uo_id"] = uo.id
                if changes:
                    template.write(changes)
            else:
                template = Template.create({**vals, "uid": uid, "uo_id": uo.id})
            structure = normalize_structure(data.get("sections"))
            if template._structure() != structure:
                template.section_ids.unlink()
                template._write_structure(structure)
        for uid, template in local.items():
            if uid not in remote and template.in_firebase and template.active and not is_blocked(uid):
                template.unlink()

    def _apply_members(self, uo, users, blocked):
        Member = self.env["fireman.member"]
        wanted = {}
        for uid, data in users:
            for entry in as_list(data.get("uo")):
                if isinstance(entry, dict) and entry.get("uo_name") == uo.code:
                    wanted[uid] = (data, entry)
                    break

        by_uid = {p.firebase_uid: p for p in self._partners_for(wanted)}
        local = {m.partner_id.id: m for m in uo.member_ids}
        seen = set()
        # La première lecture d'une UO reprend l'existant tel quel (les dépassements sont gérés par le délai de grâce).
        enforce = bool(uo.last_pull_date)
        if enforce:
            uo._lock_quota()
        refused = Member
        for uid, (data, entry) in wanted.items():
            partner = by_uid.get(uid) or self._create_partner(uid, data)
            seen.add(partner.id)
            if bool(data.get("admin")) != partner.fireman_super_admin:
                partner.fireman_super_admin = bool(data.get("admin"))
            if (Member._name, f"{uo.code}/{uid}") in blocked:
                continue
            vals = {
                "is_admin": bool(entry.get("uo_admin")),
                "is_pharmacist": bool(entry.get("uo_pharmacist")),
                "verified": bool(entry.get("verified")),
                "last_connection": to_naive_utc(entry.get("last_connection")),
            }
            member = local.get(partner.id)
            over_quota = (
                enforce
                and vals["verified"]
                and not (member and member.verified)
                and not partner.fireman_super_admin
                and uo._free_member_slots() == 0
            )
            if over_quota:
                # L'application a validé quelqu'un sans place disponible : on le remet en attente.
                vals.update(verified=False, quota_suspended=True)
            if member:
                changes = _changes(member, vals)
                if changes:
                    member.write(changes)
            else:
                member = Member.create({**vals, "uo_id": uo.id, "partner_id": partner.id})
            if over_quota:
                refused |= member
        # La lecture ne renvoie rien vers Firebase : le refus doit lui être communiqué explicitement.
        refused.with_context(fireman_no_push=False)._push_enqueue()
        for partner_id, member in local.items():
            uid = member.partner_id.firebase_uid
            if uid and partner_id not in seen and (Member._name, f"{uo.code}/{uid}") not in blocked:
                member.unlink()

    def _partners_for(self, wanted):
        return self.env["res.partner"].with_context(active_test=False).search([("firebase_uid", "in", list(wanted))])

    def _create_partner(self, uid, data):
        Partner = self.env["res.partner"].with_context(active_test=False)
        email = (data.get("email") or "").strip()
        pattern = email.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
        partner = (
            Partner.search(
                [("email", "=ilike", pattern), ("firebase_uid", "=", False), ("is_company", "=", False)], limit=1
            )
            if email
            else Partner
        )
        if partner:
            partner.firebase_uid = uid
            return partner
        return Partner.create(
            {
                "name": _name_from_email(email) or uid,
                "email": email or False,
                "firebase_uid": uid,
                "fireman_super_admin": bool(data.get("admin")),
            }
        )
