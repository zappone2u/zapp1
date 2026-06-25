# Bank Sync Manager

## Description

Module Odoo 18.0 pour gérer la synchronisation bancaire personnalisée avec matching automatique des factures et gestion des abonnements.

## Fonctionnalités

### 1. Connexion Bancaire
- Configuration de connexions à votre banque
- Support API, PSD2, et import CSV
- Synchronisation automatique ou manuelle
- Gestion multi-banques

### 2. Transactions Bancaires
- Import automatique des transactions
- Identification automatique des clients
- Matching intelligent avec les factures
- Réconciliation comptable

### 3. Matching Automatique
- Score de confiance pour chaque match
- Matching par montant, date et client
- Gestion des paiements partiels
- Historique complet

### 4. Monitoring des Abonnements
- Surveillance automatique des paiements
- Alertes de retard de paiement
- Suspension automatique en cas de non-paiement
- Réactivation automatique après paiement

## Installation

1. Copiez le module dans votre dossier `addons`
2. Mettez à jour la liste des modules
3. Installez "Bank Sync Manager"

## Configuration

### 1. Configurer une connexion bancaire

Allez dans **Bank Sync > Configuration > Bank Connections**

- Créez une nouvelle connexion
- Renseignez les informations API de votre banque
- Testez la connexion
- Activez la synchronisation automatique

### 2. Configuration de l'API bancaire

Pour connecter votre banque, vous aurez besoin de :
- URL de l'API
- Clés d'authentification (API Key, Client ID, etc.)
- Numéro de compte / IBAN

**Note**: Le module supporte plusieurs types de connexions. Adaptez le code dans `models/bank_connection.py` méthode `_fetch_transactions()` selon votre API bancaire.

### 3. Synchronisation

La synchronisation peut être :
- **Automatique** : via les tâches planifiées (cron)
- **Manuelle** : bouton "Sync Now" sur chaque connexion

### 4. Matching des factures

Le matching peut être :
- **Automatique** : bouton "Auto Match" sur les transactions
- **Manuel** : création manuelle de matchings

## Utilisation

### Workflow typique

1. **Synchronisation**
   - Les transactions sont importées depuis la banque
   - Le client est identifié automatiquement si possible

2. **Matching**
   - Cliquez sur "Auto Match" pour matcher automatiquement
   - Le système cherche les factures correspondantes
   - Un score de confiance est calculé

3. **Validation**
   - Vérifiez les matchings proposés
   - Confirmez ou ajustez si nécessaire

4. **Réconciliation**
   - Cliquez sur "Reconcile" pour créer l'écriture comptable
   - La facture est marquée comme payée

5. **Monitoring abonnements**
   - Les monitors vérifient automatiquement les paiements
   - Alertes et actions automatiques selon la configuration

## Tâches planifiées (Crons)

- **Sync Bank Connections** : Toutes les heures
- **Check Subscription Payments** : Tous les jours
- **Create Subscription Monitors** : Tous les jours

## Personnalisation

### Adapter à votre API bancaire

Modifiez la méthode `_fetch_transactions()` dans `models/bank_connection.py` :

```python
def _fetch_transactions(self):
    # Votre logique d'API ici
    headers = {
        'Authorization': f'Bearer {self.api_key}',
    }
    response = requests.get(f'{self.api_url}/transactions', headers=headers)
    return response.json()
```

### Personnaliser les délais de suspension

Dans **Bank Sync > Subscriptions > Payment Monitors**, vous pouvez configurer pour chaque abonnement :
- Période de grâce (grace_period)
- Période critique (critical_period)
- Suspension automatique (auto_suspend)

## Support

Pour toute question ou problème, consultez les logs Odoo ou contactez votre administrateur système.

## Dépendances

- base
- account
- sale
- sale_subscription

## Licence

LGPL-3
