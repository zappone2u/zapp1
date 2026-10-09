# -*- coding: utf-8 -*-

TIER_DISCOVERY = "discovery"
PAID_TIERS = [
    ("essential", "Essentiel"),
    ("caserne", "Caserne"),
    ("flotte", "Flotte"),
]
TIERS = [(TIER_DISCOVERY, "Découverte")] + PAID_TIERS

# Quotas du palier gratuit
DISCOVERY_MAX_VEHICLES = 1
DISCOVERY_MAX_MEMBERS = 1
UNLIMITED = -1

# Fonctionnalités portail par palier
REPORTING_TIERS = ("caserne", "flotte")
EXPORT_TIERS = ("flotte",)

# sale_subscription : abonnement en cours de validité
ACTIVE_SUBSCRIPTION_STATES = ("3_progress", "4_paused")

VEHICLE_STATUSES = [
    ("nothing", "En état"),
    ("broken", "Cassé"),
    ("repair", "En réparation"),
]

# Périmètres de lecture Firebase → Odoo
SCOPE_UO = "uo"
SCOPE_VEHICLES = "vehicles"
SCOPE_INVENTORIES = "inventories"
SCOPE_MEMBERS = "members"
SCOPES = (SCOPE_UO, SCOPE_VEHICLES, SCOPE_INVENTORIES, SCOPE_MEMBERS)

MAX_PUSH_ATTEMPTS = 5
