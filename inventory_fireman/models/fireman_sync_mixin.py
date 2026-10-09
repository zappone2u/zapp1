# -*- coding: utf-8 -*-
from odoo import api, models


class FiremanSyncMixin(models.AbstractModel):
    """Met en file un push Firebase à chaque création / modification / suppression.

    Un modèle qui hérite du mixin implémente :
      * ``_push_target_key()`` : clé naturelle de la cible Firebase (None = ignorer) ;
      * ``_push_uo_code()``    : code UO (pour l'affichage et les chemins) ;
      * ``_push_upsert(connector)`` : écrit l'enregistrement dans Firebase ;
      * ``_push_delete_payload()`` / ``_push_delete(connector, payload)`` : suppression ;
      * ``_push_owner()`` : enregistrement dont le push inclut celui-ci (sections → véhicule).

    Le contexte ``fireman_no_push`` désactive la mise en file : il est posé par les
    lectures Firebase → Odoo pour éviter les échos.
    """

    _name = "fireman.sync.mixin"
    _description = "Synchronisation Odoo → Firebase"

    _push_fields = ()  # vide = tout changement déclenche un push

    def _push_owner(self):
        return self

    def _push_target_key(self):
        raise NotImplementedError

    def _push_uo_code(self):
        raise NotImplementedError

    def _push_upsert(self, connector):
        raise NotImplementedError

    def _push_delete_payload(self):
        return None

    @api.model
    def _push_delete(self, connector, payload):
        raise NotImplementedError

    def _push_enabled(self):
        return not self.env.context.get("fireman_no_push")

    def _push_enqueue(self):
        if not self._push_enabled():
            return
        Event = self.env["fireman.sync.event"]
        for owner in self._push_owner():
            key = owner._push_target_key()
            if key:
                Event._enqueue_push(owner._name, owner.id, key, owner._push_uo_code())

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._push_enqueue()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self._push_fields or set(vals) & set(self._push_fields):
            self._push_enqueue()
        return res

    def unlink(self):
        deletions, owners = [], []
        if self._push_enabled():
            for record in self:
                owner = record._push_owner()
                if owner == record:
                    payload = record._push_delete_payload()
                    if payload:
                        deletions.append((record._name, record._push_target_key(), record._push_uo_code(), payload))
                else:
                    owners.append((owner._name, owner.id))
        res = super().unlink()
        Event = self.env["fireman.sync.event"]
        for model, key, code, payload in deletions:
            Event._enqueue_push(model, 0, key, code, "delete", payload)
        for model, res_id in set(owners):
            self.env[model].browse(res_id).exists()._push_enqueue()
        return res
