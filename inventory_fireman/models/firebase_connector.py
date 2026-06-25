# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import UserError
import json
import logging

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, db, auth, firestore
except ImportError:
    _logger.warning("Firebase Admin SDK non installé. Installez avec: pip install firebase-admin")
    firebase_admin = None


def convert_firestore_to_json(data):
    """Convertir les objets Firestore (comme DatetimeWithNanoseconds) en objets JSON sérialisables"""
    if data is None:
        return None
    elif isinstance(data, dict):
        return {key: convert_firestore_to_json(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [convert_firestore_to_json(item) for item in data]
    elif hasattr(data, "isoformat"):  # datetime objects
        return data.isoformat()
    else:
        return data


def convert_firebase_datetime_to_naive(dt_value):
    """Convertir un datetime Firebase (avec timezone) en datetime naive pour Odoo

    Args:
        dt_value: datetime object (peut être aware ou naive) ou False

    Returns:
        datetime naive ou False si dt_value est False/None
    """
    if not dt_value:
        return False

    # Si c'est déjà un datetime
    if hasattr(dt_value, "tzinfo"):
        # Si le datetime a une timezone, le convertir en naive (UTC)
        if dt_value.tzinfo is not None:
            # Remplacer la timezone par None pour avoir un datetime naive
            return dt_value.replace(tzinfo=None)
        # Sinon, c'est déjà naive
        return dt_value

    # Si c'est une string, essayer de la parser
    if isinstance(dt_value, str):
        from datetime import datetime

        try:
            # Parser la date ISO avec timezone
            dt = datetime.fromisoformat(dt_value.replace("Z", "+00:00"))
            # Retourner sans timezone
            return dt.replace(tzinfo=None)
        except Exception:
            return False

    return False


class FirebaseConnector(models.Model):
    """Service de connexion et synchronisation avec Firebase"""

    _name = "firebase.connector"
    _description = "Connecteur Firebase"

    name = fields.Char(string="Nom de la connexion", required=True, default="Firebase Connection")

    # Configuration Firebase
    database_url = fields.Char(string="Database URL", required=True, help="Ex: https://votre-projet.firebaseio.com")
    service_account_path = fields.Char(
        string="Chemin Service Account JSON", help="Chemin vers le fichier service_account.json"
    )
    service_account_json = fields.Text(string="Service Account JSON", help="Contenu du fichier JSON de service account")

    # Chemins dans la base Firebase
    users_path = fields.Char(
        string="Chemin Users dans Firebase",
        default="users",
        help="Chemin vers les données des utilisateurs dans Firebase",
    )
    uo_path = fields.Char(
        string="Chemin UO dans Firebase (optionnel)", default="uo", help="Chemin vers les données des UO dans Firebase"
    )
    pompiers_path = fields.Char(
        string="Chemin Pompiers dans Firebase (optionnel)",
        default="pompiers",
        help="Chemin vers les données des pompiers dans Firebase",
    )

    # Statut de connexion
    is_connected = fields.Boolean(string="Connecté", default=False, readonly=True)
    last_connection_test = fields.Datetime(string="Dernier test de connexion", readonly=True)
    connection_error = fields.Text(string="Erreur de connexion", readonly=True)

    # Statistiques
    last_sync_date = fields.Datetime(string="Dernière synchronisation complète", readonly=True)
    total_uo_synced = fields.Integer(string="UO synchronisées", readonly=True)
    total_pompiers_synced = fields.Integer(string="Pompiers synchronisés", readonly=True)

    # Paramètres de synchronisation
    auto_sync = fields.Boolean(string="Synchronisation automatique", default=True)
    sync_interval = fields.Integer(string="Intervalle de sync (minutes)", default=60)

    # Webhook push (Firebase → Odoo)
    webhook_secret = fields.Char(
        string="Secret Webhook",
        copy=False,
        help=(
            "Clé secrète partagée entre Firebase Cloud Functions et Odoo. "
            "Doit être identique à la variable ODOO_WEBHOOK_SECRET dans vos Cloud Functions. "
            "Laissez vide pour utiliser la clé par défaut (non recommandé en production)."
        ),
    )

    _sql_constraints = [("name_unique", "unique(name)", "Le nom de la connexion doit être unique!")]

    def _get_firebase_app(self):
        """Initialiser et retourner l'app Firebase"""
        self.ensure_one()

        if not firebase_admin:
            raise UserError(
                "Le module firebase-admin n'est pas installé. Installez-le avec: pip install firebase-admin"
            )

        try:
            # Vérifier si l'app existe déjà
            app = firebase_admin.get_app(self.name)
            return app
        except ValueError:
            # L'app n'existe pas, la créer
            if self.service_account_json:
                # Utiliser le JSON fourni directement
                try:
                    service_account_info = json.loads(self.service_account_json)
                    cred = credentials.Certificate(service_account_info)
                except json.JSONDecodeError as e:
                    raise UserError(f"Le contenu JSON du service account est invalide: {str(e)}")
            elif self.service_account_path and self.service_account_path.strip() not in ["", "/"]:
                # Utiliser le fichier JSON seulement si le chemin est valide
                import os

                if not os.path.isfile(self.service_account_path):
                    raise UserError(f"Le fichier service account n'existe pas: {self.service_account_path}")
                cred = credentials.Certificate(self.service_account_path)
            else:
                raise UserError(
                    "Vous devez fournir soit le contenu JSON du service account, soit un chemin valide vers le fichier."
                )

            app = firebase_admin.initialize_app(cred, {"databaseURL": self.database_url}, name=self.name)

            return app

    # ──────────────────────────────────────────────────────────────────────────
    #  Helpers temps réel (push incrémental Odoo → Firebase + notifications bus)
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_default(self):
        """Retourne le connecteur Firebase actif (singleton), ou un recordset vide.

        Utilisé par les modèles synchronisés pour pousser une seule modification
        vers Firebase sans recharger toute la base.
        """
        return self.sudo().search([], limit=1)

    def _rtdb_reference(self, path):
        """Retourne une référence Realtime Database pour `path` (ex: 'SIK/vehicle/abc').

        Retourne None si le SDK n'est pas disponible ou la connexion impossible.
        """
        self.ensure_one()
        if not firebase_admin or not path:
            return None
        try:
            app = self._get_firebase_app()
            return db.reference(path, app=app)
        except Exception as e:  # pragma: no cover - dépend de l'environnement Firebase
            _logger.warning("Firebase: impossible d'obtenir la référence '%s': %s", path, e)
            return None

    def _rtdb_update(self, path, data):
        """Met à jour (merge) un noeud Realtime Database. Push incrémental, non bloquant."""
        ref = self._rtdb_reference(path)
        if ref is None:
            return False
        try:
            ref.update(data)
            return True
        except Exception as e:  # pragma: no cover
            _logger.warning("Firebase: échec update '%s': %s", path, e)
            return False

    def _rtdb_delete(self, path):
        """Supprime un noeud Realtime Database."""
        ref = self._rtdb_reference(path)
        if ref is None:
            return False
        try:
            ref.delete()
            return True
        except Exception as e:  # pragma: no cover
            _logger.warning("Firebase: échec suppression '%s': %s", path, e)
            return False

    @api.model
    def _notify_realtime(self, channel, payload):
        """Envoie une notification bus pour rafraîchir instantanément l'UI Odoo.

        `channel` : nom de canal écouté côté front (backend + portail).
        `payload` : dict sérialisable décrivant le changement.
        """
        try:
            self.env["bus.bus"]._sendone(channel, "inventory_fireman_sync", payload)
        except Exception as e:  # pragma: no cover
            _logger.debug("Firebase: notification bus impossible sur '%s': %s", channel, e)

    def action_test_connection(self):
        """Tester la connexion à Firebase"""
        self.ensure_one()

        try:
            app = self._get_firebase_app()
            # Tester l'accès à la base de données
            ref = db.reference("/", app=app)
            ref.get()

            self.write(
                {
                    "is_connected": True,
                    "last_connection_test": fields.Datetime.now(),
                    "connection_error": False,
                }
            )

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Connexion réussie",
                    "message": "La connexion à Firebase a été établie avec succès.",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            error_msg = str(e)
            _logger.error(f"Erreur de connexion Firebase: {error_msg}")
            self.write(
                {
                    "is_connected": False,
                    "last_connection_test": fields.Datetime.now(),
                    "connection_error": error_msg,
                }
            )

            raise UserError(f"Erreur de connexion à Firebase: {error_msg}")

    def _sync_uo_from_firebase(self, uo_data, firebase_uid):
        """Synchroniser une UO depuis Firebase"""
        Partner = self.env["res.partner"].sudo()

        # Rechercher si l'UO existe déjà
        existing_uo = Partner.search([("firebase_uid", "=", firebase_uid), ("is_firebase_uo", "=", True)], limit=1)

        # Préparer les données pour res.partner
        vals = {
            "firebase_uid": firebase_uid,
            "is_firebase_uo": True,
            "is_company": True,  # UO = entreprise
            "name": uo_data.get("name", uo_data.get("nom", "UO sans nom")),
            "uo_code": uo_data.get("code"),
            "firebase_created_at": convert_firebase_datetime_to_naive(uo_data.get("created_at")),
            "firebase_updated_at": convert_firebase_datetime_to_naive(uo_data.get("updated_at")),
            "firebase_data": json.dumps(uo_data),
            "last_sync_date": fields.Datetime.now(),
            "sync_status": "synced",
            "sync_error": False,
        }

        if existing_uo:
            existing_uo.write(vals)
            return existing_uo
        else:
            return Partner.create(vals)

    def _sync_pompier_from_firebase(self, pompier_data, firebase_uid):
        """Synchroniser un pompier depuis Firebase"""
        Partner = self.env["res.partner"].sudo()

        # Rechercher si le pompier existe déjà
        existing_pompier = Partner.search(
            [("firebase_uid", "=", firebase_uid), ("is_firebase_pompier", "=", True)], limit=1
        )

        # Récupérer ou créer l'UO principale
        uo_firebase_uid = pompier_data.get("uo_id", pompier_data.get("uo_uid"))
        uo_ids = []

        if uo_firebase_uid:
            uo = Partner.search([("firebase_uid", "=", uo_firebase_uid), ("is_firebase_uo", "=", True)], limit=1)
            if uo:
                uo_ids.append(uo.id)
            else:
                _logger.warning(f"UO {uo_firebase_uid} non trouvée pour le pompier {firebase_uid}")

        # Gérer les UO multiples
        other_uo_uids = pompier_data.get("other_uo_ids", pompier_data.get("uo_ids", []))
        if other_uo_uids:
            other_uos = Partner.search([("firebase_uid", "in", other_uo_uids), ("is_firebase_uo", "=", True)])
            uo_ids.extend(other_uos.ids)

        if not uo_ids:
            _logger.warning(f"Pompier {firebase_uid} sans UO associée")
            return None

        # Préparer les données
        firstname = pompier_data.get("firstname", pompier_data.get("prenom", ""))
        lastname = pompier_data.get("lastname", pompier_data.get("nom_famille", ""))

        vals = {
            "firebase_uid": firebase_uid,
            "is_firebase_pompier": True,
            "is_company": False,  # Pompier = personne
            "name": pompier_data.get("name", pompier_data.get("nom", f"{firstname} {lastname}".strip() or "Sans nom")),
            "email": pompier_data.get("email"),
            "phone": pompier_data.get("phone", pompier_data.get("telephone")),
            "mobile": pompier_data.get("mobile"),
            "uo_ids": [(6, 0, uo_ids)],  # Many2many: toutes les UO
            "matricule": pompier_data.get("matricule"),
            "grade": pompier_data.get("grade"),
            "fonction": pompier_data.get("fonction"),
            "date_entree": pompier_data.get("date_entree"),
            "date_sortie": pompier_data.get("date_sortie"),
            "pompier_status": pompier_data.get("status", "active"),
            "last_login": pompier_data.get("last_login"),
            "firebase_created_at": convert_firebase_datetime_to_naive(pompier_data.get("created_at")),
            "firebase_updated_at": convert_firebase_datetime_to_naive(pompier_data.get("updated_at")),
            "firebase_data": json.dumps(pompier_data),
            "last_sync_date": fields.Datetime.now(),
            "sync_status": "synced",
            "sync_error": False,
        }

        if existing_pompier:
            existing_pompier.write(vals)
            return existing_pompier
        else:
            return Partner.create(vals)

    def _sync_uo_from_user_data(self, uo_data, uo_name, realtime_uo_data=None):
        """Synchroniser une UO depuis les données utilisateur Firebase

        Args:
            uo_data: Données de l'UO depuis Firestore (minimal)
            uo_name: Nom de l'UO
            realtime_uo_data: Données enrichies depuis Realtime Database (optionnel)
        """
        Partner = self.env["res.partner"].sudo()

        # Rechercher si l'UO existe déjà par son nom
        existing_uo = Partner.search([("name", "=", uo_name), ("is_firebase_uo", "=", True)], limit=1)

        # Convertir les données Firebase en JSON sérialisable
        uo_data_json = convert_firestore_to_json(uo_data)

        # Préparer les données de base pour res.partner
        vals = {
            "is_firebase_uo": True,
            "is_company": True,
            "name": uo_name,
            "uo_code": uo_name[:10] if uo_name else "",  # Code basé sur le nom
            "last_sync_date": fields.Datetime.now(),
            "sync_status": "synced",
            "sync_error": False,
        }

        # Si on a des données enrichies depuis Realtime Database, les ajouter
        if realtime_uo_data and isinstance(realtime_uo_data, dict):
            vals.update(
                {
                    "firebase_created_at": convert_firebase_datetime_to_naive(realtime_uo_data.get("created_at")),
                    "firebase_updated_at": convert_firebase_datetime_to_naive(realtime_uo_data.get("updated_at")),
                }
            )
            # Fusionner les données JSON
            merged_data = {**uo_data_json, **realtime_uo_data}
            vals["firebase_data"] = json.dumps(convert_firestore_to_json(merged_data), default=str)
        else:
            vals["firebase_data"] = json.dumps(uo_data_json, default=str)

        if existing_uo:
            existing_uo.write(vals)
            return existing_uo
        else:
            return Partner.create(vals)

    def _sync_pompier_from_user_data(self, user_data, user_uid):
        """Synchroniser un pompier depuis les données utilisateur Firebase"""
        Partner = self.env["res.partner"].sudo()
        PompierUoRel = self.env["pompier.uo.rel"].sudo()

        # Rechercher si le pompier existe déjà par son UID Firebase
        existing_pompier = Partner.search(
            [("firebase_uid", "=", user_uid), ("is_firebase_pompier", "=", True)], limit=1
        )

        # Récupérer toutes les UO associées (peut être une liste)
        uo_data_list = user_data.get("uo", [])
        is_verified = False  # Au moins une UO vérifiée

        # Préparer les données du pompier
        email = user_data.get("email", "")

        # Extraire prénom et nom de l'email si disponible
        name_parts = email.split("@")[0].split(".") if email else []
        firstname = name_parts[0].capitalize() if len(name_parts) > 0 else ""
        lastname = name_parts[1].capitalize() if len(name_parts) > 1 else ""

        # Convertir les données Firebase en JSON sérialisable
        user_data_json = convert_firestore_to_json(user_data)

        vals = {
            "firebase_uid": user_uid,
            "is_firebase_pompier": True,
            "is_company": False,
            "name": f"{firstname} {lastname}".strip() or email or user_uid,
            "email": email,
            "admin": user_data.get("admin", False),
            "firebase_data": json.dumps(user_data_json, default=str),
            "last_sync_date": fields.Datetime.now(),
            "sync_status": "synced",
            "sync_error": False,
        }

        if existing_pompier:
            pompier = existing_pompier
            pompier.write(vals)
        else:
            pompier = Partner.create(vals)

        # Traiter les relations pompier-UO
        # Récupérer les relations existantes pour les comparer
        existing_relations = {rel.uo_id.id: rel for rel in pompier.pompier_rel_ids}
        processed_uo_ids = set()

        # Traiter chaque UO de Firebase
        if isinstance(uo_data_list, list):
            for uo_data in uo_data_list:
                if isinstance(uo_data, dict):
                    uo_name = uo_data.get("uo_name")
                    if uo_name:
                        # Rechercher ou créer l'UO
                        uo = Partner.search([("name", "=", uo_name), ("is_firebase_uo", "=", True)], limit=1)
                        if not uo:
                            uo = self._sync_uo_from_user_data(uo_data, uo_name)

                        if uo:
                            processed_uo_ids.add(uo.id)

                            # Extraire les données de la relation
                            uo_admin = uo_data.get("uo_admin", False)
                            uo_pharmacist = uo_data.get("uo_pharmacist", False)
                            verified = uo_data.get("verified", False)
                            last_connection_raw = uo_data.get("last_connection", False)

                            # Convertir la date Firebase (aware) en naive pour Odoo
                            last_connection_date = convert_firebase_datetime_to_naive(last_connection_raw)

                            if verified:
                                is_verified = True

                            # Mettre à jour ou créer la relation
                            if uo.id in existing_relations:
                                # Mettre à jour la relation existante
                                existing_relations[uo.id].write(
                                    {
                                        "uo_admin": uo_admin,
                                        "uo_pharmacist": uo_pharmacist,
                                        "verified": verified,
                                        "last_connection_date": last_connection_date,
                                    }
                                )
                            else:
                                # Créer une nouvelle relation
                                PompierUoRel.create(
                                    {
                                        "pompier_id": pompier.id,
                                        "uo_id": uo.id,
                                        "uo_admin": uo_admin,
                                        "uo_pharmacist": uo_pharmacist,
                                        "verified": verified,
                                        "last_connection_date": last_connection_date,
                                    }
                                )

        # Supprimer les relations qui n'existent plus dans Firebase
        relations_to_delete = pompier.pompier_rel_ids.filtered(lambda r: r.uo_id.id not in processed_uo_ids)
        if relations_to_delete:
            relations_to_delete.unlink()

        # Mettre à jour le statut du pompier en fonction de la vérification
        pompier.write(
            {
                "pompier_status": "active" if is_verified else "inactive",
            }
        )

        return pompier

    def _get_country_id(self, country_name):
        """Récupérer l'ID du pays depuis son nom"""
        if not country_name:
            return False
        Country = self.env["res.country"].sudo()
        country = Country.search(["|", ("name", "=ilike", country_name), ("code", "=ilike", country_name)], limit=1)
        return country.id if country else False

    def _sync_inventory_history(self, uo, inventory_data):
        """Synchroniser l'historique des inventaires pour une UO

        Args:
            uo: res.partner record (UO)
            inventory_data: dict avec les inventaires depuis Firebase
                Format attendu:
                {
                    "-O8u3ceHNGqoymgE63nL": {
                        "date": "11/10/2024 7:34",
                        "inventor_full": true,
                        "lack": "",
                        "moreDescription": "ras",
                        "name": "Vigilante",
                        "rank": "SGT",
                        "vehicle": "VSAV 1"
                    }
                }
        """
        if not inventory_data or not isinstance(inventory_data, dict):
            return

        InventoryHistory = self.env["firebase.inventory.history"].sudo()

        for inventory_uid, inventory_info in inventory_data.items():
            if not isinstance(inventory_info, dict):
                continue

            # Chercher si l'inventaire existe déjà (recherche uniquement par firebase_uid car c'est la clé unique)
            existing_inventory = InventoryHistory.search([("firebase_uid", "=", inventory_uid)], limit=1)

            vals = {
                "firebase_uid": inventory_uid,
                "uo_id": uo.id,
                "inventory_date_str": inventory_info.get("date", ""),  # Date au format texte "11/10/2024 7:34"
                "inventor_name": inventory_info.get("name", ""),  # Nom de l'inventeur
                "rank": inventory_info.get("rank", ""),
                "inventor_full": inventory_info.get("inventor_full", False),
                "vehicle": inventory_info.get("vehicle", ""),
                "lack": inventory_info.get("lack", ""),
                "more_description": inventory_info.get("moreDescription", ""),
                "firebase_data": json.dumps(convert_firestore_to_json(inventory_info), default=str),
                "last_sync_date": fields.Datetime.now(),
            }

            if existing_inventory:
                existing_inventory.write(vals)
                _logger.info(f"Inventaire {inventory_uid} mis à jour pour UO {uo.name}")
            else:
                InventoryHistory.create(vals)
                _logger.info(f"Inventaire {inventory_uid} créé pour UO {uo.name}")

    def _sync_vehicles(self, uo, vehicles_data):
        """Synchroniser les véhicules pour une UO

        Args:
            uo: res.partner record (UO)
            vehicles_data: dict avec les véhicules depuis Firebase
        """
        if not vehicles_data or not isinstance(vehicles_data, dict):
            return

        Vehicle = self.env["firebase.vehicle"].sudo()

        for vehicle_uid, vehicle_info in vehicles_data.items():
            if not isinstance(vehicle_info, dict):
                continue

            # Convertir les données Firestore en JSON standard
            vehicle_data = convert_firestore_to_json(vehicle_info)

            # Utiliser la nouvelle méthode de synchronisation complète
            Vehicle.sync_vehicle_from_firebase(uo.id, vehicle_data)

    def action_sync_all(self):
        """Synchroniser toutes les données depuis Firebase"""
        self.ensure_one()

        # Marquer toute cette transaction comme provenant de Firebase : les
        # enregistrements créés/modifiés ne seront donc PAS renvoyés vers Firebase
        # (anti-boucle), seules les notifications bus de rafraîchissement UI sont émises.
        self = self.with_context(from_firebase=True)

        try:
            app = self._get_firebase_app()

            uo_count = 0
            pompier_count = 0

            # Test : lire les UO depuis Realtime Database
            _logger.info("Lecture des UO depuis Realtime Database")
            root_ref = db.reference("/", app=app)
            root_data = root_ref.get()
            _logger.info(
                f"UO trouvées dans Realtime DB: {list(root_data.keys()) if isinstance(root_data, dict) else root_data}"
            )

            # Lire les users depuis Firestore
            _logger.info("Début de la synchronisation des users depuis Firestore")
            firestore_db = firestore.client(app=app)
            users_ref = firestore_db.collection("users")
            users_docs = users_ref.stream()

            users_data = {}
            for doc in users_docs:
                users_data[doc.id] = doc.to_dict()
                _logger.info(f"User trouvé: {doc.id} = {doc.to_dict()}")

            _logger.info(f"Nombre d'utilisateurs trouvés dans Firestore: {len(users_data)}")

            # Dictionnaire pour stocker les UO uniques
            uo_dict = {}

            # Premier passage : créer/mettre à jour les UO
            for user_uid, user_data in users_data.items():
                _logger.info(f"Traitement de l'utilisateur {user_uid}: {user_data}")
                if not isinstance(user_data, dict):
                    _logger.warning(f"user_data n'est pas un dict pour {user_uid}")
                    continue

                uo_data_list = user_data.get("uo", [])
                _logger.info(f"uo_data pour {user_uid}: {uo_data_list}")

                # Gérer le cas où uo est une liste
                if isinstance(uo_data_list, list):
                    for uo_data in uo_data_list:
                        if isinstance(uo_data, dict):
                            uo_name = uo_data.get("uo_name")
                            _logger.info(f"uo_name pour {user_uid}: {uo_name}")
                            if uo_name:
                                # Utiliser le nom de l'UO comme clé unique
                                if uo_name not in uo_dict:
                                    uo_dict[uo_name] = {
                                        "name": uo_name,
                                        "uo_admin": uo_data.get("uo_admin", False),
                                        "uo_pharmacist": uo_data.get("uo_pharmacist", False),
                                        "verified": uo_data.get("verified", False),
                                        "last_connection_date": uo_data.get("last_connection"),
                                    }

            # Créer/mettre à jour les UO de base (depuis Firestore users)
            for uo_name, uo_info in uo_dict.items():
                try:
                    with self.env.cr.savepoint():
                        self._sync_uo_from_user_data(uo_info, uo_name)
                        uo_count += 1
                except Exception as e:
                    _logger.error(f"Erreur lors de la sync de l'UO {uo_name}: {str(e)}")
                    import traceback

                    _logger.error(traceback.format_exc())

            # Enrichir les UO avec les données de Realtime Database
            _logger.info("Enrichissement des UO avec les données de Realtime Database")
            try:
                # Les UO sont à la racine de Realtime Database (SIB, KOE, ONE, etc.)
                # On réutilise root_data qui contient déjà toutes les UO
                realtime_uo_data = root_data

                if realtime_uo_data and isinstance(realtime_uo_data, dict):
                    _logger.info(f"UO trouvées dans Realtime DB: {list(realtime_uo_data.keys())}")
                    Partner = self.env["res.partner"].sudo()

                    for uo_key, uo_details in realtime_uo_data.items():
                        if not isinstance(uo_details, dict):
                            continue

                        try:
                            with self.env.cr.savepoint():
                                # Chercher l'UO par son nom
                                uo_name = uo_details.get("name", uo_details.get("nom", uo_key))
                                existing_uo = Partner.search(
                                    [("name", "=", uo_name), ("is_firebase_uo", "=", True)], limit=1
                                )

                                if existing_uo:
                                    # Mettre à jour avec les données enrichies
                                    _logger.info(f"Enrichissement de l'UO {uo_name} avec les données Realtime DB")

                                    # Fusionner toutes les données (verified, inventor_history, pharmacy_items, vehicle, etc.)
                                    merged_data = {}
                                    if existing_uo.firebase_data:
                                        try:
                                            existing_data = json.loads(existing_uo.firebase_data)
                                            merged_data = {**existing_data, **uo_details}
                                        except:
                                            merged_data = uo_details
                                    else:
                                        merged_data = uo_details

                                    vals = {
                                        "firebase_created_at": convert_firebase_datetime_to_naive(
                                            uo_details.get("created_at")
                                        ),
                                        "firebase_updated_at": convert_firebase_datetime_to_naive(
                                            uo_details.get("updated_at")
                                        ),
                                        "uo_verified": uo_details.get("verified", False),
                                        "uo_prefill_qty": uo_details.get("prefillTheQty", False),
                                        "max_users": uo_details.get("limit_user", 1),
                                        "max_vehicles": uo_details.get("limit_vehicle", 1),
                                        "uo_send_inventory_to_all": uo_details.get("sendInventoryToAll", False),
                                        "firebase_data": json.dumps(
                                            convert_firestore_to_json(merged_data), default=str
                                        ),
                                    }

                                    existing_uo.write(vals)

                                    self._sync_inventory_history(existing_uo, uo_details.get("inventor_history", {}))

                                    self._sync_vehicles(existing_uo, uo_details.get("vehicle", {}))
                                else:
                                    _logger.info(f"Création de l'UO {uo_name} depuis Realtime DB")
                                    self._sync_uo_from_firebase(uo_details, uo_key)
                                    uo_count += 1
                        except Exception as e:
                            _logger.error(f"Erreur lors de l'enrichissement de l'UO {uo_key}: {str(e)}")
                            import traceback

                            _logger.error(traceback.format_exc())
                else:
                    _logger.info(f"Pas de données UO dans {self.uo_path}")
            except Exception as e:
                _logger.warning(f"Erreur lors de l'enrichissement des UO depuis Realtime DB: {str(e)}")

            # Deuxième passage : créer/mettre à jour les pompiers
            for user_uid, user_data in users_data.items():
                if not isinstance(user_data, dict):
                    continue

                # Utiliser un savepoint pour éviter que l'échec d'un pompier annule toute la transaction
                try:
                    with self.env.cr.savepoint():
                        self._sync_pompier_from_user_data(user_data, user_uid)
                        pompier_count += 1
                except Exception as e:
                    _logger.error(f"Erreur lors de la sync du pompier {user_uid}: {str(e)}")
                    import traceback

                    _logger.error(traceback.format_exc())

            # Synchroniser les pompiers depuis /pompiers/ si ce chemin existe
            # (pour compatibilité avec l'ancienne structure)
            try:
                pompiers_ref = db.reference(self.pompiers_path, app=app)
                pompiers_data = pompiers_ref.get()

                if pompiers_data:
                    for firebase_uid, data in pompiers_data.items():
                        try:
                            self._sync_pompier_from_firebase(data, firebase_uid)
                            pompier_count += 1
                        except Exception as e:
                            _logger.error(f"Erreur lors de la sync du pompier {firebase_uid}: {str(e)}")
            except Exception as e:
                _logger.debug(f"Pas de données dans {self.pompiers_path}: {str(e)}")

            # Mettre à jour les statistiques
            self.write(
                {
                    "last_sync_date": fields.Datetime.now(),
                    "total_uo_synced": uo_count,
                    "total_pompiers_synced": pompier_count,
                }
            )

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Synchronisation terminée",
                    "message": f"{uo_count} UO et {pompier_count} pompiers synchronisés.",
                    "type": "success",
                    "sticky": False,
                },
            }

        except Exception as e:
            error_msg = str(e)
            _logger.error(f"Erreur lors de la synchronisation: {error_msg}")
            raise UserError(f"Erreur lors de la synchronisation: {error_msg}")

    def sync_single_uo(self, firebase_uid):
        """Synchroniser une seule UO"""
        self.ensure_one()
        app = self._get_firebase_app()
        ref = db.reference(f"{self.uo_path}/{firebase_uid}", app=app)
        data = ref.get()

        if data:
            return self._sync_uo_from_firebase(data, firebase_uid)
        return None

    def sync_single_pompier(self, firebase_uid):
        """Synchroniser un seul pompier"""
        self.ensure_one()
        app = self._get_firebase_app()
        ref = db.reference(f"{self.pompiers_path}/{firebase_uid}", app=app)
        data = ref.get()

        if data:
            return self._sync_pompier_from_firebase(data, firebase_uid)
        return None

    @api.model
    def cron_sync_firebase_data(self):
        """Cron job pour synchroniser automatiquement les données Firebase"""
        connectors = self.search([("auto_sync", "=", True)])

        for connector in connectors:
            try:
                connector.action_sync_all()
                _logger.info(f"Synchronisation automatique réussie pour {connector.name}")
            except Exception as e:
                _logger.error(f"Erreur lors de la synchronisation automatique de {connector.name}: {str(e)}")

        return True
