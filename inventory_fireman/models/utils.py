# -*- coding: utf-8 -*-
import logging
import uuid
from datetime import datetime, timezone

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class QuotaExceeded(UserError):
    """Limite de l'abonnement atteinte (véhicules ou membres)."""


def new_uid():
    return str(uuid.uuid4())


def to_naive_utc(value):
    """datetime Firebase (aware ou texte ISO) → datetime naïf UTC pour Odoo."""
    if not value:
        return False
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def parse_app_date(text):
    """Date texte de l'application ("11/10/2024 7:34") → datetime naïf, ou False."""
    if not text:
        return False
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except ValueError:
            continue
    return False


def as_list(raw):
    """Une liste Firebase peut revenir sous forme de liste (avec trous) ou de dict à clés numériques."""
    if isinstance(raw, dict):
        try:
            raw = [raw[k] for k in sorted(raw, key=lambda k: int(k))]
        except (TypeError, ValueError):
            raw = list(raw.values())
    return [x for x in (raw or []) if x]


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
