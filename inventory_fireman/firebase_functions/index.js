/**
 * Cloud Functions Firebase — Synchronisation temps réel vers Odoo
 * =================================================================
 *
 * Ces fonctions écoutent les changements dans la Realtime Database Firebase
 * et appellent immédiatement les webhooks Odoo pour mettre à jour les données.
 *
 * STRUCTURE REALTIME DATABASE attendue :
 *   {UO_CODE}/inventory/{inventoryId}/   → inventaires
 *   {UO_CODE}/vehicle/{vehicleId}/       → véhicules
 *   users/{firebaseUid}/                 → profils pompiers
 *
 * CONFIGURATION (fichier .env dans ce dossier — chargé automatiquement au déploiement) :
 *   ODOO_URL=https://zappone.fr
 *   ODOO_WEBHOOK_SECRET=le_meme_secret_que_dans_odoo
 *
 * DÉPLOIEMENT :
 *   cd firebase_functions
 *   npm install
 *   firebase login
 *   firebase deploy --only functions
 */

const functions = require("firebase-functions");
const admin = require("firebase-admin");

admin.initializeApp();

// ─── Configuration ────────────────────────────────────────────────────────────
// Région et instance Realtime Database (la base est en europe-west1).
const REGION = "europe-west1";
const DB_INSTANCE = "sierck-inventory-default-rtdb";

// Lues depuis le fichier .env (cf. .env.example).
const ODOO_URL = process.env.ODOO_URL || "https://votre-odoo.example.com";
const WEBHOOK_SECRET = process.env.ODOO_WEBHOOK_SECRET || "fireman_webhook_secret_change_me";

// fetch est natif à partir de Node 18 (runtime Node 20 ici) — aucune dépendance requise.

/**
 * Envoie un payload JSON à un endpoint Odoo webhook.
 * @param {string} path  ex: "/firebase/webhook/inventory"
 * @param {object} body  payload JSON
 */
async function callOdooWebhook(path, body) {
  const url = `${ODOO_URL}${path}`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Firebase-Webhook-Secret": WEBHOOK_SECRET,
      },
      body: JSON.stringify(body),
      // Timeout 10 secondes
      signal: AbortSignal.timeout(10000),
    });

    const text = await response.text();
    if (!response.ok) {
      functions.logger.error(`Webhook Odoo ${path} → HTTP ${response.status}: ${text}`);
    } else {
      functions.logger.info(`Webhook Odoo ${path} → OK: ${text}`);
    }
    return response.ok;
  } catch (err) {
    functions.logger.error(`Erreur appel webhook Odoo ${path}:`, err.message);
    return false;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  INVENTAIRES  —  {UO_CODE}/inventor_history/{inventoryId}
//  Déclenché à chaque création, modification ou suppression d'un inventaire.
// ─────────────────────────────────────────────────────────────────────────────

exports.syncInventoryToOdoo = functions
  .region(REGION)
  .database.instance(DB_INSTANCE)
  .ref("/{uoCode}/inventor_history/{inventoryId}")
  .onWrite(async (change, context) => {
    const { uoCode, inventoryId } = context.params;

    // Suppression
    if (!change.after.exists()) {
      return callOdooWebhook("/firebase/webhook/inventory", {
        event: "deleted",
        firebase_uid: inventoryId,
        uo_code: uoCode,
        data: {},
      });
    }

    const data = change.after.val();
    const event = change.before.exists() ? "updated" : "created";

    return callOdooWebhook("/firebase/webhook/inventory", {
      event,
      firebase_uid: inventoryId,
      uo_code: uoCode,
      data: {
        inventoryDateStr: data.inventoryDateStr || data.inventory_date_str || data.date || "",
        inventorName: data.inventorName || data.inventor_name || data.name || "",
        rank: data.rank || "",
        inventorFull: !!(data.inventorFull || data.inventor_full),
        vehicle: data.vehicle || "",
        lack: data.lack || "",
        moreDescription: data.moreDescription || data.more_description || "",
      },
    });
  });

// ─────────────────────────────────────────────────────────────────────────────
//  VÉHICULES  —  {UO_CODE}/vehicle/{vehicleId}
//  Déclenché à chaque création, modification ou suppression d'un véhicule.
// ─────────────────────────────────────────────────────────────────────────────

exports.syncVehicleToOdoo = functions
  .region(REGION)
  .database.instance(DB_INSTANCE)
  .ref("/{uoCode}/vehicle/{vehicleId}")
  .onWrite(async (change, context) => {
    const { uoCode, vehicleId } = context.params;

    // Suppression
    if (!change.after.exists()) {
      return callOdooWebhook("/firebase/webhook/vehicle", {
        event: "deleted",
        firebase_uid: vehicleId,
        uo_code: uoCode,
        data: {},
      });
    }

    const data = change.after.val();
    const event = change.before.exists() ? "updated" : "created";

    // On ne synchronise que les métadonnées du véhicule (pas le contenu produits,
    // car celui-ci est géré par la sync Odoo → Firebase dans portal.py)
    return callOdooWebhook("/firebase/webhook/vehicle", {
      event,
      firebase_uid: vehicleId,
      uo_code: uoCode,
      data: {
        label: data.label || "",
        licensePlate: data.licensePlate || data.license_plate || "",
        status: data.status || "nothing",
        notes: data.notes || "",
        position: data.position || 0,
        verified: !!data.verified,
      },
    });
  });

// ─────────────────────────────────────────────────────────────────────────────
//  PROFILS POMPIERS  —  users/{firebaseUid}
//  Déclenché quand un pompier met à jour son profil dans l'appli mobile.
//  On ne synchronise que les champs "légers" (grade, statut, dernière connexion).
// ─────────────────────────────────────────────────────────────────────────────

exports.syncPompierToOdoo = functions
  .region(REGION)
  .database.instance(DB_INSTANCE)
  .ref("/users/{firebaseUid}")
  .onWrite(async (change, context) => {
    const { firebaseUid } = context.params;

    if (!change.after.exists()) {
      return callOdooWebhook("/firebase/webhook/pompier", {
        event: "deleted",
        firebase_uid: firebaseUid,
        data: {},
      });
    }

    const data = change.after.val();

    // Éviter les boucles : si la modif vient uniquement de last_sync_date (écrit par Odoo), ignorer
    if (change.before.exists()) {
      const before = change.before.val();
      const fieldsToWatch = ["name", "grade", "matricule", "status", "email"];
      const hasChanged = fieldsToWatch.some((f) => before[f] !== data[f]);
      if (!hasChanged) {
        functions.logger.info(`syncPompierToOdoo: aucun champ pertinent modifié pour ${firebaseUid}, ignoré`);
        return null;
      }
    }

    const event = change.before.exists() ? "updated" : "created";

    return callOdooWebhook("/firebase/webhook/pompier", {
      event,
      firebase_uid: firebaseUid,
      data: {
        name: data.name || data.displayName || "",
        email: data.email || "",
        grade: data.grade || "",
        matricule: data.matricule || "",
        status: data.status || "active",
        last_login: data.lastLogin || data.last_login || null,
      },
    });
  });

// ─────────────────────────────────────────────────────────────────────────────
//  PARAMÈTRES UO  —  {UO_CODE}/settings
//  Déclenché quand l'admin modifie les paramètres de l'UO dans l'appli.
// ─────────────────────────────────────────────────────────────────────────────

exports.syncUoSettingsToOdoo = functions
  .region(REGION)
  .database.instance(DB_INSTANCE)
  .ref("/{uoCode}/settings")
  .onWrite(async (change, context) => {
    const { uoCode } = context.params;

    if (!change.after.exists()) {
      return null; // Suppression des settings → on ne fait rien côté Odoo
    }

    const data = change.after.val();

    return callOdooWebhook("/firebase/webhook/uo", {
      event: "updated",
      uo_code: uoCode,
      data: {
        prefillTheQty: !!data.prefillTheQty,
        sendInventoryToAll: !!data.sendInventoryToAll,
        verified: !!data.verified,
      },
    });
  });
