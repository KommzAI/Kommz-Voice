#!/usr/bin/env python3
"""
Remise a niveau des licences Gamer / Voice a partir d'un export Stripe des abonnements.

Pour chaque abonnement actif, appelle POST /internal/licenses/renew : les cles activees par
l'e-mail de l'abonne sont enregistrees en base, reliees a l'abonnement, et leur fin devient
celle de la periode payee. Les abonnes bloques par une cle expiree retrouvent l'acces sans
changer de cle. Les abonnements sans cle activee sont listes : une cle neuve est a leur emettre.

Par defaut, rien n'est envoye (simulation). Avec --apply, les appels sont faits.

Variables d'environnement (jamais affichees) :
  KOMMZ_LICENSE_WEBHOOK_SECRET   secret des routes /internal/licenses/*
  KOMMZ_LICENSE_API_URL          facultatif, https://kommzvoice.onrender.com par defaut

Usage (Windows cmd) :
  set KOMMZ_LICENSE_WEBHOOK_SECRET=<votre secret>
  python scripts\\license_resync.py abonnements.csv
  python scripts\\license_resync.py abonnements.csv --apply
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import requests


def plan_to_products(plan: str) -> list:
    """Meme logique que les filtres Make : le texte du produit decide."""
    text = (plan or "").lower()
    if "ultimate" in text or "voice" in text:
        return ["gamer", "voice"]
    if "gamer" in text:
        return ["gamer"]
    return []  # Access seul : gere par le serveur Access


def parse_period_end(raw: str) -> int:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError("date de fin illisible")


def mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return (local[:2] + "***@" + domain) if domain else "***"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_file")
    ap.add_argument("--apply", action="store_true", help="envoie vraiment les appels")
    ap.add_argument("--col-id", default="id")
    ap.add_argument("--col-email", default="Customer Email")
    ap.add_argument("--col-end", default="Current Period End (UTC)")
    ap.add_argument("--col-plan", default="Plan")
    ap.add_argument("--col-status", default="Status")
    args = ap.parse_args()

    api = os.environ.get("KOMMZ_LICENSE_API_URL", "https://kommzvoice.onrender.com").rstrip("/")
    secret = os.environ.get("KOMMZ_LICENSE_WEBHOOK_SECRET", "").strip()
    if args.apply and len(secret) < 24:
        print("KOMMZ_LICENSE_WEBHOOK_SECRET absent ou trop court : rien n'est envoye.")
        return 2

    counts = {"ok": 0, "sans_cle": 0, "erreur": 0, "ignore": 0}
    with open(args.csv_file, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in (args.col_id, args.col_email, args.col_end, args.col_plan) if c not in (reader.fieldnames or [])]
        if missing:
            print("Colonnes introuvables :", ", ".join(missing))
            print("Colonnes du fichier   :", ", ".join(reader.fieldnames or []))
            print("Utilisez --col-id, --col-email, --col-end, --col-plan.")
            return 2
        for row in reader:
            status = (row.get(args.col_status) or "active").strip().lower()
            products = plan_to_products(row.get(args.col_plan, ""))
            if status not in {"active", "trialing"} or not products:
                counts["ignore"] += 1
                continue
            try:
                body = {
                    "subscription_id": (row[args.col_id] or "").strip(),
                    "customer_email": (row[args.col_email] or "").strip().lower(),
                    "period_end": parse_period_end(row[args.col_end]),
                    "products": products,
                }
            except (ValueError, KeyError) as exc:
                counts["erreur"] += 1
                print(f"ERREUR  {mask(row.get(args.col_email, ''))}: {exc}")
                continue
            label = f"{mask(body['customer_email'])} {'+'.join(products)}"
            if not args.apply:
                counts["ok"] += 1
                print(f"SIMUL   {label}")
                continue
            try:
                resp = requests.post(api + "/internal/licenses/renew", json=body,
                                     headers={"X-Kommz-Webhook-Secret": secret}, timeout=30)
            except requests.RequestException as exc:
                counts["erreur"] += 1
                print(f"ERREUR  {label}: {type(exc).__name__}")
                continue
            if resp.status_code == 200:
                counts["ok"] += 1
                print(f"OK      {label}")
            elif resp.status_code == 404:
                counts["sans_cle"] += 1
                print(f"SANS CLE ACTIVEE  {label}  -> emettre une cle neuve")
            else:
                counts["erreur"] += 1
                print(f"ERREUR  {label}: HTTP {resp.status_code}")
    print("\nResume :", ", ".join(f"{k}={v}" for k, v in counts.items()))
    if not args.apply:
        print("Simulation : relancez avec --apply pour envoyer.")
    return 0 if counts["erreur"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
