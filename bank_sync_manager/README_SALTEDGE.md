# Configuration Salt Edge pour Bank Sync Manager

## Qu'est-ce que Salt Edge ?

Salt Edge est un agrégateur bancaire qui permet de connecter plus de 5000 banques dans le monde via une seule API. C'est la solution idéale pour les particuliers et entreprises qui veulent automatiser la récupération de leurs transactions bancaires.

## Étapes de configuration

### 1. Créer un compte Salt Edge

1. Allez sur https://www.saltedge.com/
2. Créez un compte (gratuit pour développeurs/test)
3. Accédez au Dashboard : https://www.saltedge.com/dashboard

### 2. Obtenir les identifiants API

1. Dans le Dashboard, allez dans **Settings** > **Clients**
2. Créez un nouveau client ou utilisez celui par défaut
3. Notez votre **App ID** et **Secret**

### 3. Configurer dans Odoo

1. Allez dans **Bank Sync > Configuration > Bank Connections**
2. Créez une nouvelle connexion
3. Renseignez :
   - **Nom** : ex. "Ma Banque CIC"
   - **Type de connexion** : Salt Edge
   - **Salt Edge App ID** : collez votre App ID
   - **Salt Edge Secret** : collez votre Secret
   - **Journal** : sélectionnez le journal bancaire Odoo correspondant

### 4. Tester la connexion

1. Cliquez sur **Test Connection**
2. Un Customer ID sera créé automatiquement
3. Le statut passe à "Connected"

### 5. Connecter votre banque

1. Cliquez sur **Connect Bank Account**
2. Une fenêtre Salt Edge s'ouvre
3. Sélectionnez votre banque (CIC, CA, Revolut, UBS, BIL, Spuerkeess, etc.)
4. Authentifiez-vous avec vos identifiants bancaires
5. Autorisez Salt Edge à accéder à vos comptes
6. Vous serez redirigé vers Odoo

### 6. Synchroniser les transactions

1. Une fois la banque connectée, cliquez sur **Sync Now**
2. Les transactions sont importées automatiquement
3. Vous pouvez ensuite les matcher avec vos factures

## Banques supportées

Salt Edge supporte plus de 5000 banques, dont :

- **France** : CIC, Crédit Agricole, BNP Paribas, Société Générale, etc.
- **Luxembourg** : BIL, Spuerkeess, etc.
- **Suisse** : UBS, Credit Suisse, PostFinance, etc.
- **International** : Revolut, N26, Wise, PayPal, etc.

Liste complète : https://www.saltedge.com/products/account_information/providers

## Synchronisation automatique

Pour activer la synchronisation automatique :

1. Dans la connexion, cochez **Auto Synchronization**
2. Choisissez la fréquence (Hourly, Daily, Weekly)
3. Les transactions seront importées automatiquement selon la fréquence choisie

## Tarification Salt Edge

- **Gratuit** : 100 appels API par mois (suffisant pour tester)
- **Payant** : à partir de 49€/mois pour usage professionnel

Voir : https://www.saltedge.com/pricing

## Sécurité

- Les identifiants bancaires ne transitent jamais par Odoo
- Salt Edge est conforme PSD2 et certifié par les régulateurs européens
- Les données sont chiffrées en transit et au repos
- Vous pouvez révoquer l'accès à tout moment depuis votre espace bancaire

## Dépannage

### "Failed to create customer"
- Vérifiez que votre App ID et Secret sont corrects
- Vérifiez que votre compte Salt Edge est actif

### "No transactions imported"
- Vérifiez que vous avez bien connecté une banque
- Vérifiez que la période de synchronisation contient des transactions
- Consultez les logs Odoo pour plus de détails

### "Connection expired"
- Reconnectez votre banque via **Connect Bank Account**
- Certaines banques expirent le consentement après 90 jours

## Support

- Documentation Salt Edge : https://docs.saltedge.com/
- Support Odoo : Consultez les logs dans **Settings > Technical > Logging**
- Support Salt Edge : support@saltedge.com

## Alternative : Import CSV

Si vous ne souhaitez pas utiliser Salt Edge, vous pouvez :
1. Télécharger vos relevés au format CSV depuis votre banque
2. Les importer dans Odoo via **Bank Sync > Configuration > Import CSV**

Mais l'automatisation avec Salt Edge est beaucoup plus pratique !
