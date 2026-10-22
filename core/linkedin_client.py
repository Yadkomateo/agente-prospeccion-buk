"""
Guardián de acciones de LinkedIn.

IMPORTANTE — qué es y qué NO es este módulo:
  * Python NO llama al MCP de LinkedIn. Las herramientas (connect_with_person,
    send_message, search_people, ...) las invoca el agente (Antigravity/Claude).
  * Este módulo es el libro de cuentas y el semáforo que el agente consulta
    ANTES de cada acción y al que reporta DESPUÉS. Así los límites no dependen
    de que un LLM "se acuerde" de cuántas conexiones lleva en el día.

Protocolo para el agente:
    1) authorize  -> {"allowed", "execute", ...}      (si execute=false: simular, NO llamar al MCP)
    2) <llamar al MCP de LinkedIn>
    3) record     -> descuenta cuota (solo si la acción fue real y salió bien)
    4) si el MCP falla con sesión expirada -> mcp-error  (frena todo hasta `resume`)

Cuota compartida: es por CUENTA de LinkedIn, no por campaña. Dos campañas que
usen la misma cuenta descuentan del mismo pool diario (state/linkedin_quota.json).

CLI:
    python -m core.linkedin_client --campaign buk status
    python -m core.linkedin_client --campaign buk authorize connection --n 15 [--real] [--confirmed]
    python -m core.linkedin_client --campaign buk record connection --target "Nombre" --real
    python -m core.linkedin_client --campaign buk mcp-error "texto del error"
    python -m core.linkedin_client --campaign buk halt "motivo" | resume
Códigos de salida: 0 ok/permitido · 3 denegado · 4 requiere confirmación humana · 2 uso incorrecto
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from . import ROOT, Campaign, CampaignError, add_campaign_args, common_args, finalize_args, load_campaign, log

STATE_DIR = ROOT / "state"
QUOTA_PATH = STATE_DIR / "linkedin_quota.json"
HALT_PATH = STATE_DIR / "linkedin_halt.json"

# acción -> clave en campaigns/<x>.json -> limits
ACTION_LIMIT_KEYS = {
    "connection": "daily_connections",
    "message": "daily_messages",
    "profile_view": "daily_profile_views",
}
# Heurística (NO verificada contra el MCP real): textos típicos de sesión caída.
SESSION_ERROR_HINTS = (
    "session", "sesión", "expired", "expir", "login", "log in", "sign in", "authwall",
    "checkpoint", "unauthorized", "401", "403", "captcha", "cookie", "not authenticated",
)


@dataclass
class Decision:
    allowed: bool
    execute: bool          # True solo si es modo real Y está permitido
    mode: str              # "real" | "dry-run"
    action: str
    n: int
    reason: str
    used_today: int = 0
    limit: int = 0
    remaining: int = 0
    needs_confirmation: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _filelock(path: Path):
    try:
        from filelock import FileLock
    except ImportError as exc:  # un lock "falso" daría cuotas corruptas: mejor fallar
        raise RuntimeError("Falta 'filelock' (pip install -r requirements.txt).") from exc
    return FileLock(str(path) + ".lock")


class LinkedInGuard:
    def __init__(self, campaign: Campaign, quota_path: Path = QUOTA_PATH, halt_path: Path = HALT_PATH):
        self.campaign = campaign
        self.quota_path = Path(quota_path)
        self.halt_path = Path(halt_path)
        self.quota_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _filelock(self.quota_path)

    # ---- estado ------------------------------------------------------------
    def _limit(self, action: str) -> int:
        if action not in ACTION_LIMIT_KEYS:
            raise ValueError(f"Acción desconocida: {action!r}. Válidas: {sorted(ACTION_LIMIT_KEYS)}")
        value = self.campaign.get(f"limits.{ACTION_LIMIT_KEYS[action]}")
        if not isinstance(value, int) or value < 0:
            raise CampaignError(
                f"campaigns/{self.campaign.name}.json: limits.{ACTION_LIMIT_KEYS[action]} debe ser entero >= 0."
            )
        return value

    def _fresh(self) -> dict:
        return {"date": date.today().isoformat(), "used": {a: 0 for a in ACTION_LIMIT_KEYS}, "log": []}

    def _read(self) -> dict:
        if not self.quota_path.exists():
            return self._fresh()
        try:
            st = json.loads(self.quota_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Archivo corrupto: se aparta y se arranca de cero. Se avisa en el log.
            self.quota_path.rename(self.quota_path.with_suffix(".json.corrupt"))
            log("linkedin_client", "ARCHIVO DE CUOTA CORRUPTO -> apartado; contador reiniciado", self.campaign)
            return self._fresh()
        if st.get("date") != date.today().isoformat():  # rollover diario con histórico
            hist = STATE_DIR / "history"
            hist.mkdir(parents=True, exist_ok=True)
            (hist / f"linkedin_quota_{st.get('date', 'unknown')}.json").write_text(
                json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return self._fresh()
        for a in ACTION_LIMIT_KEYS:
            st["used"].setdefault(a, 0)
        return st

    def _write(self, st: dict) -> None:
        tmp = self.quota_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.quota_path)

    # ---- freno de emergencia ----------------------------------------------
    def halted(self) -> dict | None:
        if not self.halt_path.exists():
            return None
        try:
            return json.loads(self.halt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"reason": "halt corrupto", "since": "?"}

    def halt(self, reason: str) -> None:
        self.halt_path.write_text(
            json.dumps({"reason": reason, "since": datetime.now().isoformat(timespec="seconds"),
                        "campaign": self.campaign.name}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log("linkedin_client", f"HALT: {reason}", self.campaign)

    def resume(self) -> bool:
        if self.halt_path.exists():
            self.halt_path.unlink()
            log("linkedin_client", "RESUME (levantado por el usuario)", self.campaign)
            return True
        return False

    @staticmethod
    def looks_like_session_expired(error_text: str) -> bool:
        t = (error_text or "").lower()
        return any(h in t for h in SESSION_ERROR_HINTS)

    def report_mcp_error(self, error_text: str) -> dict:
        """Si el error parece sesión caída: frena TODO y devuelve la alerta a mostrar al usuario."""
        if self.looks_like_session_expired(error_text):
            self.halt(f"Posible sesión de LinkedIn expirada/bloqueada: {error_text[:200]}")
            return {"halted": True, "alert": (
                "ALERTA: la sesión del MCP de LinkedIn parece expirada o bloqueada. Envíos detenidos. "
                "No se modificó Google Sheets. Reautentica el MCP y luego ejecuta `resume`.")}
        log("linkedin_client", f"error MCP no clasificado como sesión: {error_text[:200]}", self.campaign)
        return {"halted": False, "alert": "Error del MCP no clasificado como sesión caída; reintentar o revisar manualmente."}

    # ---- API principal -----------------------------------------------------
    def authorize(self, action: str, n: int = 1, real: bool = False, confirmed: bool = False) -> Decision:
        limit = self._limit(action)
        mode = "real" if real else "dry-run"
        with self._lock:
            used = self._read()["used"].get(action, 0)
        remaining = max(limit - used, 0)
        base = dict(mode=mode, action=action, n=n, used_today=used, limit=limit, remaining=remaining)

        h = self.halted()
        if h:
            return Decision(False, False, reason=f"DETENIDO desde {h.get('since')}: {h.get('reason')}. Requiere `resume` del usuario.", **base)
        if n < 1:
            return Decision(False, False, reason="n debe ser >= 1", **base)
        if used + n > limit:
            return Decision(False, False, reason=f"Excede el límite diario ({used}+{n} > {limit}). Máximo permitido ahora: {remaining}.", **base)
        threshold = int(self.campaign.get("limits.bulk_confirm_threshold", 10))
        if real and n > threshold and not confirmed:
            return Decision(False, False, needs_confirmation=True,
                            reason=f"{n} acciones en bloque (> {threshold}): pedir confirmación explícita al usuario y repetir con --confirmed.", **base)
        if not real:
            return Decision(True, False, reason="Simulación: mostrar qué se haría, NO llamar al MCP ni registrar cuota.", **base)
        return Decision(True, True, reason="Permitido. Ejecutar y luego `record`.", **base)

    def record(self, action: str, target: str | None = None, skill: str | None = None) -> dict:
        self._limit(action)
        with self._lock:
            st = self._read()
            st["used"][action] = st["used"].get(action, 0) + 1
            st["log"].append({"ts": datetime.now().isoformat(timespec="seconds"), "campaign": self.campaign.name,
                              "skill": skill, "action": action, "target": target})
            self._write(st)
        log("linkedin_client", f"record {action} -> {target}", self.campaign)
        return st

    def summary(self) -> str:
        with self._lock:
            st = self._read()
        lines = [f"Cuota LinkedIn — {st['date']} (campaña activa: {self.campaign.name})"]
        for action in ACTION_LIMIT_KEYS:
            lim, used = self._limit(action), st["used"].get(action, 0)
            lines.append(f"  {'❌' if used >= lim else '  '} {action:12s} {used:3d}/{lim:3d}  (queda {max(lim-used,0):3d})")
        h = self.halted()
        if h:
            lines.append(f"  ⛔ DETENIDO: {h.get('reason')} (desde {h.get('since')})")
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Guardián de acciones de LinkedIn")
    add_campaign_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common_args()])
    a = sub.add_parser("authorize", parents=[common_args()])
    a.add_argument("action", choices=sorted(ACTION_LIMIT_KEYS))
    a.add_argument("--n", type=int, default=1)
    a.add_argument("--confirmed", action="store_true", help="el usuario ya confirmó el lote")
    r = sub.add_parser("record", parents=[common_args()])
    r.add_argument("action", choices=sorted(ACTION_LIMIT_KEYS))
    r.add_argument("--target")
    r.add_argument("--skill")
    e = sub.add_parser("mcp-error", parents=[common_args()])
    e.add_argument("text")
    h = sub.add_parser("halt", parents=[common_args()])
    h.add_argument("reason")
    sub.add_parser("resume", parents=[common_args()])
    args = p.parse_args(argv)
    finalize_args(p, args)

    try:
        g = LinkedInGuard(load_campaign(args.campaign))
        if args.cmd == "status":
            print(g.summary())
        elif args.cmd == "authorize":
            d = g.authorize(args.action, args.n, real=args.real, confirmed=args.confirmed)
            print(d.to_json())
            return 0 if d.allowed else (4 if d.needs_confirmation else 3)
        elif args.cmd == "record":
            if not args.real:
                print(json.dumps({"recorded": False, "reason": "dry-run: no se descuenta cuota"}))
                return 0
            g.record(args.action, args.target, args.skill)
            print(g.summary())
        elif args.cmd == "mcp-error":
            print(json.dumps(g.report_mcp_error(args.text), ensure_ascii=False))
        elif args.cmd == "halt":
            g.halt(args.reason)
            print(g.summary())
        elif args.cmd == "resume":
            print("Reanudado." if g.resume() else "No había freno activo.")
    except (CampaignError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
