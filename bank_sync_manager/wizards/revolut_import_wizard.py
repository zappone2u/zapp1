# -*- coding: utf-8 -*-
"""
Assistant d'import de relevé Revolut (XLSX / CSV)
=================================================
Permet d'importer manuellement un relevé de compte Revolut, de créer les
transactions bancaires correspondantes, puis de les rapprocher automatiquement
des factures d'abonnement (sale.order) par montant, date et nom du payeur.

Colonnes Revolut attendues (dans l'ordre de l'export) :
    0 Type | 1 Produit | 2 Date de début | 3 Date de fin | 4 Description
    5 Montant | 6 Frais | 7 Devise | 8 État | 9 Solde
"""

import base64
import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta

from defusedxml.ElementTree import fromstring as xml_fromstring

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Base des dates Excel (numéro de série → date)
_EXCEL_EPOCH = datetime(1899, 12, 30)
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class RevolutImportWizard(models.TransientModel):
    _name = "revolut.import.wizard"
    _description = "Import relevé Revolut"

    connection_id = fields.Many2one(
        "bank.connection",
        string="Connexion bancaire",
        required=True,
        default=lambda self: self._default_connection(),
        help="Connexion de type « CSV Import » utilisée pour rattacher les transactions et le journal.",
    )
    data_file = fields.Binary(string="Fichier Revolut", required=True)
    filename = fields.Char(string="Nom du fichier")

    only_incoming = fields.Boolean(
        string="Paiements reçus uniquement",
        default=True,
        help="N'importer que les montants positifs (paiements d'abonnement reçus des clients).",
    )
    auto_match = fields.Boolean(
        string="Rapprochement automatique",
        default=True,
        help="Tenter de rapprocher chaque paiement reçu d'une facture d'abonnement ouverte.",
    )
    date_tolerance = fields.Integer(
        string="Tolérance de date (jours)",
        default=7,
        help="Écart maximal autorisé entre la date du paiement et la date de la facture.",
    )
    amount_tolerance = fields.Float(
        string="Tolérance de montant",
        default=0.01,
        help="Écart maximal autorisé entre le montant reçu et le montant restant dû de la facture.",
    )

    # ──────────────────────────────────────────────────────────────────────
    #  Valeurs par défaut
    # ──────────────────────────────────────────────────────────────────────

    @api.model
    def _default_connection(self):
        return self.env["bank.connection"].search([("connection_type", "=", "csv")], limit=1)

    # ──────────────────────────────────────────────────────────────────────
    #  Lecture du fichier
    # ──────────────────────────────────────────────────────────────────────

    def _read_rows(self):
        """Retourne la liste des lignes (listes de valeurs) du fichier importé."""
        self.ensure_one()
        content = base64.b64decode(self.data_file)
        name = (self.filename or "").lower()

        if name.endswith(".csv"):
            return self._read_csv(content)
        if name.endswith(".xlsx") or content[:2] == b"PK":
            return self._read_xlsx(content)
        # Par défaut, tenter le CSV
        return self._read_csv(content)

    def _read_csv(self, content):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        # Revolut CSV utilise la virgule ; on détecte le séparateur le plus probable
        sample = text[:2048]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        return [list(row) for row in reader]

    def _read_xlsx(self, content):
        """Parse un XLSX sans dépendance externe (lecture directe du ZIP/XML)."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            raise UserError(_("Le fichier fourni n'est pas un fichier XLSX valide."))

        # Chaînes partagées
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = xml_fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.iter(_XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))

        # Première feuille
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in zf.namelist():
            sheets = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
            if not sheets:
                raise UserError(_("Aucune feuille de calcul trouvée dans le fichier XLSX."))
            sheet_name = sorted(sheets)[0]

        sheet = xml_fromstring(zf.read(sheet_name))
        rows = []
        for row in sheet.iter(_XLSX_NS + "row"):
            values = []
            for cell in row.iter(_XLSX_NS + "c"):
                cell_type = cell.get("t")
                v = cell.find(_XLSX_NS + "v")
                text = v.text if v is not None else ""
                if cell_type == "s" and text not in (None, ""):
                    text = shared[int(text)]
                elif cell_type == "inlineStr":
                    is_node = cell.find(_XLSX_NS + "is")
                    text = "".join(t.text or "" for t in is_node.iter(_XLSX_NS + "t")) if is_node is not None else ""
                values.append(text)
            rows.append(values)
        return rows

    # ──────────────────────────────────────────────────────────────────────
    #  Helpers de conversion
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_amount(value):
        if value in (None, ""):
            return 0.0
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _parse_date(value):
        """Convertit une date Revolut (numéro de série Excel ou texte) en date Odoo."""
        if value in (None, ""):
            return False
        # Numéro de série Excel
        try:
            serial = float(value)
            return (_EXCEL_EPOCH + timedelta(days=serial)).date()
        except (ValueError, TypeError):
            pass
        # Formats texte courants
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
        return False

    # ──────────────────────────────────────────────────────────────────────
    #  Action principale
    # ──────────────────────────────────────────────────────────────────────

    def action_import(self):
        self.ensure_one()

        if not self.connection_id:
            raise UserError(
                _(
                    "Aucune connexion bancaire sélectionnée. Créez d'abord une connexion de type "
                    "« CSV Import » (avec un journal de banque) dans Bank Sync > Configuration."
                )
            )
        if not self.connection_id.journal_id:
            raise UserError(
                _("La connexion « %s » doit avoir un journal de banque configuré.") % self.connection_id.name
            )

        rows = self._read_rows()
        if not rows:
            raise UserError(_("Le fichier est vide ou illisible."))

        BankTransaction = self.env["bank.transaction"]
        company_currency = self.env.company.currency_id

        created = self.env["bank.transaction"]
        matched_count = 0
        skipped = 0
        imported = 0

        for index, row in enumerate(rows):
            # Ignorer la ligne d'en-tête (première ligne contenant « Type » / « Montant »)
            if index == 0 and row and str(row[0]).strip().lower() in ("type", "type de transaction"):
                continue
            if len(row) < 6:
                continue

            description = (row[4] or "").strip() if len(row) > 4 else ""
            amount = self._parse_amount(row[5] if len(row) > 5 else 0)
            txn_date = self._parse_date(row[2] if len(row) > 2 else "")
            currency_name = (row[7] or "").strip() if len(row) > 7 else ""

            if not amount or not txn_date:
                continue
            if self.only_incoming and amount <= 0:
                continue

            # Référence stable pour éviter les doublons à la ré-importation
            reference = "REV-%s-%s-%s" % (
                txn_date.isoformat(),
                ("%.2f" % amount).replace("-", "n"),
                (description[:20] or "tx").replace(" ", "_"),
            )
            existing = BankTransaction.search(
                [("connection_id", "=", self.connection_id.id), ("reference", "=", reference)], limit=1
            )
            if existing:
                skipped += 1
                continue

            currency = company_currency
            if currency_name:
                found = (
                    self.env["res.currency"]
                    .with_context(active_test=False)
                    .search([("name", "=", currency_name)], limit=1)
                )
                if found:
                    currency = found

            partner = self._find_partner(description)

            txn = BankTransaction.create(
                {
                    "connection_id": self.connection_id.id,
                    "reference": reference,
                    "date": txn_date,
                    "amount": amount,
                    "currency_id": currency.id,
                    "partner_name": description,
                    "partner_id": partner.id if partner else False,
                    "description": "%s — %s" % (row[0] if row else "", description),
                    "state": "unmatched",
                }
            )
            created |= txn
            imported += 1

            if self.auto_match and amount > 0:
                if self._match_to_subscription(txn):
                    matched_count += 1

        message = _(
            "%(imported)s transaction(s) importée(s), %(matched)s rapprochée(s) d'un abonnement, "
            "%(skipped)s déjà existante(s)."
        ) % {"imported": imported, "matched": matched_count, "skipped": skipped}

        if not created:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": _("Import Revolut"), "message": message, "type": "warning", "sticky": False},
            }

        return {
            "name": _("Transactions importées"),
            "type": "ir.actions.act_window",
            "res_model": "bank.transaction",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
            "context": {"search_default_group_state": 1},
        }

    # ──────────────────────────────────────────────────────────────────────
    #  Recherche de partenaire et rapprochement abonnement
    # ──────────────────────────────────────────────────────────────────────

    def _find_partner(self, description):
        """Tente de retrouver le client payeur à partir du libellé Revolut."""
        if not description:
            return self.env["res.partner"]
        return self.env["res.partner"].search(
            ["|", ("name", "=ilike", description), ("name", "ilike", description)], limit=1
        )

    @staticmethod
    def _normalize_ref(value):
        """Réduit une référence à ses caractères alphanumériques en majuscules.

        Permet de comparer « INV/2026/00042 » avec « INV 2026 00042 » ou
        « inv2026-00042 » tels qu'ils peuvent apparaître dans un libellé bancaire.
        """
        if not value:
            return ""
        return "".join(ch for ch in str(value) if ch.isalnum()).upper()

    def _match_by_reference(self, invoices, description):
        """Rapproche par numéro de facture trouvé dans le libellé.

        Le client et la date peuvent différer : si le numéro de facture (ou sa
        communication structurée) apparaît dans la description, c'est une
        correspondance fiable qui prime sur les autres critères.
        """
        normalized_desc = self._normalize_ref(description)
        if not normalized_desc:
            return self.env["account.move"]

        for invoice in invoices:
            for ref in (invoice.name, invoice.payment_reference, invoice.ref):
                normalized_ref = self._normalize_ref(ref)
                # Référence trop courte = risque de faux positif
                if len(normalized_ref) >= 4 and normalized_ref in normalized_desc:
                    return invoice
        return self.env["account.move"]

    def _match_to_subscription(self, txn):
        """Rapproche une transaction reçue d'une facture d'abonnement ouverte."""
        amount = abs(txn.amount)
        invoices = self.env["account.move"].search(
            [
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("payment_state", "in", ["not_paid", "partial"]),
                ("subscription_id", "!=", False),
            ]
        )
        if not invoices:
            return False

        # 0) PRIORITÉ : rapprochement par numéro de facture dans le libellé.
        #    Le client et la date peuvent ne pas correspondre, donc on ne filtre
        #    ni par partenaire ni par date dans ce cas.
        invoice = self._match_by_reference(invoices, txn.partner_name)

        if not invoice:
            # 1) Filtrer par montant restant dû (à la tolérance près)
            candidates = invoices.filtered(lambda inv: abs(inv.amount_residual - amount) <= self.amount_tolerance)
            if not candidates:
                return False

            # 2) Restreindre au même client si connu
            if txn.partner_id:
                same_partner = candidates.filtered(
                    lambda inv: inv.commercial_partner_id == txn.partner_id.commercial_partner_id
                )
                if same_partner:
                    candidates = same_partner

            # 3) Restreindre à la fenêtre de date si possible
            dated = candidates.filtered(
                lambda inv: inv.invoice_date and abs((inv.invoice_date - txn.date).days) <= self.date_tolerance
            )
            if dated:
                candidates = dated

            invoice = candidates[0]

        if not txn.partner_id:
            txn.partner_id = invoice.partner_id.id

        self.env["invoice.matching"].create(
            {
                "transaction_id": txn.id,
                "invoice_id": invoice.id,
                "matched_amount": min(amount, invoice.amount_residual),
                "state": "auto",
            }
        )
        txn.state = "matched" if txn.remaining_amount <= self.amount_tolerance else "partial"
        return True
