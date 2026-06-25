# -*- coding: utf-8 -*-
"""
EXEMPLE D'ADAPTATION POUR VOTRE API BANCAIRE

Ce fichier montre comment adapter le module à différentes APIs bancaires.
Copiez et modifiez les méthodes selon votre besoin dans bank_connection.py
"""

# ========================================
# EXEMPLE 1: API REST Standard
# ========================================


def _fetch_transactions_rest_api(self):
    """Exemple pour une API REST standard"""
    import requests
    from datetime import datetime, timedelta

    # Calculer la période
    start_date = self.last_sync_date or (datetime.now() - timedelta(days=30))
    end_date = datetime.now()

    # Préparer les headers
    headers = {
        "Authorization": f"Bearer {self.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Paramètres de requête
    params = {
        "account_id": self.account_number,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "limit": 100,
    }

    # Appel API
    response = requests.get(
        f"{self.api_url}/accounts/{self.account_number}/transactions", headers=headers, params=params
    )

    if response.status_code != 200:
        raise Exception(f"API Error: {response.status_code} - {response.text}")

    data = response.json()

    # Transformer les données au format Odoo
    transactions = []
    for trans in data.get("transactions", []):
        transactions.append(
            {
                "reference": trans["id"],
                "date": trans["booking_date"],
                "amount": float(trans["amount"]),
                "description": trans.get("remittance_information", ""),
                "partner_name": trans.get("creditor_name") or trans.get("debtor_name"),
                "partner_account": trans.get("creditor_account") or trans.get("debtor_account"),
            }
        )

    return transactions


# ========================================
# EXEMPLE 2: PSD2 (Open Banking)
# ========================================


def _fetch_transactions_psd2(self):
    """Exemple pour API PSD2 (Open Banking)"""
    import requests
    from datetime import datetime, timedelta

    # 1. Obtenir un token d'accès
    token = self._get_psd2_access_token()

    # 2. Préparer la requête
    start_date = self.last_sync_date or (datetime.now() - timedelta(days=30))

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "Consent-ID": self.api_secret,  # Consent ID obtenu lors de l'enregistrement
    }

    # 3. Récupérer les transactions
    response = requests.get(
        f"{self.api_url}/v1/accounts/{self.account_number}/transactions",
        headers=headers,
        params={"dateFrom": start_date.strftime("%Y-%m-%d"), "bookingStatus": "booked"},
    )

    if response.status_code != 200:
        raise Exception(f"PSD2 Error: {response.text}")

    data = response.json()

    # 4. Parser les transactions
    transactions = []
    for trans in data["transactions"]["booked"]:
        transactions.append(
            {
                "reference": trans["transactionId"],
                "date": trans["bookingDate"],
                "amount": float(trans["transactionAmount"]["amount"]),
                "description": trans.get("remittanceInformationUnstructured", ""),
                "partner_name": trans.get("creditorName") or trans.get("debtorName"),
                "partner_account": trans.get("creditorAccount", {}).get("iban"),
            }
        )

    return transactions


def _get_psd2_access_token(self):
    """Obtenir un token OAuth2 pour PSD2"""
    import requests

    response = requests.post(
        f"{self.api_url}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        },
    )

    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        raise Exception("Failed to get access token")


# ========================================
# EXEMPLE 3: SFTP / Fichiers CSV
# ========================================


def _fetch_transactions_sftp(self):
    """Exemple pour récupération via SFTP"""
    import paramiko
    import csv
    from io import StringIO
    from datetime import datetime

    # 1. Connexion SFTP
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=self.api_url,  # Utiliser api_url pour l'adresse SFTP
        username=self.client_id,
        password=self.client_secret,
        port=22,
    )

    sftp = ssh.open_sftp()

    # 2. Lister les fichiers dans le répertoire
    files = sftp.listdir("/transactions")

    # 3. Récupérer le dernier fichier
    latest_file = sorted(files)[-1]

    # 4. Télécharger et lire le fichier
    with sftp.open(f"/transactions/{latest_file}", "r") as f:
        content = f.read().decode("utf-8")

    sftp.close()
    ssh.close()

    # 5. Parser le CSV
    transactions = []
    reader = csv.DictReader(StringIO(content), delimiter=";")

    for row in reader:
        transactions.append(
            {
                "reference": row["Transaction_ID"],
                "date": datetime.strptime(row["Date"], "%d/%m/%Y").date(),
                "amount": float(row["Amount"].replace(",", ".")),
                "description": row["Description"],
                "partner_name": row["Beneficiary"],
                "partner_account": row["Account_Number"],
            }
        )

    return transactions


# ========================================
# EXEMPLE 4: Banque Française (exemple Crédit Agricole)
# ========================================


def _fetch_transactions_ca(self):
    """Exemple pour API Crédit Agricole"""
    import requests
    from datetime import datetime, timedelta

    # Authentification spécifique CA
    session = requests.Session()

    # 1. Login
    login_response = session.post(
        f"{self.api_url}/authenticate",
        json={
            "username": self.client_id,
            "password": self.client_secret,
        },
    )

    if login_response.status_code != 200:
        raise Exception("Authentication failed")

    # 2. Récupérer les comptes
    accounts_response = session.get(f"{self.api_url}/accounts")
    accounts = accounts_response.json()

    # Trouver le compte correspondant
    account = None
    for acc in accounts:
        if acc["accountNumber"] == self.account_number:
            account = acc
            break

    if not account:
        raise Exception("Account not found")

    # 3. Récupérer les opérations
    start_date = self.last_sync_date or (datetime.now() - timedelta(days=30))

    operations_response = session.get(
        f"{self.api_url}/accounts/{account['id']}/operations",
        params={
            "fromDate": start_date.strftime("%Y-%m-%d"),
            "toDate": datetime.now().strftime("%Y-%m-%d"),
        },
    )

    operations = operations_response.json()

    # 4. Transformer en format Odoo
    transactions = []
    for op in operations:
        transactions.append(
            {
                "reference": op["operationId"],
                "date": datetime.strptime(op["dateOperation"], "%Y-%m-%d").date(),
                "amount": float(op["montant"]),
                "description": op["libelle"],
                "partner_name": op.get("tiers", ""),
                "partner_account": "",
            }
        )

    return transactions


# ========================================
# EXEMPLE 5: Webhook (réception push)
# ========================================

"""
Pour un système de webhook, vous devrez créer un controller:

from odoo import http
from odoo.http import request
import json

class BankWebhook(http.Controller):
    
    @http.route('/bank_sync/webhook/<int:connection_id>', type='json', auth='none', csrf=False)
    def receive_transaction(self, connection_id, **kwargs):
        '''Endpoint pour recevoir les transactions en push'''
        
        # Vérifier la signature
        signature = request.httprequest.headers.get('X-Signature')
        if not self._verify_signature(signature, request.jsonrequest):
            return {'error': 'Invalid signature'}
        
        # Récupérer la connexion
        connection = request.env['bank.connection'].sudo().browse(connection_id)
        if not connection.exists():
            return {'error': 'Connection not found'}
        
        # Parser la transaction
        data = request.jsonrequest
        transaction_vals = {
            'connection_id': connection_id,
            'reference': data['transaction_id'],
            'date': data['date'],
            'amount': float(data['amount']),
            'description': data.get('description', ''),
            'partner_name': data.get('counterparty_name'),
            'partner_account': data.get('counterparty_account'),
        }
        
        # Créer la transaction
        request.env['bank.transaction'].sudo().create(transaction_vals)
        
        return {'status': 'success'}
"""


# ========================================
# CONSEILS D'IMPLÉMENTATION
# ========================================

"""
1. SÉCURITÉ
   - Toujours utiliser HTTPS
   - Stocker les credentials de manière sécurisée
   - Valider les données reçues
   - Gérer les erreurs proprement

2. PERFORMANCE
   - Limiter le nombre de transactions par appel
   - Utiliser la pagination si disponible
   - Mettre en cache les tokens d'accès
   - Éviter les duplicatas avec la référence unique

3. GESTION D'ERREURS
   - Logger toutes les erreurs
   - Prévoir des retry en cas d'échec temporaire
   - Notifier l'administrateur en cas de problème

4. TESTS
   - Tester avec des données de test d'abord
   - Vérifier les formats de date
   - Valider les montants (positif/négatif)
   - Tester les cas limites

5. MONITORING
   - Suivre le nombre de transactions importées
   - Vérifier les échecs de synchronisation
   - Surveiller les temps de réponse API
"""
