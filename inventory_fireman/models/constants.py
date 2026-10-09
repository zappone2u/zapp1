# -*- coding: utf-8 -*-

TIER_DISCOVERY = "discovery"
PAID_TIERS = [
    ("essential", "Essentiel"),
    ("caserne", "Caserne"),
    ("flotte", "Flotte"),
]
TIERS = [(TIER_DISCOVERY, "Découverte")] + PAID_TIERS
TIER_RANK = {TIER_DISCOVERY: 0, "essential": 1, "caserne": 2, "flotte": 3}

# Baisse de palier : délai (jours) laissé à l'UO pour choisir ce qu'elle garde avant l'archivage automatique.
# Paramétrable dans Paramètres > Inventory Fireman.
QUOTA_GRACE_PARAM = "inventory_fireman.quota_grace_days"
DEFAULT_QUOTA_GRACE_DAYS = 30

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
