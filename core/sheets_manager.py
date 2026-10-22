"""
Lectura/escritura IDEMPOTENTE en Google Sheets (CRM de leads + interacciones + señales).

Diferencias deliberadas frente a los scripts originales (google_sheets_sync.py / signals_sync.py):
  * Escribe por NOMBRE de columna leyendo la fila 1 real de la hoja (no por posición fija):
    si alguien reordena o añade columnas, no se corrompe nada.
  * Idempotente: add_lead* deduplica por URL de LinkedIn; add_signals_batch por URL_Oferta o Empresa+Cargo.
    Re-ejecutar una carga no duplica filas (antes no había dedup: 71 leads se cargaron sin trazabilidad).
  * update_lead hace UNA llamada batch (antes: una llamada por celda -> rate limit).
  * Dry-run por defecto: sin --real, lee pero no escribe.
  * Prefijos de ID, nombres de pestaña y spreadsheet salen de campaigns/<x>.json (cero hardcoding).
  * Escritura en modo RAW: un texto que empiece por "=" no se interpreta como fórmula.

Cruce de empresas: coincidencia EXACTA tras normalizar (minúsculas, sin tildes, sin sufijo
S.A.S./Ltda/...). No hay coincidencia difusa a propósito: "Bolívar" ≠ "Grupo Bolívar" y un falso
positivo contamina el gancho. Los casos dudosos los revisa una persona.

CLI:
    python -m core.sheets_manager --campaign buk check
    python -m core.sheets_manager --campaign buk ensure [--add-missing-columns] [--real]
    python -m core.sheets_manager --campaign buk leads [--status Nuevo]
    python -m core.sheets_manager --campaign buk signals
    python -m core.sheets_manager --campaign buk add-leads --file leads.json [--real]      # JSON: lista de objetos
    python -m core.sheets_manager --campaign buk add-signals --file senales.json [--real]  # '-' = stdin
    python -m core.sheets_manager --campaign buk update-lead --id buk-071 --json '{"Estado Lead":"Contactando"}' [--real]
    python -m core.sheets_manager --campaign buk add-interaction --lead-id buk-071 --json '{...}' [--real]
Sin --real nada se escribe: se imprime lo que se haría.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import Campaign, CampaignError, add_campaign_args, common_args, finalize_args, load_campaign, log, require_env, ROOT

LEADS_HEADERS = [
    "ID", "Nombre Completo", "Empresa", "Cargo", "Tier", "Segmento ICP",
    "Tipo Persona", "Perfil LinkedIn", "Email", "Verificación Email",
    "Teléfono", "Estado Conexión", "Fecha Conexión", "Contenido Conexión",
    "Saludo Enviado", "Fecha Saludo", "Contenido Saludo", "Respuesta Saludo",
    "Pregunta Abierta Enviada", "Fecha Pregunta Abierta",
    "Contenido Pregunta Abierta", "Respuesta Pregunta Abierta",
    "Fecha Respuesta", "Follow-up Count", "Fecha Último Follow-up", "Score",
    "Estado Lead", "Próxima Acción", "Notas", "Última Actualización",
]
# Columnas que el diseño original usaba pero la hoja no tenía (se añaden solo con --add-missing-columns).
LEADS_OPTIONAL_HEADERS = ["Fuente", "Fecha Email"]
INTERACTIONS_HEADERS = [
    "ID", "Lead_ID", "Fecha", "Tipo", "Dirección", "Contenido", "Respuesta",
    "Sentimiento", "Resumen",
]
SIGNALS_HEADERS = [
    "ID", "Fecha_Detectada", "Empresa", "Cargo", "Segmento_ICP_Sugerido",
    "Que_Buscan", "Herramientas_Mencionadas", "Correo_Contacto",
    "Es_Lead_Existente", "Lead_ID_Relacionado", "URL_Oferta", "Fuente",
    "Estado", "Notas",
]

_COMPANY_SUFFIXES = {"sas", "sa", "ltda", "limitada", "ltd", "inc", "llc", "corp", "cia", "scs", "esp"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- #
# Normalización
# --------------------------------------------------------------------------- #
def _unaccent(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize_linkedin_url(url: str) -> str:
    """https://www.linkedin.com/in/Juan-Perez/?utm=x -> linkedin.com/in/juan-perez"""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    parsed = urlparse(u)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/").lower()
    return f"{host}{path}"


def normalize_company(name: str) -> str:
    if not name:
        return ""
    t = _unaccent(name).lower().replace(".", "").replace(",", " ")
    t = re.sub(r"[^a-z0-9& ]+", " ", t)
    tokens = [x for x in t.split() if x]
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _seg_key(s: str) -> str:
    return _unaccent(s or "").strip().lower()


def _a1(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


# --------------------------------------------------------------------------- #
class SheetsManager:
    def __init__(self, campaign: Campaign, dry_run: bool = True, spreadsheet: Any = None):
        """`spreadsheet` permite inyectar un doble en pruebas; en uso normal se conecta con la cuenta de servicio."""
        self.campaign = campaign
        self.dry_run = dry_run
        self._spreadsheet = spreadsheet
        self._cache: dict[str, tuple[list[str], list[list[str]]]] = {}

    # ---- conexión ----------------------------------------------------------
    @property
    def spreadsheet(self):
        if self._spreadsheet is None:
            try:
                import gspread
            except ImportError as exc:
                raise RuntimeError("Falta 'gspread' (pip install -r requirements.txt).") from exc
            sa_file = require_env("GOOGLE_SERVICE_ACCOUNT_FILE")
            path = Path(sa_file)
            if not path.is_absolute():
                path = ROOT / path
            client = gspread.service_account(filename=str(path))
            self._spreadsheet = client.open_by_key(self.campaign.require("spreadsheet_id"))
        return self._spreadsheet

    def _call(self, fn, *args, **kwargs):
        """Reintenta 429/5xx con backoff exponencial (cuota de lectura/escritura de la API de Sheets)."""
        last = None
        for attempt in range(4):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # gspread.exceptions.APIError u otros transitorios
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    last = exc
                    continue
                raise
        raise last  # pragma: no cover

    def _tab_name(self, key: str) -> str:
        return str(self.campaign.require(f"sheets.{key}"))

    def _ws(self, key: str):
        return self._call(self.spreadsheet.worksheet, self._tab_name(key))

    def _table(self, key: str, refresh: bool = False) -> tuple[list[str], list[list[str]]]:
        """(headers, filas). Cacheado por instancia; las escrituras invalidan."""
        if refresh or key not in self._cache:
            values = self._call(self._ws(key).get_all_values)
            headers = values[0] if values else []
            self._cache[key] = (headers, values[1:] if values else [])
        return self._cache[key]

    def _records(self, key: str) -> list[dict]:
        headers, rows = self._table(key)
        out = []
        for i, r in enumerate(rows, start=2):
            rec = {h: (r[j] if j < len(r) else "") for j, h in enumerate(headers) if h}
            rec["_row"] = i
            out.append(rec)
        return out

    # ---- helpers ------------------------------------------------------------
    def _prefix(self, kind: str) -> str:
        return str(self.campaign.get(f"id_prefixes.{kind}", kind[:3]))

    @staticmethod
    def _next_ids(existing_ids: list[str], prefix: str, count: int) -> list[str]:
        nums = []
        for i in existing_ids:
            m = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", str(i).strip())
            if m:
                nums.append(int(m.group(1)))
        start = (max(nums) + 1) if nums else 1
        return [f"{prefix}-{start + k:03d}" for k in range(count)]

    def _row_for(self, key: str, fields: dict) -> tuple[list[Any], list[str]]:
        """Fila alineada a la fila 1 REAL de la hoja. Devuelve (fila, campos_ignorados)."""
        headers, _ = self._table(key)
        ignored = [k for k in fields if k not in headers and not k.startswith("_")]
        return [fields.get(h, "") for h in headers], ignored

    def _append(self, key: str, rows: list[list[Any]]) -> None:
        if not rows:
            return
        self._call(self._ws(key).append_rows, rows, value_input_option="RAW")
        self._cache.pop(key, None)

    # ---- estructura ----------------------------------------------------------
    def ensure_sheets(self, add_missing_columns: bool = False) -> dict:
        """Crea pestañas faltantes con headers. Si ya existen, SOLO reporta columnas faltantes
        (y las añade al final únicamente con add_missing_columns=True y modo real)."""
        report: dict[str, Any] = {"created": [], "missing_columns": {}, "added_columns": {}, "dry_run": self.dry_run}
        existing = {ws.title: ws for ws in self._call(self.spreadsheet.worksheets)}
        specs = [("leads", LEADS_HEADERS, LEADS_OPTIONAL_HEADERS), ("interactions", INTERACTIONS_HEADERS, []),
                 ("signals", SIGNALS_HEADERS, [])]
        for key, required, optional in specs:
            name = self._tab_name(key)
            if name not in existing:
                report["created"].append(name)
                if not self.dry_run:
                    ws = self._call(self.spreadsheet.add_worksheet, title=name, rows=500, cols=len(required))
                    self._call(ws.update, range_name="A1", values=[required])
                    log("sheets_manager", f"ensure_sheets: creada pestaña '{name}'", self.campaign)
                continue
            headers, _ = self._table(key, refresh=True)
            missing = [h for h in required + optional if h not in headers]
            if missing:
                report["missing_columns"][name] = missing
                if add_missing_columns and not self.dry_run:
                    ws = existing[name]
                    start = len(headers) + 1
                    self._call(ws.update, range_name=_a1(1, start), values=[missing])
                    report["added_columns"][name] = missing
                    self._cache.pop(key, None)
                    log("sheets_manager", f"ensure_sheets: añadidas columnas {missing} a '{name}'", self.campaign)
        return report

    # ---- LEADS ---------------------------------------------------------------
    def get_leads(self, status: str | None = None) -> list[dict]:
        recs = self._records("leads")
        return [r for r in recs if r.get("Estado Lead") == status] if status else recs

    def find_lead_by_url(self, url: str) -> dict | None:
        key = normalize_linkedin_url(url)
        if not key:
            return None
        for r in self._records("leads"):
            if normalize_linkedin_url(r.get("Perfil LinkedIn", "")) == key:
                return r
        return None

    def add_leads_batch(self, leads: list[dict]) -> dict:
        """Alta idempotente. Obligatorios: Nombre Completo, Empresa, Perfil LinkedIn.
        Una sola escritura para todo el lote."""
        existing = self._records("leads")
        seen = {normalize_linkedin_url(r.get("Perfil LinkedIn", "")) for r in existing}
        seen.discard("")
        ids = [r.get("ID", "") for r in existing]
        result: dict[str, Any] = {"created": [], "skipped_existing": [], "invalid": [], "ignored_fields": set(),
                                  "dry_run": self.dry_run}
        candidates = []
        for lead in leads:
            missing = [k for k in ("Nombre Completo", "Empresa", "Perfil LinkedIn") if not str(lead.get(k, "")).strip()]
            if missing:
                result["invalid"].append({"lead": lead.get("Nombre Completo") or "?", "missing": missing})
                continue
            key = normalize_linkedin_url(lead["Perfil LinkedIn"])
            if key in seen:
                result["skipped_existing"].append(lead["Perfil LinkedIn"])
                continue
            seen.add(key)
            candidates.append(dict(lead))
        new_ids = self._next_ids(ids, self._prefix("lead"), len(candidates))
        rows = []
        for fields, new_id in zip(candidates, new_ids):
            fields["ID"] = new_id
            fields.setdefault("Estado Lead", "Nuevo")
            fields.setdefault("Última Actualización", date.today().isoformat())
            row, ignored = self._row_for("leads", fields)
            result["ignored_fields"].update(ignored)
            rows.append(row)
            result["created"].append({"ID": new_id, "Nombre Completo": fields["Nombre Completo"], "Empresa": fields["Empresa"]})
        result["ignored_fields"] = sorted(result["ignored_fields"])
        if not self.dry_run:
            self._append("leads", rows)
        log("sheets_manager",
            f"add_leads_batch dry_run={self.dry_run}: +{len(result['created'])} nuevos, "
            f"{len(result['skipped_existing'])} ya existían, {len(result['invalid'])} inválidos", self.campaign)
        return result

    def add_lead(self, data: dict) -> dict:
        return self.add_leads_batch([data])

    def update_lead(self, lead_id: str, data: dict) -> dict:
        """Actualiza campos de un lead en UNA llamada batch. Siempre refresca 'Última Actualización'."""
        headers, _ = self._table("leads")
        rec = next((r for r in self._records("leads") if r.get("ID") == lead_id), None)
        if rec is None:
            return {"found": False, "lead_id": lead_id}
        fields = dict(data)
        if "Última Actualización" in headers:
            fields["Última Actualización"] = date.today().isoformat()
        updates, applied, ignored = [], [], []
        for k, v in fields.items():
            if k == "ID" or k not in headers:
                ignored.append(k)
                continue
            updates.append({"range": _a1(rec["_row"], headers.index(k) + 1), "values": [[v]]})
            applied.append(k)
        if updates and not self.dry_run:
            self._call(self._ws("leads").batch_update, updates)
            self._cache.pop("leads", None)
        log("sheets_manager", f"update_lead({lead_id}) dry_run={self.dry_run} campos={applied}", self.campaign)
        return {"found": True, "lead_id": lead_id, "applied": applied, "ignored": ignored, "dry_run": self.dry_run}

    def add_interaction(self, lead_id: str, data: dict) -> dict:
        """Regla del flujo: ninguna acción sin su interacción registrada."""
        required = {"Fecha", "Tipo", "Dirección", "Contenido"}
        if required - data.keys():
            raise ValueError(f"Faltan campos obligatorios en la interacción: {sorted(required - data.keys())}")
        if not any(r.get("ID") == lead_id for r in self._records("leads")):
            raise ValueError(f"Lead_ID inexistente: {lead_id}")
        ids = [r.get("ID", "") for r in self._records("interactions")]
        fields = dict(data)
        fields["ID"] = data.get("ID") or self._next_ids(ids, self._prefix("interaction"), 1)[0]
        fields["Lead_ID"] = lead_id
        fields.setdefault("Resumen", f"{data['Tipo']} - {data['Fecha']}")
        row, ignored = self._row_for("interactions", fields)
        if not self.dry_run:
            self._append("interactions", [row])
        log("sheets_manager", f"add_interaction({lead_id}, {data['Tipo']}) dry_run={self.dry_run}", self.campaign)
        return {"interaction": fields, "ignored_fields": ignored, "dry_run": self.dry_run}

    # ---- SEÑALES --------------------------------------------------------------
    def get_signals(self) -> list[dict]:
        return self._records("signals")

    def leads_by_company(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self._records("leads"):
            k = normalize_company(r.get("Empresa", ""))
            if k:
                out.setdefault(k, []).append(r)
        return out

    def add_signals_batch(self, signals: list[dict]) -> dict:
        """Inserta señales en UNA escritura. Dedup contra la hoja y dentro del propio lote;
        cruza con 'Leads' para rellenar Es_Lead_Existente / Lead_ID_Relacionado."""
        existing = self.get_signals()
        seen_urls = {s.get("URL_Oferta", "").strip() for s in existing if s.get("URL_Oferta", "").strip()}
        seen_pairs = {(normalize_company(s.get("Empresa", "")), _seg_key(s.get("Cargo", ""))) for s in existing}
        by_company = self.leads_by_company()
        valid_segments = {_seg_key(x) for x in self.campaign.get("icp_segments", [])}
        result: dict[str, Any] = {"added": [], "duplicates": [], "invalid": [], "warnings": [], "existing_lead_matches": [],
                                  "dry_run": self.dry_run}
        prepared = []
        for sig in signals:
            empresa, cargo = str(sig.get("Empresa", "")).strip(), str(sig.get("Cargo", "")).strip()
            if not empresa or not cargo:
                result["invalid"].append({"signal": sig, "missing": "Empresa/Cargo"})
                continue
            url = str(sig.get("URL_Oferta", "")).strip()
            pair = (normalize_company(empresa), _seg_key(cargo))
            if (url and url in seen_urls) or pair in seen_pairs:
                result["duplicates"].append(f"{empresa} — {cargo}")
                continue
            if url:
                seen_urls.add(url)
            seen_pairs.add(pair)
            fields = dict(sig)
            mail = str(fields.get("Correo_Contacto", "")).strip()
            if mail and not _EMAIL_RE.match(mail):
                result["warnings"].append(f"{empresa}: Correo_Contacto con formato inválido, se dejó vacío")
                fields["Correo_Contacto"] = ""
            seg = str(fields.get("Segmento_ICP_Sugerido", "")).strip()
            if seg and valid_segments and _seg_key(seg) not in valid_segments:
                result["warnings"].append(f"{empresa}: segmento '{seg}' no está en icp_segments de la campaña")
            matches = by_company.get(normalize_company(empresa), [])
            fields["Es_Lead_Existente"] = bool(matches)
            fields["Lead_ID_Relacionado"] = ", ".join(m.get("ID", "") for m in matches)
            if matches:
                result["existing_lead_matches"].append({"empresa": empresa, "leads": [m.get("ID") for m in matches]})
            fields.setdefault("Fecha_Detectada", date.today().isoformat())
            fields.setdefault("Estado", "Nueva")
            prepared.append(fields)
        new_ids = self._next_ids([s.get("ID", "") for s in existing], self._prefix("signal"), len(prepared))
        rows = []
        for fields, new_id in zip(prepared, new_ids):
            fields["ID"] = new_id
            row, _ = self._row_for("signals", fields)
            rows.append(row)
            result["added"].append({"ID": new_id, "Empresa": fields["Empresa"], "Cargo": fields["Cargo"]})
        if not self.dry_run:
            self._append("signals", rows)
        log("sheets_manager",
            f"add_signals_batch dry_run={self.dry_run}: +{len(rows)}, {len(result['duplicates'])} duplicadas", self.campaign)
        return result


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Gestor de Google Sheets")
    add_campaign_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", parents=[common_args()])
    e = sub.add_parser("ensure", parents=[common_args()])
    e.add_argument("--add-missing-columns", action="store_true")
    l = sub.add_parser("leads", parents=[common_args()])
    l.add_argument("--status")
    sub.add_parser("signals", parents=[common_args()])
    for name in ("add-leads", "add-signals"):
        sp = sub.add_parser(name, parents=[common_args()])
        sp.add_argument("--file", required=True, help="JSON con una lista de objetos ('-' = stdin)")
    ul = sub.add_parser("update-lead", parents=[common_args()])
    ul.add_argument("--id", required=True)
    ul.add_argument("--json", required=True, help="objeto JSON con los campos a actualizar")
    ai = sub.add_parser("add-interaction", parents=[common_args()])
    ai.add_argument("--lead-id", required=True)
    ai.add_argument("--json", required=True, help="objeto JSON de la interacción")
    args = p.parse_args(argv)
    finalize_args(p, args)
    try:
        sm = SheetsManager(load_campaign(args.campaign), dry_run=not args.real)
        if args.cmd == "check":
            for key, req in (("leads", LEADS_HEADERS), ("interactions", INTERACTIONS_HEADERS), ("signals", SIGNALS_HEADERS)):
                headers, rows = sm._table(key)
                miss = [h for h in req if h not in headers]
                print(f"{sm._tab_name(key)}: {len(rows)} filas · columnas faltantes: {miss or 'ninguna'}")
        elif args.cmd == "ensure":
            print(sm.ensure_sheets(add_missing_columns=args.add_missing_columns))
        elif args.cmd == "leads":
            leads = sm.get_leads(args.status)
            print(f"{len(leads)} leads" + (f" con Estado Lead = {args.status}" if args.status else ""))
            for r in leads:
                print(f"- {r.get('ID')} · {r.get('Nombre Completo')} @ {r.get('Empresa')} [{r.get('Estado Lead')}]")
        elif args.cmd == "signals":
            sigs = sm.get_signals()
            print(f"{len(sigs)} señales")
        elif args.cmd in ("add-leads", "add-signals"):
            raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("El JSON debe ser una lista de objetos.")
            res = sm.add_leads_batch(items) if args.cmd == "add-leads" else sm.add_signals_batch(items)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "update-lead":
            print(json.dumps(sm.update_lead(args.id, json.loads(args.json)), ensure_ascii=False, indent=2))
        elif args.cmd == "add-interaction":
            print(json.dumps(sm.add_interaction(args.lead_id, json.loads(args.json)), ensure_ascii=False, indent=2))
    except (CampaignError, ValueError, RuntimeError) as exc:  # ValueError incluye JSON inválido
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
