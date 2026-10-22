"""
Scoring de leads 0-100 = fit + intent + urgency. Pesos y bonos salen de campaigns/<x>.json -> scoring.

ADVERTENCIA HONESTA: la fórmula es la del script original (fit 40 / intent 35 / urgency 25), que su
propio docstring llamaba "default razonable" porque el skill lead-scoring.md estaba vacío. No está
calibrada con datos de conversión. Úsala para ORDENAR leads, no como verdad absoluta.

Entradas:
  tier      : 'Tier 1' | 'Tier 2' | 'Tier 3' (ajuste de la cuenta al ICP)
  sector    : opcional; suma bono si está en scoring.sector_fit_bonus
  intent    : {"signals": [str], "triggers": [str]}
              - signals : actividad/publicaciones del lead relevantes
              - triggers: eventos de negocio (expansión, cambio de líder, vacante de Nómina/RRHH detectada en 'Señales'...)

CLI:
    python -m core.scorer --campaign buk --tier "Tier 1" --signals 1 --triggers 2 [--sector banca]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import Campaign, CampaignError, add_campaign_args, common_args, finalize_args, load_campaign, log


def calculate_score(campaign: Campaign, tier: str, sector: str | None, intent: dict) -> dict:
    cfg = campaign.get("scoring")
    if not cfg:
        raise CampaignError(f"campaigns/{campaign.name}.json no define 'scoring'.")
    w = cfg["weights"]
    base = cfg["tier_fit"].get(tier, min(cfg["tier_fit"].values()))
    if sector and sector.lower() in {s.lower() for s in cfg.get("sector_fit_bonus", [])}:
        base = min(base + cfg.get("sector_bonus_value", 0.1), 1.0)
    fit = base * w["fit"]
    n_sig, n_trg = len(intent.get("signals", [])), len(intent.get("triggers", []))
    intent_pts = min(n_sig * cfg["signal_value"] + n_trg * cfg["trigger_value"], 1.0) * w["intent"]
    urgency = min(n_trg * cfg["urgency_per_trigger"], 1.0) * w["urgency"]
    total = max(0, min(round(fit + intent_pts + urgency), 100))
    threshold = cfg.get("handoff_min_score")
    result = {
        "score": total,
        "breakdown": {"fit": round(fit, 1), "intent": round(intent_pts, 1), "urgency": round(urgency, 1)},
        # None = el usuario aún no definió el umbral de handoff al AE (no se inventa uno)
        "handoff_to_ae": (total >= threshold) if isinstance(threshold, (int, float)) else None,
    }
    log("scorer", f"tier={tier} sector={sector} -> {result}", campaign)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scoring de leads")
    add_campaign_args(p)
    p.add_argument("--tier", required=True)
    p.add_argument("--sector")
    p.add_argument("--signals", type=int, default=0)
    p.add_argument("--triggers", type=int, default=0)
    args = p.parse_args(argv)
    finalize_args(p, args)
    try:
        intent = {"signals": ["s"] * args.signals, "triggers": ["t"] * args.triggers}
        print(json.dumps(calculate_score(load_campaign(args.campaign), args.tier, args.sector, intent), ensure_ascii=False))
        return 0
    except CampaignError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
