"""
Conectores de enriquecimiento: Snov.io (email), Apify (perfil de LinkedIn), Clay (señales).

Reglas:
  * NUNCA se inventa ni se "adivina" un email: si Snov.io no devuelve nada, el resultado es email=None.
  * El dominio de la empresa lo debe aportar el lead/usuario; no se deduce del nombre de la empresa.
  * Cada conector devuelve un dict {"ok": bool, ...}; un fallo NO lanza excepción hacia el flujo
    (se reintenta en la siguiente ejecución y queda en logs/agent_actions.log).
  * Credenciales solo desde variables de entorno (ver .env.example).

Estado real de cada conector (heredado de los scripts originales):
  * Snov.io : API v2 (OAuth2 client_credentials + start/result asíncrono). Era el que funcionaba.
  * Apify   : el actor dev_fusion~linkedin-profile-scraper devolvía HTTP 403 en los logs originales.
              Causa NO verificada. Si persiste, cambiar `enrichment.apify_actor` en la campaña.
  * Clay    : la API REST "/v1/enrich" del script original devolvía 404 (no existe como tal). Clay se integra
              enviando filas al WEBHOOK de una tabla; el resultado se lee después en Clay, no en esta llamada.
              Por eso aquí solo se EMPUJA el lead (push_to_clay) y no se esperan señales de vuelta.

CLI:
    python -m core.enrichment --campaign buk email --first Ana --last Ruiz --domain empresa.com
    python -m core.enrichment --campaign buk profile --url https://linkedin.com/in/slug
    python -m core.enrichment --campaign buk clay --company X --role Y --url https://linkedin.com/in/slug
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import Campaign, CampaignError, add_campaign_args, common_args, finalize_args, load_campaign, log, require_env

SNOV_TOKEN_URL = "https://api.snov.io/v1/oauth/access_token"
SNOV_FINDER_START = "https://api.snov.io/v2/emails-by-domain-by-name/start"
SNOV_FINDER_RESULT = "https://api.snov.io/v2/emails-by-domain-by-name/result"
SNOV_VERIFY_START = "https://api.snov.io/v2/email-verification/start"
SNOV_VERIFY_RESULT = "https://api.snov.io/v2/email-verification/result"
POLL_INTERVAL_S, POLL_TIMEOUT_S = 2, 60
APIFY_TIMEOUT_S = 30


def _requests():
    try:
        import requests
        return requests
    except ImportError as exc:
        raise RuntimeError("Falta 'requests' (pip install -r requirements.txt).") from exc


# --------------------------------------------------------------------------- #
# Snov.io
# --------------------------------------------------------------------------- #
class SnovClient:
    def __init__(self, campaign: Campaign):
        self.campaign = campaign
        self._token: str | None = None
        self._expires = 0.0

    def _headers(self) -> dict:
        if not self._token or time.time() >= self._expires:
            r = _requests().post(
                SNOV_TOKEN_URL,
                data={"grant_type": "client_credentials",
                      "client_id": require_env("SNOV_CLIENT_ID"),
                      "client_secret": require_env("SNOV_CLIENT_SECRET")},
                headers={"Accept": "application/json"}, timeout=30)
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + data.get("expires_in", 3600) - 60
        return {"Authorization": f"Bearer {self._token}"}

    def _poll(self, url: str, task_hash: str) -> dict:
        deadline = time.time() + POLL_TIMEOUT_S
        while time.time() < deadline:
            r = _requests().get(url, params={"task_hash": task_hash}, headers=self._headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "completed":
                return data
            time.sleep(POLL_INTERVAL_S)
        raise TimeoutError(f"timeout esperando task_hash={task_hash}")

    def verify_email(self, email: str) -> str:
        """'valid' | 'invalid' | 'unknown'"""
        try:
            s = _requests().post(SNOV_VERIFY_START, json={"emails": [email]}, headers=self._headers(), timeout=30)
            s.raise_for_status()
            data = self._poll(SNOV_VERIFY_RESULT, s.json()["data"]["task_hash"])
        except Exception as exc:
            log("enrichment", f"verify_email ERROR: {exc}", self.campaign)
            return "unknown"
        entries = data.get("data") or []
        status = entries[0].get("result", {}).get("smtp_status") if entries else None
        return {"valid": "valid", "not_valid": "invalid"}.get(status, "unknown")

    def find_email(self, first_name: str, last_name: str, domain: str) -> dict:
        if not (first_name and last_name and domain):
            return {"ok": False, "email": None, "verification": "unknown", "error": "faltan nombre, apellido o dominio"}
        try:
            payload = {"rows": [{"first_name": first_name, "last_name": last_name, "domain": domain}]}
            s = _requests().post(SNOV_FINDER_START, json=payload, headers=self._headers(), timeout=30)
            s.raise_for_status()
            data = self._poll(SNOV_FINDER_RESULT, s.json()["data"]["task_hash"])
        except Exception as exc:
            log("enrichment", f"find_email ERROR {first_name} {last_name}@{domain}: {exc}", self.campaign)
            return {"ok": False, "email": None, "verification": "unknown", "error": str(exc)}
        results = (data.get("data") or [{}])[0].get("result", [])
        if not results:
            log("enrichment", f"EMAIL_NOT_FOUND {first_name} {last_name}@{domain}", self.campaign)
            return {"ok": True, "email": None, "verification": "unknown", "error": None}
        email = results[0]["email"]
        return {"ok": True, "email": email, "verification": self.verify_email(email), "error": None}


# --------------------------------------------------------------------------- #
# Apify
# --------------------------------------------------------------------------- #
def scrape_linkedin_profile(campaign: Campaign, linkedin_url: str) -> dict:
    actor = str(campaign.get("enrichment.apify_actor", "dev_fusion~linkedin-profile-scraper"))
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    try:
        r = _requests().post(url, headers={"Authorization": f"Bearer {require_env('APIFY_TOKEN')}"},
                             json={"profileUrls": [linkedin_url]}, timeout=APIFY_TIMEOUT_S)
        r.raise_for_status()
        items = r.json()
    except Exception as exc:
        log("enrichment", f"apify ERROR {linkedin_url}: {exc}", campaign)
        return {"ok": False, "error": str(exc), "summary": None}
    if not items:
        return {"ok": True, "error": None, "summary": None}
    p = items[0]
    return {"ok": True, "error": None, "summary": {
        "experience": p.get("experiences", []), "education": p.get("educations", []),
        "recommendations": p.get("recommendations", []),
        "recent_activity": p.get("recentActivity", p.get("posts", []))}}


# --------------------------------------------------------------------------- #
# Clay (solo empuje al webhook de una tabla)
# --------------------------------------------------------------------------- #
def push_to_clay(campaign: Campaign, company: str, role: str, linkedin_url: str, email: str | None = None) -> dict:
    try:
        payload = {"company": company, "role": role, "linkedin_url": linkedin_url, "email": email,
                   "keywords": campaign.get("enrichment.clay_keywords", [])}
        headers = {"Content-Type": "application/json"}
        token = None
        try:
            token = require_env("CLAY_WEBHOOK_TOKEN")
        except CampaignError:
            pass  # el token es opcional según cómo se haya creado el webhook
        if token:
            headers["x-clay-webhook-auth"] = token
        r = _requests().post(require_env("CLAY_WEBHOOK_URL"), json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        return {"ok": True, "error": None, "note": "Enviado a Clay. Las señales se leen en la tabla de Clay, no en esta respuesta."}
    except Exception as exc:
        log("enrichment", f"clay push ERROR {company}: {exc}", campaign)
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
def enrich_lead(campaign: Campaign, lead: dict, dry_run: bool = True) -> dict:
    """Ejecuta los conectores activos en la campaña. En dry-run no llama a ninguna API."""
    plan = [n for n, flag in (("snov", "snov_enabled"), ("apify", "apify_enabled"), ("clay", "clay_enabled"))
            if campaign.get(f"enrichment.{flag}", False)]
    if dry_run:
        return {"dry_run": True, "would_run": plan}
    out: dict[str, Any] = {"dry_run": False}
    if "snov" in plan:
        first, _, last = str(lead.get("Nombre Completo", "")).partition(" ")
        out["snov"] = SnovClient(campaign).find_email(first, last.strip(), str(lead.get("_domain", "")))
    if "apify" in plan:
        out["apify"] = scrape_linkedin_profile(campaign, str(lead.get("Perfil LinkedIn", "")))
    if "clay" in plan:
        email = (out.get("snov") or {}).get("email")
        out["clay"] = push_to_clay(campaign, str(lead.get("Empresa", "")), str(lead.get("Cargo", "")),
                                   str(lead.get("Perfil LinkedIn", "")), email)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enriquecimiento de leads")
    add_campaign_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("email", parents=[common_args()])
    e.add_argument("--first", required=True); e.add_argument("--last", required=True); e.add_argument("--domain", required=True)
    pr = sub.add_parser("profile", parents=[common_args()]); pr.add_argument("--url", required=True)
    c = sub.add_parser("clay", parents=[common_args()])
    c.add_argument("--company", required=True); c.add_argument("--role", required=True); c.add_argument("--url", required=True)
    args = p.parse_args(argv)
    finalize_args(p, args)
    try:
        camp = load_campaign(args.campaign)
        if not args.real:
            print(json.dumps({"dry_run": True, "nota": "Sin --real no se llama a ninguna API externa (consume créditos)."}, ensure_ascii=False))
            return 0
        if args.cmd == "email":
            res = SnovClient(camp).find_email(args.first, args.last, args.domain)
        elif args.cmd == "profile":
            res = scrape_linkedin_profile(camp, args.url)
        else:
            res = push_to_clay(camp, args.company, args.role, args.url)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("ok") else 3
    except (CampaignError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
