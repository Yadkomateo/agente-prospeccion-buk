# Workspace de prospección para Antigravity

Migración del trabajo de `agents/prospecting-buk` y `agents/job-hunter` (Claude Code) a una estructura modular: **skills + reglas + motor Python + una campaña JSON por cliente**.
Los originales **no se tocaron**: esto es una copia reorganizada.

```
antigravity_workspace/
├── .agents/
│   ├── rules/linkedin-safety.md            # reglas duras, siempre activas
│   └── skills/
│       ├── buk-prospecting/                # flujo conexión -> saludo -> pregunta -> seguimientos -> scoring
│       │   ├── SKILL.md
│       │   └── references/                 # ICP.md, matriz-ganchos.md, esquema-sheets.md
│       └── hr-signals-detector/            # vacantes Nómina/RRHH -> pestaña Señales
│           ├── SKILL.md
│           └── references/                 # keywords-busqueda.md, fuentes-y-extraccion.md
├── core/                                   # motor reutilizable (Python)
│   ├── __init__.py                         # campañas, .env, plantillas (render_prompt), log
│   ├── linkedin_client.py                  # semáforo de cuota + dry-run + freno por sesión expirada
│   ├── sheets_manager.py                   # Sheets idempotente (leads, interacciones, señales)
│   ├── enrichment.py                       # Snov.io, Apify, Clay (webhook)
│   └── scorer.py                           # scoring 0-100
├── campaigns/  buk.json · cliente_nuevo.json
├── prompts/    greeting.txt · validation.txt · follow_ups/{follow_up_1,breakup}.txt
├── .env.example · requirements.txt · .gitignore · README.md
```
(`.gitignore` es el único archivo añadido a tu estructura propuesta; `secrets/`, `state/` y `logs/` se crean solos al ejecutar.)

## Puesta en marcha (5 pasos)
1. `python -m venv .venv` -> activar -> `pip install -r requirements.txt` (Python 3.10+).
2. `cp .env.example .env` y completa credenciales (nunca las pegues en `campaigns/*.json`).
3. Crea `secrets/` y coloca la clave JSON de la cuenta de servicio de Google (la de `prospecting-buk/config/google_service_account.json`; **no la copié**). Comprueba que el libro esté compartido con su `client_email`.
4. Verifica: `python -m core.sheets_manager --campaign buk check` (lista pestañas, filas y columnas faltantes).
5. Prueba en seco: `python -m core.linkedin_client --campaign buk authorize connection --n 5` (debe decir `execute: false`).

## Configurar el MCP de LinkedIn en Antigravity
Nada migra solo: el servidor se registra de nuevo. Es el mismo que usas hoy en Claude Code:
```json
{ "mcpServers": { "linkedin": { "command": "uvx", "args": ["mcp-server-linkedin==4.23.3"] } } }
```
Dónde va ese JSON (`~/.gemini/config/mcp_config.json`, `.agents/mcp_config.json` o el panel *Manage MCP servers*) **varía según las guías que encontré; verifícalo en tu versión**. Tras conectarlo, comprueba los nombres reales de las herramientas (`connect_with_person`, `send_message`, `search_people`, `search_jobs`...): los skills los citan por ese nombre, **sin** el prefijo `mcp__linkedin__` de Claude Code.

## Mapa origen -> destino
| Origen | Destino |
|---|---|
| `prospecting-buk/agent.md` (6 fases, reglas) | `buk-prospecting/SKILL.md` + `linkedin-safety.md` |
| `skills/*.md` (outreach, follow-up, enrichment, sheets) | integrados en `SKILL.md` + `references/esquema-sheets.md` |
| `ICP.md`, tabla de ganchos | `references/ICP.md`, `references/matriz-ganchos.md` |
| `prompts/*` | `prompts/` (parametrizados; la plantilla de invitación con nota quedó **fuera**, está retirada) |
| `scripts/google_sheets_sync.py`, `job-hunter/scripts/signals_sync.py` | `core/sheets_manager.py` |
| `scripts/snovio_*`, `apify_*`, `clay_*` | `core/enrichment.py` |
| `scripts/lead_scoring.py` | `core/scorer.py` |
| `shared/linkedin_quota.py` | dentro de `core/linkedin_client.py` |
| `job-hunter/agent.md` | `hr-signals-detector/SKILL.md` |
| `config/*.json` con claves | **no migrado**; ahora `.env` |
| `heylead`, `omniroute` | **no migrados**: HeyLead figuraba en `agent.md` pero no existía en el código ni en requirements; OmniRoute es del entorno Claude Code |
| Archivos retirados de job-hunter (CV, Drive, postulaciones) | **no migrados** (histórico) |

## Qué mejoró respecto a los scripts originales
- **Idempotencia:** `add_lead` ya no duplica (dedup por URL de LinkedIn); `add_signals_batch` deduplica y cruza con Leads en una sola escritura.
- **Escritura por nombre de columna:** no se corrompe si reordenas columnas.
- **`update_lead`:** 1 llamada batch (antes 1 por celda).
- **Dry-run real por defecto** y semáforo de cuota **hecho en código**, no en un prompt.
- **Cero hardcoding:** spreadsheet, pestañas, prefijos de ID, límites, pesos de scoring y directorio de prompts salen de `campaigns/<x>.json`.
- **Plantillas seguras:** `render_prompt` falla si falta `{trigger}` en vez de enviar llaves crudas.
- **Fallos de sesión:** `mcp-error` frena todo y deja Sheets intacto hasta que tú hagas `resume`.
- **Credenciales fuera del código** y de los JSON de campaña.

Probado con una hoja simulada (dedup, idempotencia, batch, cruce de señales, cuota, confirmación >10, halt/resume, plantillas, CLI). **No se probó contra Google Sheets, Snov.io, Apify, Clay ni LinkedIn reales** (sin credenciales ni acceso desde aquí).

## Replicar para otro cliente: lo honesto
La propuesta dice "solo duplicas un JSON". Es cierto para la **infraestructura** (hoja, límites, IDs, scoring). **No** lo es para el contenido:
1. `cp campaigns/cliente_nuevo.json campaigns/<cliente>.json` y completa los `REEMPLAZAR` (una campaña con valores pendientes falla con error claro).
2. Necesita **su propio ICP**: el skill y las referencias se llaman `buk-prospecting` y contienen el ICP y los ganchos de Buk. Para otro cliente, duplica la carpeta del skill con otro nombre y reescribe `references/`.
3. Revisa `prompts/`: el saludo es genérico, pero la pregunta abierta y los ganchos dependen del producto.
4. La cuenta de LinkedIn y su cuota son **compartidas** entre campañas.

## Por verificar en Antigravity (no lo pude confirmar)
- La página oficial de documentación solo devolvió una redirección; me basé en guías de terceros que **se contradicen** en rutas. Verifica: carpeta de skills (`.agents/skills/` vs `~/.gemini/skills/`), carga de `.agents/rules/` (la guía dice límite de ~12.000 caracteres por archivo; la regla pesa ~3.400) y si el frontmatter `trigger: always_on` es el correcto (lo tomé por analogía; si no, configura la regla como "siempre activa" desde la UI).
- Existencia y semántica de **`/schedule`**. Si no existe, usa el programador del sistema operativo o ejecuta a mano.
- Modos de permisos (Deny/Ask/Allow): pon `Ask` en la herramienta de envío de mensajes/conexiones.
- Los conectores remotos de claude.ai (Gmail, Notion, Apollo, Hunter, Clay...) **no pasan**; este pipeline no los usa.

## Críticas y riesgos (léelos antes de automatizar)
1. **Riesgo de cuenta.** El MCP de LinkedIn opera con una sesión real de navegador; automatizar conexiones y mensajes puede violar los términos de LinkedIn y terminar en restricción de **tu perfil personal**. 40 conexiones/día no es "seguro": es un número que heredamos sin base documentada. Empieza más bajo y sube mirando señales (captchas, avisos).
2. **Texto idéntico a escala.** El saludo es fijo. 30-40 mensajes iguales al día es un patrón fácil de detectar y de ignorar. Considera 2-3 variantes y personalización real.
3. **No automatices envíos reales sin supervisión** (el `/schedule` de lunes a viernes 10:00 es el escenario de mayor riesgo). Programa solo la *preparación* (detectar aceptaciones, redactar borradores, reporte) y aprueba tú los envíos al menos varias semanas.
4. **Los límites los hace cumplir el código, pero el agente debe invocarlo.** Si el modelo ignora la regla y llama al MCP directo, nada lo frena. Por eso el permiso `Ask` en Antigravity es la segunda barrera.
5. **Huecos del diseño original que sigo arrastrando:** no está definido cómo se detecta que una conexión fue *aceptada*, ni qué hace Tier 1/2/3, ni el umbral de handoff al AE, ni el `lead-scoring.md` (estaba vacío). El score está sin calibrar: úsalo para ordenar, no para decidir.
6. **Apify** devolvía 403 y **Clay** `/v1/enrich` no existe (404); ya no se finge que devuelve señales. Clay queda desactivado hasta que definas la tabla/webhook.
7. **La Fase 0 (ingesta de leads) es propuesta mía**: el original no la definía y hubo 71 leads cargados sin registro de origen. Es borrable.
8. `message` queda en **30/día** (tu propuesta), antes eran 100 en `shared/linkedin_quota.py`. `profile_view` 200 se mantuvo.
9. Cruce de empresas por nombre **exacto normalizado**: no detecta "Bolívar" vs "Grupo Bolívar". Es preferible perder una coincidencia a ensuciar un gancho.
