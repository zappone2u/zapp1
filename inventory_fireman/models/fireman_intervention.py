# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .utils import as_int, as_list, to_naive_utc


class FiremanIntervention(models.Model):
    """Articles utilisés lors d'un retour d'intervention, à remplacer dans le véhicule.

    Copie lecture seule de Firebase : le réarmement se valide dans l'application."""

    _name = "fireman.intervention"
    _description = "Retour d'intervention"
    _order = "date desc, id desc"

    uo_id = fields.Many2one("fireman.uo", string="UO", required=True, ondelete="cascade", index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, index=True, copy=False)
    date = fields.Datetime(string="Date")
    vehicle_name = fields.Char(string="Véhicule")
    vehicle_uid = fields.Char(string="Identifiant du véhicule")
    rank = fields.Char(string="Grade")
    inventor_name = fields.Char(string="Déclarant")
    note = fields.Text(string="Remarque")
    line_ids = fields.One2many("fireman.intervention.line", "intervention_id", string="Articles utilisés")
    state = fields.Selection(
        [("pending", "À réarmer"), ("done", "Réarmé")],
        string="État",
        compute="_compute_state",
        store=True,
        index=True,
    )

    _sql_constraints = [("uid_uo_unique", "unique(uo_id, uid)", "Retour d'intervention déjà enregistré.")]

    @api.depends("line_ids.restocked")
    def _compute_state(self):
        for intervention in self:
            pending = intervention.line_ids.filtered(lambda line: not line.restocked)
            intervention.state = "pending" if pending else "done"

    @api.model
    def _vals_from_firebase(self, data):
        return {
            "date": to_naive_utc(data.get("date")),
            "vehicle_name": str(data.get("vehicle") or ""),
            "vehicle_uid": str(data.get("vehicleId") or ""),
            "rank": str(data.get("rank") or ""),
            "inventor_name": str(data.get("inventorName") or ""),
            "note": str(data.get("note") or ""),
        }

    def _apply_firebase_lines(self, raw):
        """Aligne les lignes sur Firebase (clés = identifiants de ligne)."""
        self.ensure_one()
        Line = self.env["fireman.intervention.line"]
        remote = raw if isinstance(raw, dict) else {}
        local = {line.uid: line for line in self.line_ids}
        for uid, data in remote.items():
            if not isinstance(data, dict):
                continue
            vals = Line._vals_from_firebase(data)
            line = local.get(uid)
            if line:
                changes = {k: v for k, v in vals.items() if line[k] != v and (line[k] or v)}
                if changes:
                    line.write(changes)
            else:
                Line.create({**vals, "intervention_id": self.id, "uid": uid})
        Line.browse([line.id for uid, line in local.items() if uid not in remote]).unlink()


class FiremanInterventionLine(models.Model):
    _name = "fireman.intervention.line"
    _description = "Article utilisé en intervention"
    _order = "intervention_id, name, id"

    intervention_id = fields.Many2one("fireman.intervention", required=True, ondelete="cascade", index=True)
    uo_id = fields.Many2one(related="intervention_id.uo_id", store=True, index=True)
    uid = fields.Char(string="Identifiant Firebase", required=True, copy=False)
    name = fields.Char(string="Article", required=True)
    item_uid = fields.Char(string="Identifiant de l'article de pharmacie")
    quantity = fields.Integer(string="Quantité utilisée")
    restocked = fields.Boolean(string="Réarmé")
    restocked_date = fields.Datetime(string="Réarmé le")
    restocked_by = fields.Char(string="Réarmé par")
    picked_quantity = fields.Integer(string="Prélevé en pharmacie")
    lots = fields.Char(string="Lots prélevés")

    _sql_constraints = [("uid_intervention_unique", "unique(intervention_id, uid)", "Ligne déjà enregistrée.")]

    @api.model
    def _vals_from_firebase(self, data):
        picks = [p for p in as_list(data.get("picks")) if isinstance(p, dict)]
        quantities = [max(as_int(p.get("quantity")), 0) for p in picks]
        lots = ", ".join(f"{q} × {p.get('lot') or 'sans lot'}" for p, q in zip(picks, quantities) if q)
        return {
            "name": str(data.get("name") or "(sans nom)"),
            "item_uid": str(data.get("itemId") or ""),
            "quantity": max(as_int(data.get("quantity")), 0),
            "restocked": bool(data.get("restocked")),
            "restocked_date": to_naive_utc(data.get("restockedDate")),
            "restocked_by": str(data.get("restockedBy") or ""),
            "picked_quantity": sum(quantities),
            "lots": lots,
        }
