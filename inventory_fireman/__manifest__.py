# -*- coding: utf-8 -*-
#
# File: __manifest__.py
{
    "name": "Inventory Fireman",
    "summary": "Gestion d'inventaire pour équipements de pompiers avec synchronisation Firebase",
    "description": """
Module de gestion d'inventaire pour équipements de pompiers
============================================================

Fonctionnalités principales :
* Synchronisation avec Firebase (UO et Pompiers)
* Gestion des Unités Organisationnelles (Entreprises) via res.partner
* Gestion des Pompiers (Employés) via res.partner
* Système d'abonnements natif Odoo (sale_subscription)
* Support des pompiers avec plusieurs UO
* Synchronisation automatique planifiée
    """,
    "category": "Operations/Inventory",
    "author": "ZappOne",
    "version": "18.0.1.0.0",
    "website": "https://www.open-net.ch",
    "license": "OPL-1",
    "depends": [
        "base",
        "contacts",
        "mail",
        "sale_management",  # Module de vente
        "portal",  # Module portail pour l'accès web
        "website",  # Pour les templates web
        "account",  # Pour la facturation
        "helpdesk",  # Pour la création de tickets depuis le formulaire contact
        "http_routing",
    ],
    "data": [
        # Security
        "security/ir.model.access.csv",
        # Data
        "data/ir_cron.xml",
        "data/subscription_products.xml",
        # Views
        "views/firebase_connector_views.xml",
        "views/firebase_uo_views.xml",
        "views/firebase_pompier_views.xml",
        "views/pompier_uo_rel_views.xml",
        "views/firebase_inventory_history_views.xml",
        "views/firebase_vehicle_views.xml",
        "views/home_page_new.xml",
        "views/portal_templates.xml",
        "views/portal_subscription_choose.xml",
        "views/portal_subscription_activate.xml",
        "views/website_menu.xml",
        "views/menu.xml",
        "views/portal_breadcrumbs.xml",
        "views/product_template.xml",
        "views/portal_contact.xml",
        "views/website_footer.xml",
        "views/website_customizations.xml",
        "views/report_uo_inventory.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "inventory_fireman/static/src/js/firebase_realtime_backend.js",
        ],
        "web.assets_frontend": [
            "inventory_fireman/static/src/css/custom_portal.css",
            "inventory_fireman/static/src/css/portal_custom.css",
            "inventory_fireman/static/src/js/firebase_realtime_portal.js",
        ],
    },
    "installable": True,
    "auto_install": False,
    "application": True,
}
