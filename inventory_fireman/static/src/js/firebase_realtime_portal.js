/** @odoo-module **/

/**
 * Rafraîchissement temps réel des pages portail Inventory Fireman.
 *
 * Quand une donnée d'une UO change dans Firebase (inventaire, véhicule, statut),
 * le webhook Odoo émet une notification bus sur le canal
 * "inventory_fireman_uo_<id>". Ce script y souscrit pour les UO affichées sur la
 * page courante et recharge automatiquement le contenu — sauf si l'utilisateur
 * est en train de saisir un formulaire.
 */

import { registry } from "@web/core/registry";
import { browser } from "@web/core/browser/browser";

export const firebaseRealtimePortalService = {
    dependencies: ["bus_service"],

    start(env, { bus_service }) {
        const holder = document.querySelector("[data-fireman-uo-ids]");
        if (!holder) {
            return; // Page sans données temps réel : rien à faire.
        }

        const uoIds = (holder.dataset.firemanUoIds || "")
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);

        if (!uoIds.length) {
            return;
        }

        let reloadTimer = null;
        const scheduleReload = () => {
            // Ne pas interrompre une saisie en cours.
            const active = document.activeElement;
            if (active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
                return;
            }
            browser.clearTimeout(reloadTimer);
            reloadTimer = browser.setTimeout(() => browser.location.reload(), 600);
        };

        for (const id of uoIds) {
            bus_service.addChannel(`inventory_fireman_uo_${id}`);
        }
        bus_service.subscribe("inventory_fireman_sync", () => scheduleReload());
        bus_service.start();
    },
};

registry.category("services").add("inventory_fireman_realtime_portal", firebaseRealtimePortalService);
