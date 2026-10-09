# -*- coding: utf-8 -*-
"""Conserve les données de l'ancien modèle (res.partner étendu) avant la suppression de ses colonnes."""
from odoo.tools.sql import column_exists, table_exists


def migrate(cr, version):
    if not column_exists(cr, "res_partner", "is_firebase_uo"):
        return

    cr.execute(
        """
        CREATE TABLE fireman_mig_uo AS
        SELECT id AS partner_id, name, uo_code AS code,
               COALESCE(uo_verified, FALSE) AS verified,
               COALESCE(uo_prefill_qty, FALSE) AS prefill_qty,
               COALESCE(uo_send_inventory_to_all, FALSE) AS send_all
          FROM res_partner
         WHERE is_firebase_uo AND uo_code IS NOT NULL
        """
    )
    if table_exists(cr, "pompier_uo_rel"):
        cr.execute(
            """
            CREATE TABLE fireman_mig_member AS
            SELECT pompier_id AS partner_id, uo_id AS uo_partner_id,
                   COALESCE(uo_admin, FALSE) AS is_admin,
                   COALESCE(uo_pharmacist, FALSE) AS is_pharmacist,
                   COALESCE(verified, FALSE) AS verified,
                   last_connection_date
              FROM pompier_uo_rel
            """
        )
    if column_exists(cr, "sale_order", "firebase_uo_id"):
        cr.execute(
            """
            CREATE TABLE fireman_mig_order AS
            SELECT id AS order_id, firebase_uo_id AS uo_partner_id
              FROM sale_order WHERE firebase_uo_id IS NOT NULL
            """
        )

    cr.execute("ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS fireman_super_admin boolean")
    if column_exists(cr, "res_partner", "admin"):
        cr.execute("UPDATE res_partner SET fireman_super_admin = COALESCE(admin, FALSE) WHERE is_firebase_pompier")

    if column_exists(cr, "product_template", "max_vehicle"):
        cr.execute("ALTER TABLE product_template ADD COLUMN IF NOT EXISTS fireman_max_vehicles integer")
        cr.execute("ALTER TABLE product_template ADD COLUMN IF NOT EXISTS fireman_max_members integer")
        cr.execute("UPDATE product_template SET fireman_max_vehicles = max_vehicle, fireman_max_members = max_user")
