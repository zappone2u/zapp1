# -*- coding: utf-8 -*-
import csv
import io
import logging
from urllib.parse import urlencode

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal

from ..models.constants import PAID_TIERS, TIER_RANK, TIERS
from ..models.fireman_uo import MAX_CODE_LENGTH, is_valid_code
from ..models.utils import QuotaExceeded

_logger = logging.getLogger(__name__)

# Messages affichés après une redirection (?ok=… / ?error=…)
FLASH = {
    "created": "Enregistré.",
    "saved": "Modifications enregistrées.",
    "deleted": "Supprimé.",
    "subscribed": "Votre abonnement est actif. La facture vous a été envoyée par email.",
    "cancelled": "Votre abonnement est résilié. L'UO repasse au palier Découverte.",
    "invited": "Membre ajouté.",
    "removed": "Membre retiré.",
    "no_account": "Aucun compte Firebase avec cet email : la personne doit d'abord s'inscrire dans l'application.",
    "already_member": "Cette personne est déjà membre de l'UO.",
    "last_admin": "Il doit rester au moins un administrateur dans l'UO.",
    "invalid": "Données invalides.",
    "code_taken": "Ce trigramme est déjà utilisé.",
    "invalid_code": f"Trigramme invalide : {MAX_CODE_LENGTH} caractères maximum, sans espace ni . $ # [ ] /",
    "quota": "La limite de votre palier est atteinte : choisissez un palier supérieur.",
    "downgrade_scheduled": "Passage au palier inférieur planifié à la fin de la période payée.",
    "downgrade_cancelled": "Passage au palier inférieur annulé.",
    "downgrade_invalid": "Sélection invalide : respectez les limites du palier choisi.",
    "quota_saved": "Votre choix est enregistré.",
    "failed": "L'opération a échoué. Réessayez ou contactez le support.",
}

VEHICLE_ACTIONS = (
    "save",
    "add_section",
    "rename_section",
    "delete_section",
    "add_item",
    "add_group",
    "update_item",
    "delete_item",
    "move",
)


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class FiremanPortal(CustomerPortal):
    """Espace client : gestion de l'UO, des véhicules, du personnel et de l'abonnement."""

    # ------------------------------------------------------------------
    # Accès
    # ------------------------------------------------------------------
    def _my_uos(self):
        partner = request.env.user.partner_id
        Uo = request.env["fireman.uo"].sudo()
        if partner.fireman_super_admin:
            return Uo.search([])
        return (
            request.env["fireman.member"]
            .sudo()
            .search([("partner_id", "=", partner.id), ("verified", "=", True)])
            .uo_id
        )

    def _get_uo(self, uo_id, admin=False):
        """UO (en sudo) + droit d'administration ; 403 si l'utilisateur n'en est pas membre."""
        uo = request.env["fireman.uo"].sudo().browse(uo_id).exists()
        if not uo:
            raise request.not_found()
        partner = request.env.user.partner_id
        member = uo.member_ids.filtered(lambda m: m.partner_id == partner and m.verified)
        is_admin = bool(partner.fireman_super_admin or member.is_admin)
        if not (member or partner.fireman_super_admin) or (admin and not is_admin):
            raise AccessError(_("Vous n'avez pas accès à cette page."))
        return uo, is_admin

    def _values(self, uo, is_admin, page, **extra):
        return {
            "uo": uo,
            "is_admin": is_admin,
            "page_name": page,
            "tiers": dict(TIERS),
            "flash": {
                "ok": FLASH.get(request.params.get("ok")),
                "error": FLASH.get(request.params.get("error")),
            },
            **extra,
        }

    def _redirect(self, url, ok=None, error=None, anchor=None):
        query = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
        return request.redirect(f"{url}{'?' + query if query else ''}{'#' + anchor if anchor else ''}")

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        uos = self._my_uos()
        if uos:
            values["portal_client_category_enable"] = True
        if "uo_count" in counters:
            values["uo_count"] = len(uos)
        return values

    # ------------------------------------------------------------------
    # UO
    # ------------------------------------------------------------------
    @http.route(
        ["/my/uo", "/my/firebase/uo", "/my/firebase", "/firebase/dashboard"], type="http", auth="user", website=True
    )
    def uo_list(self, **kw):
        return request.render(
            "inventory_fireman.portal_uo_list",
            {
                "uos": self._my_uos(),
                "page_name": "uo",
                "flash": {"ok": FLASH.get(kw.get("ok")), "error": FLASH.get(kw.get("error"))},
            },
        )

    @http.route("/my/uo/new", type="http", auth="user", website=True, methods=["GET", "POST"])
    def uo_new(self, **kw):
        if request.httprequest.method == "GET":
            return request.render(
                "inventory_fireman.portal_uo_new",
                {"page_name": "uo", "flash": {"error": FLASH.get(kw.get("error"))}},
            )
        name = (kw.get("name") or "").strip()
        code = (kw.get("code") or "").strip().upper()
        env = request.env
        Uo = env["fireman.uo"].sudo()
        if not name or not code:
            return self._redirect("/my/uo/new", error="invalid")
        if not is_valid_code(code):
            return self._redirect("/my/uo/new", error="invalid_code")
        if Uo.with_context(active_test=False).search_count([("code", "=", code)]) or self._firebase_has_node(code):
            return self._redirect("/my/uo/new", error="code_taken")
        partner = env.user.partner_id
        try:
            uo = Uo.create({"name": name, "code": code, "billing_email": env.user.login})
            creator = uo.member_ids.filtered(lambda m: m.partner_id == partner)  # déjà membre si super administrateur
            if creator:
                creator.write({"is_admin": True, "verified": True})
            else:
                env["fireman.member"].sudo().create(
                    {"uo_id": uo.id, "partner_id": partner.id, "is_admin": True, "verified": True}
                )
        except UserError:
            return self._redirect("/my/uo/new", error="invalid")
        return self._redirect(f"/my/uo/{uo.id}", ok="created")

    def _firebase_has_node(self, code):
        connector = request.env["firebase.connector"]._get_default()
        if not connector:
            return False
        try:
            return connector.rtdb(code).get(shallow=True) is not None
        except Exception:
            _logger.warning("Vérification du trigramme %s dans Firebase impossible", code, exc_info=True)
            return False

    @http.route("/my/uo/<int:uo_id>", type="http", auth="user", website=True)
    def uo_dashboard(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id)
        return request.render("inventory_fireman.portal_uo_dashboard", self._values(uo, is_admin, "uo"))

    @http.route("/my/uo/<int:uo_id>/settings", type="http", auth="user", website=True, methods=["POST"])
    def uo_settings(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        uo.write(
            {
                "prefill_qty": bool(kw.get("prefill_qty")),
                "send_inventory_to_all": bool(kw.get("send_inventory_to_all")),
            }
        )
        return self._redirect(f"/my/uo/{uo.id}", ok="saved")

    # ------------------------------------------------------------------
    # Véhicules
    # ------------------------------------------------------------------
    @http.route("/my/uo/<int:uo_id>/vehicles", type="http", auth="user", website=True)
    def vehicles(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id)
        return request.render(
            "inventory_fireman.portal_vehicles",
            self._values(uo, is_admin, "vehicles", statuses=self._vehicle_statuses()),
        )

    @http.route("/my/uo/<int:uo_id>/vehicles/new", type="http", auth="user", website=True, methods=["POST"])
    def vehicle_new(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        name = (kw.get("name") or "").strip()
        if not name:
            return self._redirect(f"/my/uo/{uo.id}/vehicles", error="invalid")
        try:
            vehicle = (
                request.env["fireman.vehicle"]
                .sudo()
                .create(
                    {
                        "uo_id": uo.id,
                        "name": name,
                        "license_plate": (kw.get("license_plate") or "").strip(),
                        "sequence": (max(uo.vehicle_ids.mapped("sequence"), default=-1) + 1),
                    }
                )
            )
        except QuotaExceeded:
            return self._redirect(f"/my/uo/{uo.id}/subscription", error="quota")
        return self._redirect(f"/my/uo/{uo.id}/vehicles/{vehicle.id}", ok="created")

    def _vehicle_statuses(self):
        return dict(request.env["fireman.vehicle"]._fields["status"].selection)

    def _get_vehicle(self, uo, vehicle_id):
        vehicle = uo.vehicle_ids.filtered(lambda v: v.id == vehicle_id)
        if not vehicle:
            raise request.not_found()
        return vehicle

    @http.route("/my/uo/<int:uo_id>/vehicles/<int:vehicle_id>", type="http", auth="user", website=True)
    def vehicle(self, uo_id, vehicle_id, **kw):
        uo, is_admin = self._get_uo(uo_id)
        vehicle = self._get_vehicle(uo, vehicle_id)
        return request.render(
            "inventory_fireman.portal_vehicle",
            self._values(uo, is_admin, "vehicles", vehicle=vehicle, statuses=self._vehicle_statuses()),
        )

    @http.route(
        "/my/uo/<int:uo_id>/vehicles/<int:vehicle_id>/edit", type="http", auth="user", website=True, methods=["POST"]
    )
    def vehicle_edit(self, uo_id, vehicle_id, action=None, **kw):
        """Toutes les modifications du véhicule passent par ce point d'entrée (champ `action`)."""
        uo, _admin = self._get_uo(uo_id, admin=True)
        vehicle = self._get_vehicle(uo, vehicle_id)
        if action not in VEHICLE_ACTIONS:
            raise request.not_found()
        url = f"/my/uo/{uo.id}/vehicles/{vehicle.id}"
        try:
            anchor = getattr(self, f"_vehicle_{action}")(vehicle, kw)
        except (UserError, ValueError, LookupError):
            _logger.info("Action véhicule %s refusée", action, exc_info=True)
            return self._redirect(url, error="invalid")
        return self._redirect(url, ok="saved", anchor=anchor)

    @http.route(
        "/my/uo/<int:uo_id>/vehicles/<int:vehicle_id>/delete", type="http", auth="user", website=True, methods=["POST"]
    )
    def vehicle_delete(self, uo_id, vehicle_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        self._get_vehicle(uo, vehicle_id).unlink()
        return self._redirect(f"/my/uo/{uo.id}/vehicles", ok="deleted")

    # -- actions de l'éditeur : chacune renvoie l'ancre où revenir -------------
    def _pick(self, records, record_id):
        record = records.filtered(lambda r: r.id == _to_int(record_id))
        if not record:
            raise LookupError(record_id)
        return record

    def _text(self, kw, key):
        value = (kw.get(key) or "").strip()
        if not value:
            raise ValueError(key)
        return value

    def _vehicle_save(self, vehicle, kw):
        status = kw.get("status")
        vehicle.write(
            {
                "name": self._text(kw, "name"),
                "license_plate": (kw.get("license_plate") or "").strip(),
                "status": status if status in self._vehicle_statuses() else vehicle.status,
                "notes": (kw.get("notes") or "").strip(),
            }
        )

    def _vehicle_add_section(self, vehicle, kw):
        section = (
            request.env["fireman.vehicle.section"]
            .sudo()
            .create(
                {
                    "vehicle_id": vehicle.id,
                    "name": self._text(kw, "name"),
                    "sequence": max(vehicle.section_ids.mapped("sequence"), default=-10) + 10,
                }
            )
        )
        return f"section-{section.id}"

    def _vehicle_rename_section(self, vehicle, kw):
        section = self._pick(vehicle.section_ids, kw.get("section_id"))
        section.name = self._text(kw, "name")
        return f"section-{section.id}"

    def _vehicle_delete_section(self, vehicle, kw):
        self._pick(vehicle.section_ids, kw.get("section_id")).unlink()

    def _vehicle_add_item(self, vehicle, kw):
        """Ajoute un équipement à la catégorie, ou au groupe si `group_id` est fourni."""
        Item = request.env["fireman.vehicle.item"].sudo()
        group = self._pick(vehicle.section_ids.item_ids, kw["group_id"]) if kw.get("group_id") else None
        section = group.section_id if group else self._pick(vehicle.section_ids, kw.get("section_id"))
        siblings = group.child_ids if group else section.item_ids.filtered(lambda i: not i.parent_id)
        Item.create(
            {
                "section_id": section.id,
                "parent_id": group.id if group else False,
                "name": self._text(kw, "name"),
                "quantity": max(_to_int(kw.get("quantity"), 1), 1),
                "sequence": max(siblings.mapped("sequence"), default=-10) + 10,
            }
        )
        return f"section-{section.id}"

    def _vehicle_add_group(self, vehicle, kw):
        section = self._pick(vehicle.section_ids, kw.get("section_id"))
        request.env["fireman.vehicle.item"].sudo().create(
            {
                "section_id": section.id,
                "is_group": True,
                "name": self._text(kw, "name"),
                "sequence": max(section.item_ids.filtered(lambda i: not i.parent_id).mapped("sequence"), default=-10)
                + 10,
            }
        )
        return f"section-{section.id}"

    def _vehicle_update_item(self, vehicle, kw):
        item = self._pick(vehicle.section_ids.item_ids, kw.get("item_id"))
        vals = {"name": self._text(kw, "name")}
        if not item.is_group:
            vals["quantity"] = max(_to_int(kw.get("quantity"), 1), 1)
        item.write(vals)
        return f"section-{item.section_id.id}"

    def _vehicle_delete_item(self, vehicle, kw):
        item = self._pick(vehicle.section_ids.item_ids, kw.get("item_id"))
        anchor = f"section-{item.section_id.id}"
        item.unlink()
        return anchor

    def _vehicle_move(self, vehicle, kw):
        """Monte ou descend une catégorie, un équipement ou un équipement de groupe."""
        if kw.get("kind") == "section":
            record = self._pick(vehicle.section_ids, kw.get("id"))
            siblings = vehicle.section_ids
        else:
            record = self._pick(vehicle.section_ids.item_ids, kw.get("id"))
            siblings = (
                record.parent_id.child_ids
                if record.parent_id
                else record.section_id.item_ids.filtered(lambda i: not i.parent_id)
            )
        ordered = siblings.sorted(lambda r: (r.sequence, r.id))
        ids = ordered.ids
        index = ids.index(record.id)
        target = index - 1 if kw.get("direction") == "up" else index + 1
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            for position, record_id in enumerate(ids):
                sibling = ordered.browse(record_id)
                if sibling.sequence != position * 10:
                    sibling.sequence = position * 10
        return f"section-{record.id if kw.get('kind') == 'section' else record.section_id.id}"

    # ------------------------------------------------------------------
    # Personnel
    # ------------------------------------------------------------------
    @http.route("/my/uo/<int:uo_id>/members", type="http", auth="user", website=True)
    def members(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        return request.render("inventory_fireman.portal_members", self._values(uo, is_admin, "members"))

    @http.route("/my/uo/<int:uo_id>/members/invite", type="http", auth="user", website=True, methods=["POST"])
    def member_invite(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        url = f"/my/uo/{uo.id}/members"
        email = (kw.get("email") or "").strip().lower()
        if "@" not in email:
            return self._redirect(url, error="invalid")
        connector = request.env["firebase.connector"]._get_default()
        try:
            found = connector.find_user_by_email(email) if connector else None
        except Exception:
            _logger.exception("Recherche Firebase de %s impossible", email)
            return self._redirect(url, error="failed")
        if not found:
            return self._redirect(url, error="no_account")
        uid, data = found
        Partner = request.env["res.partner"].sudo()
        partner = Partner.search([("firebase_uid", "=", uid)], limit=1) or Partner.search(
            [("email", "=ilike", email), ("is_company", "=", False), ("firebase_uid", "=", False)], limit=1
        )
        if partner:
            partner.firebase_uid = uid
        else:
            partner = Partner.create({"name": email.split("@")[0], "email": email, "firebase_uid": uid})
        if uo.member_ids.filtered(lambda m: m.partner_id == partner):
            return self._redirect(url, error="already_member")
        try:
            request.env["fireman.member"].sudo().create(
                {
                    "uo_id": uo.id,
                    "partner_id": partner.id,
                    "is_admin": bool(kw.get("is_admin")),
                    "is_pharmacist": bool(kw.get("is_pharmacist")),
                    "verified": True,
                }
            )
        except QuotaExceeded:
            return self._redirect(f"/my/uo/{uo.id}/subscription", error="quota")
        return self._redirect(url, ok="invited")

    def _get_member(self, uo, member_id):
        member = uo.member_ids.filtered(lambda m: m.id == member_id and not m.is_super_admin)
        if not member:
            raise request.not_found()
        return member

    def _would_remove_last_admin(self, uo, member, still_admin):
        admins = uo.member_ids.filtered(lambda m: m.is_admin and m.verified and not m.is_super_admin)
        has_super_admin = any(uo.member_ids.mapped("is_super_admin"))
        return member in admins and not still_admin and len(admins) <= 1 and not has_super_admin

    @http.route(
        "/my/uo/<int:uo_id>/members/<int:member_id>/update", type="http", auth="user", website=True, methods=["POST"]
    )
    def member_update(self, uo_id, member_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        member = self._get_member(uo, member_id)
        url = f"/my/uo/{uo.id}/members"
        is_admin, verified = bool(kw.get("is_admin")), bool(kw.get("verified"))
        if self._would_remove_last_admin(uo, member, is_admin and verified):
            return self._redirect(url, error="last_admin")
        try:
            member.write({"is_admin": is_admin, "is_pharmacist": bool(kw.get("is_pharmacist")), "verified": verified})
        except QuotaExceeded:
            return self._redirect(f"/my/uo/{uo.id}/subscription", error="quota")
        return self._redirect(url, ok="saved")

    @http.route(
        "/my/uo/<int:uo_id>/members/<int:member_id>/remove", type="http", auth="user", website=True, methods=["POST"]
    )
    def member_remove(self, uo_id, member_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        member = self._get_member(uo, member_id)
        url = f"/my/uo/{uo.id}/members"
        if self._would_remove_last_admin(uo, member, False):
            return self._redirect(url, error="last_admin")
        member.unlink()
        return self._redirect(url, ok="removed")

    # ------------------------------------------------------------------
    # Abonnement
    # ------------------------------------------------------------------
    @http.route("/my/uo/<int:uo_id>/subscription", type="http", auth="user", website=True)
    def subscription(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        products = request.env["product.template"].sudo().search([("fireman_tier", "!=", False)], order="list_price")
        invoices = (
            request.env["account.move"]
            .sudo()
            .search(
                [("partner_id", "=", uo.partner_id.id), ("move_type", "=", "out_invoice"), ("state", "=", "posted")],
                limit=6,
                order="invoice_date desc, id desc",
            )
        )
        return request.render(
            "inventory_fireman.portal_subscription",
            self._values(uo, is_admin, "subscription", tier_products=products, invoices=invoices, tier_rank=TIER_RANK),
        )

    @http.route("/my/uo/<int:uo_id>/subscription/subscribe", type="http", auth="user", website=True, methods=["POST"])
    def subscription_subscribe(self, uo_id, tier=None, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        url = f"/my/uo/{uo.id}/subscription"
        email = (kw.get("billing_email") or "").strip()
        if tier not in dict(PAID_TIERS) or "@" not in email:
            return self._redirect(url, error="invalid")
        try:
            uo.subscribe(tier, billing_email=email)
        except UserError:
            _logger.warning("Souscription %s impossible pour %s", tier, uo.code, exc_info=True)
            return self._redirect(url, error="failed")
        return self._redirect(url, ok="subscribed")

    @http.route("/my/uo/<int:uo_id>/subscription/cancel", type="http", auth="user", website=True, methods=["POST"])
    def subscription_cancel(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        url = f"/my/uo/{uo.id}/subscription"
        try:
            uo.cancel_subscription((kw.get("reason") or "").strip())
        except UserError:
            return self._redirect(url, error="failed")
        return self._redirect(url, ok="cancelled")

    # ------------------------------------------------------------------
    # Baisse de palier et dépassement des quotas
    # ------------------------------------------------------------------
    def _selection_values(self, uo, tier=None):
        if tier:
            product = request.env["product.template"].sudo().search([("fireman_tier", "=", tier)], limit=1)
            max_vehicles, max_members = product.fireman_max_vehicles, product.fireman_max_members
        else:
            max_vehicles, max_members = uo.max_vehicles, uo.max_members
        vehicles = uo.vehicle_ids.sorted(lambda v: (v.sequence, v.id))
        members = uo._billable_members().sorted(lambda m: (not m.is_admin, m.id))
        # Présélection : le choix déjà fait, sinon ce que l'archivage automatique conserverait.
        kept_vehicles = (uo.keep_vehicle_ids & vehicles) or (
            vehicles if max_vehicles == -1 else vehicles[:max_vehicles]
        )
        kept_members = (uo.keep_member_ids & members) or (members if max_members == -1 else members[:max_members])
        return {
            "tier": tier,
            "max_vehicles": max_vehicles,
            "max_members": max_members,
            "vehicles": vehicles,
            "members": members,
            "checked_vehicles": set(kept_vehicles.ids),
            "checked_members": set(kept_members.ids),
            "too_many_vehicles": max_vehicles != -1 and len(vehicles) > max_vehicles,
            "too_many_members": max_members != -1 and len(members) > max_members,
            "grace_days": uo._quota_grace_days(),
        }

    def _selected_ids(self):
        form = request.httprequest.form
        if form.get("mode") != "choose":
            return [], []
        return (
            [int(x) for x in form.getlist("keep_vehicle") if x.isdigit()],
            [int(x) for x in form.getlist("keep_member") if x.isdigit()],
        )

    @http.route("/my/uo/<int:uo_id>/subscription/downgrade", type="http", auth="user", website=True, methods=["GET"])
    def subscription_downgrade(self, uo_id, tier=None, **kw):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        url = f"/my/uo/{uo.id}/subscription"
        if tier not in dict(PAID_TIERS) or not uo.subscription_id or TIER_RANK[tier] >= TIER_RANK[uo.tier]:
            return self._redirect(url, error="invalid")
        values = self._values(
            uo,
            is_admin,
            "subscription",
            mode="downgrade",
            effective_date=uo.subscription_id.next_invoice_date,
            **self._selection_values(uo, tier),
        )
        return request.render("inventory_fireman.portal_selection", values)

    @http.route(
        "/my/uo/<int:uo_id>/subscription/downgrade/confirm",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def subscription_downgrade_confirm(self, uo_id, tier=None, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        url = f"/my/uo/{uo.id}/subscription"
        email = (kw.get("billing_email") or "").strip()
        if tier not in dict(PAID_TIERS) or (email and "@" not in email):
            return self._redirect(url, error="invalid")
        vehicle_ids, member_ids = self._selected_ids()
        try:
            uo.schedule_downgrade(tier, billing_email=email or None, vehicle_ids=vehicle_ids, member_ids=member_ids)
        except UserError:
            _logger.warning("Baisse de palier %s impossible pour %s", tier, uo.code, exc_info=True)
            return self._redirect(url, error="downgrade_invalid")
        return self._redirect(url, ok="downgrade_scheduled")

    @http.route(
        "/my/uo/<int:uo_id>/subscription/downgrade/cancel",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def subscription_downgrade_cancel(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        uo.cancel_pending_downgrade()
        return self._redirect(f"/my/uo/{uo.id}/subscription", ok="downgrade_cancelled")

    @http.route("/my/uo/<int:uo_id>/quota", type="http", auth="user", website=True, methods=["GET"])
    def quota(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        values = self._selection_values(uo)
        if not (values["too_many_vehicles"] or values["too_many_members"]):
            return self._redirect(f"/my/uo/{uo.id}")
        return request.render(
            "inventory_fireman.portal_selection", self._values(uo, is_admin, "uo", mode="quota", **values)
        )

    @http.route("/my/uo/<int:uo_id>/quota/confirm", type="http", auth="user", website=True, methods=["POST"])
    def quota_confirm(self, uo_id, **kw):
        uo, _admin = self._get_uo(uo_id, admin=True)
        vehicle_ids, member_ids = self._selected_ids()
        if vehicle_ids or member_ids:
            try:
                uo.set_keep_selection(vehicle_ids, member_ids)
            except UserError:
                return self._redirect(f"/my/uo/{uo.id}/quota", error="downgrade_invalid")
            return self._redirect(f"/my/uo/{uo.id}", ok="quota_saved")
        return self._redirect(f"/my/uo/{uo.id}")

    # ------------------------------------------------------------------
    # Reporting (paliers Caserne et Flotte)
    # ------------------------------------------------------------------
    def _reporting_uo(self, uo_id, export=False):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        if not (uo.has_export if export else uo.has_reporting):
            raise AccessError(_("Cette fonctionnalité n'est pas incluse dans votre palier."))
        return uo, is_admin

    @http.route("/my/uo/<int:uo_id>/reporting", type="http", auth="user", website=True)
    def reporting(self, uo_id, **kw):
        uo, is_admin = self._get_uo(uo_id, admin=True)
        if not uo.has_reporting:
            return self._redirect(f"/my/uo/{uo.id}/subscription", error="quota")
        return request.render(
            "inventory_fireman.portal_reporting", self._values(uo, is_admin, "reporting", report=uo._reporting_data())
        )

    @http.route("/my/uo/<int:uo_id>/reporting/pdf", type="http", auth="user", website=True)
    def reporting_pdf(self, uo_id, **kw):
        uo, _admin = self._reporting_uo(uo_id)
        pdf, _fmt = (
            request.env["ir.actions.report"]
            .sudo()
            ._render_qweb_pdf("inventory_fireman.action_report_uo_inventory", uo.ids)
        )
        return request.make_response(
            pdf,
            headers=[
                ("Content-Type", "application/pdf"),
                ("Content-Disposition", f'attachment; filename="rapport_inventaire_{uo.code}.pdf"'),
            ],
        )

    @http.route("/my/uo/<int:uo_id>/reporting/csv", type="http", auth="user", website=True)
    def reporting_csv(self, uo_id, **kw):
        uo, _admin = self._reporting_uo(uo_id, export=True)
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Date", "Véhicule", "Inventoriste", "Grade", "Complet", "Manques", "Commentaire"])
        for inv in uo.inventory_ids:
            writer.writerow(
                [
                    inv.date.strftime("%d/%m/%Y %H:%M") if inv.date else inv.date_text or "",
                    inv.vehicle_name or "",
                    inv.inventor_name or "",
                    inv.rank or "",
                    "Oui" if inv.is_complete else "Non",
                    (inv.lack or "").replace("\n", " | "),
                    (inv.comment or "").replace("\n", " | "),
                ]
            )
        return request.make_response(
            ("\ufeff" + out.getvalue()).encode("utf-8"),
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="inventaires_{uo.code}.csv"'),
            ],
        )
