# -*- coding: utf-8 -*-
{
    "name": "Bank Sync Manager",
    "version": "18.0.1.0.0",
    "category": "Accounting",
    "summary": "Custom Bank Synchronization with Invoice Matching and Subscription Management",
    "description": """
Bank Sync Manager
=================
Ce module permet de :
* Se connecter directement à votre banque (bypass de la synchronisation Odoo standard)
* Récupérer automatiquement les transactions bancaires
* Matcher automatiquement les transactions avec les factures
    """,
    "author": "ZappOne2U",
    "website": "https://www.zappone2u.com",
    "depends": [
        "base",
        "account",
        "sale",
        "sale_subscription",
        "mail",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/email_templates.xml",
        "views/bank_connection_views.xml",
        "views/bank_transaction_views.xml",
        "views/invoice_matching_views.xml",
        "views/subscription_monitor_views.xml",
        "wizards/revolut_import_wizard_views.xml",
        "views/menu.xml",
        "wizards/manual_sync_wizard_views.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "LGPL-3",
}
