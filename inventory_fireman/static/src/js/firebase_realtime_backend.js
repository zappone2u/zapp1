/** @odoo-module **/

/**
 * Service de synchronisation temps réel Firebase → backend Odoo.
 * Écoute le canal bus "inventory_fireman" et recharge automatiquement la vue
 * courante (liste/formulaire) dès qu'un inventaire, véhicule ou UO change dans
 * Firebase — sans rafraîchissement manuel.
 */

import { registry } from "@web/core/registry";
import { browser } from "@web/core/browser/browser";

const RELOADABLE_MODELS = new Set([
    "firebase.vehicle",
    "firebase.inventory.history",
    "res.partner",
]);

export const firebaseRealtimeService = {
    dependencies: ["bus_service", "action"],

    start(env, { bus_service, action }) {
        let reloadTimer = null;

        const scheduleReload = () => {
            // Anti-rebond : regrouper plusieurs notifications rapprochées en un
            // seul rechargement pour éviter de saturer la vue.
            browser.clearTimeout(reloadTimer);
            reloadTimer = browser.setTimeout(() => {
                const controller = action.currentController;
                if (controller) {
                    action.doAction("soft_reload");
                }
            }, 400);
        };

        bus_service.addChannel("inventory_fireman");
        bus_service.subscribe("inventory_fireman_sync", (payload) => {
            if (!payload || RELOADABLE_MODELS.has(payload.model)) {
                scheduleReload();
            }
        });
        bus_service.start();
    },
};

registry.category("services").add("inventory_fireman_realtime", firebaseRealtimeService);
