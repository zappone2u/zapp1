# -*- coding: utf-8 -*-
"""Recrée les UO, les membres et les liens d'abonnement à partir des tables conservées par la pré-migration.

Véhicules, inventaires et appartenances sont ensuite relus depuis Firebase par la file de synchronisation.
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools.sql import table_exists

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not table_exists(cr, "fireman_mig_uo"):
        return
    env = api.Environment(cr, SUPERUSER_ID, {"fireman_no_push": True})
    Uo, Member = env["fireman.uo"], env["fireman.member"]

    cr.execute("SELECT partner_id, name, code, verified, prefill_qty, send_all FROM fireman_mig_uo")
    uo_by_partner = {}
    for partner_id, name, code, verified, prefill, send_all in cr.fetchall():
        try:
            with cr.savepoint():
                uo_by_partner[partner_id] = Uo.create(
                    {
                        "name": name,
                        "code": code,
                        "partner_id": partner_id,
                        "verified": verified,
                        "prefill_qty": prefill,
                        "send_inventory_to_all": send_all,
                    }
                )
        except Exception:
            _logger.exception("Migration de l'UO %s impossible", code)

    if table_exists(cr, "fireman_mig_member"):
        cr.execute(
            "SELECT partner_id, uo_partner_id, is_admin, is_pharmacist, verified, last_connection_date FROM fireman_mig_member"
        )
        for partner_id, uo_partner_id, is_admin, is_pharmacist, verified, last_connection in cr.fetchall():
            uo = uo_by_partner.get(uo_partner_id)
            if not uo:
                continue
            try:
                with cr.savepoint():
                    Member.create(
                        {
                            "uo_id": uo.id,
                            "partner_id": partner_id,
                            "is_admin": is_admin,
                            "is_pharmacist": is_pharmacist,
                            "verified": verified,
                            "last_connection": last_connection,
                        }
                    )
            except Exception:
                _logger.exception("Migration du membre %s de l'UO %s impossible", partner_id, uo.code)

    if table_exists(cr, "fireman_mig_order"):
        for partner_id, uo in uo_by_partner.items():
            cr.execute(
                "UPDATE sale_order SET fireman_uo_id = %s WHERE id IN "
                "(SELECT order_id FROM fireman_mig_order WHERE uo_partner_id = %s)",
                (uo.id, partner_id),
            )
        env["sale.order"].invalidate_model(["fireman_uo_id"])

    # Relecture complète de chaque UO (véhicules, inventaires, membres) par le cron de synchronisation.
    pull_env = api.Environment(cr, SUPERUSER_ID, {})
    for uo in uo_by_partner.values():
        pull_env["fireman.sync.event"].enqueue_pull(uo.code)

    for table in ("fireman_mig_uo", "fireman_mig_member", "fireman_mig_order"):
        cr.execute(f"DROP TABLE IF EXISTS {table}")
