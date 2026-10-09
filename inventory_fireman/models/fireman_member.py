# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class FiremanMember(models.Model):
    """Appartenance d'une personne à une UO (entrée de `users/{uid}.uo` dans Firestore)."""

    _name = "fireman.member"
    _description = "Membre d'une UO"
    _inherit = ["fireman.sync.mixin"]
    _order = "uo_id, partner_id"
    _push_fields = ("uo_id", "partner_id", "is_admin", "is_pharmacist", "verified")

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("res.partner", string="Personne", required=True, ondelete="cascade", index=True)
    name = fields.Char(related="partner_id.name", string="Nom")
    email = fields.Char(related="partner_id.email", string="Email")
    firebase_uid = fields.Char(related="partner_id.firebase_uid", string="UID Firebase")
    is_super_admin = fields.Boolean(related="partner_id.fireman_super_admin", string="Super administrateur")
    is_admin = fields.Boolean(string="Administrateur de l'UO")
    is_pharmacist = fields.Boolean(string="Pharmacien")
    verified = fields.Boolean(string="Validé", help="Décoché : demande d'accès en attente.")
    last_connection = fields.Datetime(string="Dernière connexion", readonly=True)

    _sql_constraints = [("uo_partner_unique", "unique(uo_id, partner_id)", "Cette personne est déjà membre de l'UO.")]

    # ------------------------------------------------------------------
    # Quotas
    # ------------------------------------------------------------------
    def _is_billable(self):
        return self.verified and not self.partner_id.fireman_super_admin

    def _check_quota(self, adding):
        for uo in adding.uo_id:
            uo._check_member_quota(len(adding.filtered(lambda m: m.uo_id == uo)))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if self._push_enabled() and not self.env.context.get("fireman_skip_quota"):
            records.filtered(lambda m: m._is_billable())._check_quota_after_create()
        return records

    def _check_quota_after_create(self):
        # Le décompte inclut déjà les nouveaux membres : on vérifie qu'il reste dans la limite.
        for uo in self.uo_id:
            uo._check_member_quota(0)

    def write(self, vals):
        if vals.get("verified") and self._push_enabled() and not self.env.context.get("fireman_skip_quota"):
            newly = self.filtered(lambda m: not m.verified and not m.partner_id.fireman_super_admin)
            if newly:
                self._check_quota(newly)
        return super().write(vals)

    # ------------------------------------------------------------------
    # Firebase
    # ------------------------------------------------------------------
    def _push_target_key(self):
        self.ensure_one()
        partner = self.partner_id
        return f"{self.uo_id.code}/{partner.firebase_uid or 'mail:%s' % (partner.email or partner.id)}"

    def _push_uo_code(self):
        return self.uo_id.code

    def _ensure_firebase_uid(self, connector):
        partner = self.partner_id
        if partner.firebase_uid:
            return partner.firebase_uid
        if not partner.email:
            raise UserError(_("%s n'a pas d'email : impossible de retrouver son compte Firebase.", partner.name))
        found = connector.find_user_by_email(partner.email)
        if not found:
            raise UserError(
                _(
                    "Aucun compte Firebase pour %s : la personne doit d'abord créer son compte dans l'application.",
                    partner.email,
                )
            )
        partner.sudo().with_context(fireman_no_push=True).firebase_uid = found[0]
        return found[0]

    def _push_upsert(self, connector):
        self.ensure_one()
        uid = self._ensure_firebase_uid(connector)
        connector.set_user_uo_entry(
            uid,
            self.uo_id.code,
            {
                "uo_name": self.uo_id.code,
                "uo_admin": bool(self.is_admin),
                "uo_pharmacist": bool(self.is_pharmacist),
                "verified": bool(self.verified),
            },
        )

    def _push_delete_payload(self):
        self.ensure_one()
        if not self.partner_id.firebase_uid:
            return None
        return {"code": self.uo_id.code, "uid": self.partner_id.firebase_uid}

    @api.model
    def _push_delete(self, connector, payload):
        connector.set_user_uo_entry(payload["uid"], payload["code"], None)
