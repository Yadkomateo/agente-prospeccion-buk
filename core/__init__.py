"""
Motor agnóstico de prospección. Nada aquí conoce a un cliente concreto:
todo lo específico (spreadsheet, límites, ICP, tabs, prompts) vive en
campaigns/<nombre>.json y se elige con --campaign <nombre>.

Convenciones:
  * Sin --real, TODO es simulación (dry-run): no se escribe en Sheets, no se
    consume cuota de LinkedIn. Es una regla dura (.agents/rules/linkedin-safety.md).
  * Los secretos viven en variables de entorno (.env), nunca en campaigns/*.json.
  * Rutas relativas a la raíz del workspace (ROOT), no al directorio actual.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CAMPAIGNS_DIR = ROOT / "campaigns"
LOG_PATH = ROOT / "logs" / "agent_actions.log"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"^\s*(REEMPLAZAR|TU_|<)", re.IGNORECASE)


class CampaignError(RuntimeError):
    """Campaña inexistente, mal formada o con valores pendientes de completar."""


class PromptError(RuntimeError):
    """Plantilla inexistente o con variables sin valor (nunca se envía un {placeholder} crudo)."""


# --------------------------------------------------------------------------- #
# Entorno
# --------------------------------------------------------------------------- #
def load_env(path: Path | None = None) -> None:
    """Carga .env (KEY=VALUE) sin pisar variables ya definidas. Sin dependencias externas."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    load_env()
    value = os.environ.get(name, "").strip()
    if not value or _PLACEHOLDER_RE.match(value):
        raise CampaignError(f"Falta la variable de entorno {name} (ver .env.example).")
    return value


# --------------------------------------------------------------------------- #
# Campañas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Campaign:
    name: str
    data: dict

    def get(self, dotted: str, default: Any = None) -> Any:
        """get('limits.daily_connections') -> 40"""
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None or (isinstance(value, str) and _PLACEHOLDER_RE.match(value)):
            raise CampaignError(
                f"campaigns/{self.name}.json: '{dotted}' está vacío o pendiente de completar."
            )
        return value

    @property
    def campaign_id(self) -> str:
        return str(self.get("campaign_id", self.name))

    @property
    def prompts_dir(self) -> Path:
        return ROOT / str(self.get("prompts_dir", "prompts/"))

    @property
    def limits(self) -> dict:
        return dict(self.get("limits", {}))


def load_campaign(name: str) -> Campaign:
    if not _NAME_RE.match(name or ""):
        raise CampaignError(f"Nombre de campaña inválido: {name!r} (solo a-z, 0-9, _ y -).")
    path = CAMPAIGNS_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in CAMPAIGNS_DIR.glob("*.json"))
        raise CampaignError(f"No existe campaigns/{name}.json. Disponibles: {available}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CampaignError(f"campaigns/{name}.json no es JSON válido: {exc}") from exc
    return Campaign(name=name, data=data)


def add_campaign_args(parser: argparse.ArgumentParser) -> None:
    """Argumentos del parser principal: --campaign (obligatorio, validado en finalize_args) y --real."""
    parser.add_argument("--campaign", default=None, help="nombre de campaigns/<nombre>.json (ej. buk)")
    parser.add_argument(
        "--real",
        action="store_true",
        help="ejecuta de verdad. Sin este flag todo es simulación (dry-run).",
    )


def common_args() -> argparse.ArgumentParser:
    """Para `sub.add_parser(..., parents=[common_args()])`: permite --campaign/--real DESPUÉS del subcomando."""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--campaign", default=argparse.SUPPRESS)
    c.add_argument("--real", action="store_true", default=argparse.SUPPRESS)
    return c


def finalize_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not getattr(args, "campaign", None):
        parser.error("--campaign es obligatorio (ej. --campaign buk)")


# --------------------------------------------------------------------------- #
# Prompts parametrizados
# --------------------------------------------------------------------------- #
def render_prompt(campaign: Campaign, template: str, **variables: Any) -> str:
    """
    Renderiza prompts/<template>.txt (ej. 'greeting', 'follow_ups/breakup').
    Falla si falta una variable o viene vacía: preferible un error a enviar
    por LinkedIn un mensaje con '{trigger}' literal.
    """
    base = campaign.prompts_dir.resolve()
    path = (base / f"{template}.txt").resolve()
    if base not in path.parents:
        raise PromptError(f"Plantilla fuera de prompts_dir: {template!r}")
    if not path.exists():
        raise PromptError(f"No existe la plantilla {path.relative_to(ROOT)}")
    text = path.read_text(encoding="utf-8").strip()
    needed = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    missing = sorted(n for n in needed if not str(variables.get(n, "")).strip())
    if missing:
        raise PromptError(f"Plantilla '{template}': faltan valores para {missing}")
    return text.format_map({k: str(v).strip() for k, v in variables.items()})


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #
def log(module: str, message: str, campaign: Campaign | str | None = None) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tag = campaign.name if isinstance(campaign, Campaign) else (campaign or "-")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] [{module}] {message}\n")
