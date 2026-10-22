# Agente de prospección Buk — para Google Antigravity

Agente de prospección B2B en LinkedIn para **Buk** (software de nómina y RRHH, LatAm). Trabaja con **skills** (instrucciones), una **regla de seguridad** siempre activa y un **motor Python** que lleva la cuota diaria y escribe en Google Sheets.

Tiene dos skills:

| Skill | Para qué sirve |
|---|---|
| `buk-prospecting` | Cargar leads, conectar (sin nota), saludar, preguntar, hacer seguimientos, enriquecer y calificar |
| `hr-signals-detector` | Detectar vacantes de Nómina/RRHH en Colombia como señales de compra y guardarlas en la pestaña `Señales` |

> **Por defecto todo es simulación (dry-run).** Nada se envía a LinkedIn ni se escribe en Sheets hasta que tú digas explícitamente "modo real".

---

## 1. Requisitos

- **Google Antigravity** instalado.
- **Python 3.10+** (para el motor `core/`).
- **uv** (trae `uvx`, que ejecuta el MCP de LinkedIn). Instalación:
  - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- Una **cuenta de servicio de Google** con acceso de Editor al libro de Sheets de la campaña.
- Una **cuenta de LinkedIn** (ver advertencia de riesgo al final).

## 2. Instalar el proyecto

```bash
git clone <URL-de-este-repositorio>
cd <carpeta-del-repositorio>

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
mkdir secrets
```

1. Edita `.env` y completa las credenciales que vayas a usar (Snov.io, Apify, etc.).
2. Copia la clave JSON de tu cuenta de servicio de Google a `secrets/google_service_account.json`.
3. Comparte el Google Sheet con el `client_email` de esa cuenta (rol **Editor**).
4. Verifica la conexión:

```bash
python -m core.sheets_manager --campaign buk check
```

Debe listar las pestañas `Buk Leads`, `Buk Interacciones` y `Señales` con su número de filas.

> `.env`, `secrets/`, `state/` y `logs/` están en `.gitignore`. **Nunca los subas a GitHub.**

## 3. Instalar el MCP de LinkedIn

El MCP es [`mcp-server-linkedin`](https://pypi.org/project/mcp-server-linkedin/) (mantenedor: Daniel Sticker; proyecto independiente, no afiliado a LinkedIn). Opera a través de **tu sesión real de navegador**.

### 3.1 Iniciar sesión en LinkedIn (una sola vez)

```bash
uvx mcp-server-linkedin@4.23.3 --login
```

Se abre una ventana de LinkedIn: inicia sesión. La sesión queda guardada en `~/.linkedin-mcp/profile/`. Si LinkedIn pide confirmación en el móvil o un captcha, resuélvelo ahí.

- Si tu versión no reconoce `--login`, prueba con `@latest` (a octubre de 2026 es la 4.27.0).
- Alternativa: `uvx mcp-server-linkedin@4.23.3 --import-from-browser` importa las cookies de tu navegador (puede fallar con Chrome 127+; en ese caso usa `--login`).
- La primera ejecución descarga un Chromium en segundo plano: puede tardar unos minutos.

### 3.2 Registrarlo en Antigravity

1. Abre la configuración de MCP de Antigravity. Según la versión: **Settings → Customizations → Open MCP Config**, o desde el panel del agente: menú **(…) → MCP Servers → Manage → View raw config**. Se abre `mcp_config.json`.
2. Añade el servidor dentro de `mcpServers`:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["mcp-server-linkedin@4.23.3"],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

3. Guarda y pulsa **refresh** en la lista de servidores MCP instalados. Deben aparecer las herramientas (`search_people`, `connect_with_person`, `send_message`, `search_jobs`, `get_person_profile`, `get_inbox`…).

> Antigravity documenta con claridad los servidores remotos (`serverUrl`). El formato local `command`/`args` es el estándar de `mcpServers`, pero **no pude confirmarlo en la documentación oficial de Antigravity**. Si el servidor no aparece o no registra herramientas, revisa la pestaña de logs del servidor MCP y la documentación de tu versión.

### 3.3 Reglas importantes
- **Una sola sesión activa.** Si también usas este MCP en Claude Code, no los corras a la vez: comparten `~/.linkedin-mcp/profile/` y LinkedIn recomienda una sesión.
- **La sesión expira.** Si el agente avisa de "sesión expirada", repite el paso 3.1 y luego ejecuta `resume` (ver sección 6).
- **Versión fijada a propósito** (`4.23.3`, la que usabas en Claude Code). Fijarla evita cambios silenciosos; el costo es perder correcciones. Si algo falla, prueba `@latest`.

## 4. Abrir el proyecto en Antigravity

1. **File → Open Folder** y elige la carpeta del repositorio. Antigravity lee `.agents/` (reglas y skills) de esa carpeta.
2. Abre una conversación con el agente. Para comprobar que cargó todo, pregúntale:
   > ¿Qué skills y reglas tienes disponibles en este proyecto?
   
   Debe mencionar `buk-prospecting`, `hr-signals-detector` y `linkedin-safety`.
3. En los permisos de herramientas, pon **Ask** (pedir confirmación) en `send_message` y `connect_with_person`. Es tu segunda barrera además de la cuota.

> Si el agente no ve los skills, comprueba la carpeta que usa tu versión de Antigravity (`.agents/skills/` en el proyecto, o `~/.gemini/skills/` global). Las guías que encontré no coinciden entre sí.

## 5. Cómo usarlo (ejemplos de lo que le pides al agente)

Siempre empieza en simulación. Ejemplos:

| Quieres… | Dile al agente |
|---|---|
| Buscar leads | *"Busca 10 Jefes de Nómina en Bogotá y prepárame la carga, en simulación."* |
| Cargarlos | *"Carga esos leads a la hoja en modo real."* |
| Conectar | *"Envía las conexiones de los leads en estado Nuevo, en simulación."* |
| Saludar | *"Revisa quién aceptó y prepara los saludos."* |
| Seguimientos | *"¿A quién le toca follow-up hoy?"* |
| Señales | *"Busca vacantes de Nómina y RRHH en Colombia de los últimos 14 días y prepara las señales."* |
| Calificar | *"Califica los leads con respuesta."* |
| Estado de cuota | *"¿Cuánta cuota de LinkedIn me queda hoy?"* |

Para ejecutar de verdad dile explícitamente **"modo real"**. Con más de 10 acciones en un lote, el agente te mostrará la lista completa y esperará tu confirmación.

## 6. Comandos del motor (por si quieres usarlos tú)

Todos llevan `--campaign <nombre>` (lee `campaigns/<nombre>.json`). Sin `--real` no se escribe ni se envía nada.

```bash
# Cuota y seguridad de LinkedIn
python -m core.linkedin_client --campaign buk status
python -m core.linkedin_client --campaign buk authorize connection --n 5
python -m core.linkedin_client --campaign buk resume          # tras reautenticar el MCP

# Google Sheets
python -m core.sheets_manager --campaign buk check
python -m core.sheets_manager --campaign buk leads --status Nuevo
python -m core.sheets_manager --campaign buk add-leads --file leads.json          # simulación
python -m core.sheets_manager --campaign buk add-leads --file leads.json --real   # escribe
python -m core.sheets_manager --campaign buk add-signals --file senales.json

# Scoring
python -m core.scorer --campaign buk --tier "Tier 1" --signals 1 --triggers 2
```

## 7. Reglas de seguridad (resumen)

Están en `.agents/rules/linkedin-safety.md` y el agente debe respetarlas siempre:

- Simulación por defecto; modo real solo si lo pides.
- Máximo **40 conexiones al día** (y límites de mensajes y visitas en `campaigns/buk.json`). Los hace cumplir el código, no la memoria del modelo.
- Confirmación tuya antes de más de **10 acciones** en bloque.
- Invitaciones **sin nota**; mensajes solo tras aceptar; pregunta abierta **sin CTA de llamada**.
- **Nunca inventar emails** ni el dato (`{trigger}`) de un mensaje.
- Si la sesión de LinkedIn expira o aparece un captcha: se detiene todo y se te avisa.

## 8. Estructura

```
.agents/rules/linkedin-safety.md        reglas siempre activas
.agents/skills/buk-prospecting/         skill de prospección (+ references/: ICP, ganchos, esquema de Sheets)
.agents/skills/hr-signals-detector/     skill de señales (+ references/)
core/                                   motor: linkedin_client, sheets_manager, enrichment, scorer
campaigns/                              buk.json y cliente_nuevo.json
prompts/                                plantillas de mensajes
docs/MIGRACION.md                       detalle técnico de la migración, mejoras y huecos conocidos
.env.example · requirements.txt
```

## 9. Otro cliente

Copia `campaigns/cliente_nuevo.json`, completa los campos `REEMPLAZAR` y úsalo con `--campaign <nombre>`. Ojo: el **ICP y los ganchos** viven en los skills (`references/`) y son de Buk; para otro cliente hay que duplicar el skill y reescribirlos. Detalle en `docs/MIGRACION.md`.

## 10. Problemas frecuentes

| Síntoma | Qué hacer |
|---|---|
| `Falta la variable de entorno …` | Completa `.env` (ver `.env.example`) |
| `check` falla con permisos | Comparte el Sheet con el `client_email` de la cuenta de servicio |
| El agente dice "sesión expirada" | Repite `--login` (3.1), luego `... linkedin_client --campaign buk resume` |
| No aparecen herramientas de LinkedIn | Refresca MCP en Antigravity; revisa logs; prueba `@latest`; confirma que `uvx` está en el PATH |
| Excede el límite diario | Es intencional; espera al día siguiente o revisa `limits` en `campaigns/buk.json` |
| Falta un valor en un mensaje | `render_prompt` se niega a enviar con `{llaves}` vacías: aporta el dato real |

## ⚠️ Advertencias

- **Riesgo para tu cuenta.** Automatizar LinkedIn, aun con una sesión real, puede violar su Acuerdo de Usuario y terminar en restricción de tu perfil personal. Empieza con volúmenes bajos y supervisa cada envío.
- **Confidencialidad.** Este repositorio contiene el ICP y la metodología de prospección de Buk. Mantenlo **privado** y confirma que la empresa lo permite.
- **Sin garantías de calibración.** El score no está calibrado con datos reales: sirve para ordenar, no para decidir. Los umbrales de handoff y los criterios de Tier están pendientes de definir.
- **No probado contra servicios reales.** El motor se probó con una hoja simulada; la primera ejecución real con Sheets, Snov.io, Apify y LinkedIn debe hacerse supervisada.
