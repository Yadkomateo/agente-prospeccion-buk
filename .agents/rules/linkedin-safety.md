---
trigger: always_on
---
# Reglas de seguridad LinkedIn (siempre activas)

> Estas reglas aplican a cualquier acción sobre LinkedIn, de cualquier skill o campaña.
> Los límites NO dependen de tu memoria: los hace cumplir `core/linkedin_client.py`.

## 1. Simulación por defecto
- Todo se ejecuta en **dry-run** salvo que el usuario diga explícitamente "modo real" / "ejecuta de verdad" **en esta conversación**.
- En dry-run: muestra qué harías (a quién, con qué texto, qué filas de Sheets tocarías). No llames al MCP de LinkedIn, no escribas en Sheets, no descuentes cuota.
- Un "sí" a un plan en dry-run no activa el modo real por sí solo: pregunta "¿lo ejecuto en real?".

## 2. Semáforo antes de cada acción
Antes de **cada** acción (`connection`, `message`, `profile_view`) ejecuta:

`python -m core.linkedin_client --campaign <campaña> authorize <acción> --n <cantidad> [--real]`

- `execute: false` → NO llames al MCP.
- Código de salida `3` (denegado) o `4` (requiere confirmación) → detente y repórtalo.
- Tras una acción real exitosa: `... record <acción> --target "<nombre>" --skill <skill> --real`.
- Si la acción falló, NO hagas `record`.

## 3. Límites diarios (por cuenta de LinkedIn, compartidos entre campañas)
- Máx. **40 solicitudes de conexión/día**. Mensajes y visitas a perfil: según `campaigns/<campaña>.json → limits`.
- Si estás cerca del límite, avisa al usuario antes de agotarlo.

## 4. Confirmación humana en bloque
- Más de **10 acciones** en una misma ejecución → pide confirmación interactiva mostrando la lista completa (nombre, empresa, texto).
- Solo con un "sí" explícito repite el `authorize` con `--confirmed`. Nunca pongas `--confirmed` por tu cuenta.

## 5. Nunca inventar datos
- **Nunca inventes ni "adivines" un email** (ni patrones tipo `nombre.apellido@`, ni dominios deducidos del nombre de la empresa). Solo emails publicados o devueltos por Snov.io/otra fuente verificable; si no hay, el campo queda vacío.
- **Nunca inventes el `{trigger}`** de un mensaje (una sede nueva, un cambio de líder, una vacante). Si no tienes un hecho verificable y citable, no envíes la pregunta abierta: pide el dato al usuario o salta ese lead.

## 6. Reglas de contenido
- Invitaciones de conexión **siempre sin nota**.
- Mensajes solo después de que la conexión esté aceptada.
- La pregunta abierta **no incluye CTA de llamada ni de demo**.
- Redacta solo con las plantillas de `prompts/` vía `render_prompt`. **Jamás envíes un texto que contenga `{llaves}` sin resolver.**
- Máx. 3 reintentos por lead; después, registra en `Notas` y sigue con el siguiente.

## 7. Sesión expirada o bloqueo
- Si el MCP responde con sesión expirada, login, captcha, *checkpoint* o aviso de restricción: ejecuta `python -m core.linkedin_client --campaign <campaña> mcp-error "<texto del error>"`.
- **Detén todos los envíos**, avisa al usuario de inmediato y no sigas con otros leads.
- No intentes iniciar sesión, resolver captchas ni pedir contraseñas. Solo el usuario reautentica el MCP y ejecuta `resume`.
- No dejes Google Sheets a medias: no marques como enviado nada que no se envió.

## 8. Trazabilidad
- Ninguna acción real ocurre sin su registro: `update_lead` + `add_interaction` en Sheets.
- Si el envío salió bien pero Sheets falló: **no reenvíes**. Repórtalo como desfase para corregirlo a mano.
