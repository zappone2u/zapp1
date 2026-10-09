# -*- coding: utf-8 -*-
"""Lecture Firebase → Odoo.

Firebase reste la source de l'application ; Odoo en garde une copie à jour. Chaque lecture
est idempotente (on n'écrit que ce qui diffère), n'écrase jamais une modification Odoo
en attente de push et s'exécute avec ``fireman_no_push`` pour ne rien renvoyer vers Firebase.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .constants import SCOPE_INVENTORIES, SCOPE_MEMBERS, SCOPE_UO, SCOPE_VEHICLES, SCOPES
from .utils import as_list, to_naive_utc

_logger = logging.getLogger(__name__)

_UNSET = object()
UO_NODE_MARKERS = {"name", "vehicle", "verified", "inventor_history", "pharmacy_items", "settings", "activation_status"}


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
    def pull_all(self, connector=None):
        """Relit toutes les UO connues de Firebase (cron de filet de sécurité)."""
        connector = self._connector(connector)
        users = list(connector.iter_users())
        nodes = {}
        for key, flag in (connector.rtdb("/").get(shallow=True) or {}).items():
            if flag is True:
                node = connector.rtdb(key).get()
                if isinstance(node, dict) and UO_NODE_MARKERS & set(node):
                    nodes[key] = node
        codes = set(nodes)
        for _uid, data in users:
            codes.update(e.get("uo_name") for e in as_list(data.get("uo")) if isinstance(e, dict) and e.get("uo_name"))

        stats = {"uo": 0, "errors": 0}
        for code in sorted(codes):
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
            needs_node = scopes & {SCOPE_UO, SCOPE_VEHICLES, SCOPE_INVENTORIES}
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
        local = {v.uid: v for v in uo.vehicle_ids}
        for uid, data in remote.items():
            if not isinstance(data, dict) or (Vehicle._name, f"{uo.code}/{uid}") in blocked:
                continue
            vals = Vehicle._vals_from_firebase(data)
            vehicle = local.get(uid)
            if vehicle:
                changes = _changes(vehicle, vals)
                if changes:
                    vehicle.write(changes)
            else:
                vehicle = Vehicle.create({**vals, "uo_id": uo.id, "uid": uid})
            vehicle._apply_firebase_products(data.get("product"))
        for uid, vehicle in local.items():
            if uid not in remote and (Vehicle._name, f"{uo.code}/{uid}") not in blocked:
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
            if member:
                changes = _changes(member, vals)
                if changes:
                    member.write(changes)
            else:
                Member.create({**vals, "uo_id": uo.id, "partner_id": partner.id})
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
