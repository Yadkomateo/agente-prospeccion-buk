---
name: buk-prospecting
description: Prospección B2B consultiva en LinkedIn para Buk (nómina y RRHH, LatAm) - conexión sin nota, saludo, pregunta abierta, seguimientos, enriquecimiento y scoring, con trazabilidad en Google Sheets. Úsalo cuando el usuario pida cargar leads, enviar conexiones/saludos/seguimientos, enriquecer o calificar leads de Buk.
---

# Skill: Prospección Buk (BDR Enterprise)

Lema: **"Vender es ayudar, no insistir o perseguir."** Tono relajado pero con propósito, directo, párrafos cortos, datos concretos. Nada de jerga corporativa.

## Antes de empezar
1. Lee y respeta `.agents/rules/linkedin-safety.md` (dry-run por defecto, semáforo de cuota, confirmación >10, nunca inventar datos).
2. Campaña activa: `buk` (`campaigns/buk.json`). Todos los comandos llevan `--campaign buk`. Para otro cliente, otra campaña.
3. Comprueba que `.env` tenga credenciales y que el MCP `linkedin` esté conectado. Si falta algo, dilo y para; no improvises.
4. Estado de la hoja: `python -m core.sheets_manager --campaign buk check`.

Referencias: `references/ICP.md` (segmentos y señales), `references/matriz-ganchos.md` (`{topic}` por segmento), `references/esquema-sheets.md` (columnas y estados).

## Convención de comandos
- Cuota/semáforo: `python -m core.linkedin_client --campaign buk authorize|record|status|mcp-error`
- Sheets: `python -m core.sheets_manager --campaign buk leads|add-leads|update-lead|add-interaction`
- Plantillas: `core.render_prompt(campaign, "greeting", name=...)` (falla si falta una variable)
- Enriquecimiento: `python -m core.enrichment --campaign buk email|profile|clay`
- Scoring: `python -m core.scorer --campaign buk --tier "Tier 1" --signals N --triggers N`
- Agrega `--real` solo cuando el usuario haya pedido modo real.

## Fase 0 - Ingesta de leads (por orden del usuario) [PROPUESTA NUEVA]
> No existía en el `agent.md` original: allí los leads "aparecían" en la hoja sin proceso definido. Se documenta para que sea trazable. Bórrala si no la quieres.
1. El usuario indica segmento/cargo/empresa/cantidad. Busca con la herramienta `search_people` del MCP `linkedin` (consume cuota de `profile_view`: usa `authorize`).
2. Clasifica cada persona en `Segmento ICP` y `Tipo Persona` (Influenciador / Decision Maker) según `references/ICP.md`. Si dudas, déjalo en blanco y márcalo en `Notas`; no adivines.
3. Arma un JSON y muéstralo al usuario (dry-run): `add-leads --file leads.json`. Incluye `Fuente` (ej. "LinkedIn search: Jefe de Nómina Bogotá") para auditar.
4. La carga es idempotente (dedup por URL de LinkedIn). Con >10 leads pide confirmación. Entra con `Estado Lead = Nuevo`.

## Fase 1 - Conexión (Día 0)
- **Trigger:** `Estado Lead = Nuevo`.
- **Regla vigente desde 2026-10-16:** la invitación va **sin mensaje ni nota** (`connect_with_person` sin `note`).
- **Acción:** `authorize connection` -> si `execute:true`, enviar -> `record` -> `update-lead` con `Estado Conexión=Solicitud enviada`, `Fecha Conexión=hoy`, `Contenido Conexión=Sin nota` -> `add-interaction` tipo `connection_sent`, `Contenido=Sin nota`.

## Fase 2 - Saludo (Día 2, condicional)
- **Trigger:** `Estado Conexión = Conectado` Y `Saludo Enviado` no es `true`. Nunca antes de la aceptación.
- **Detectar aceptaciones:** usa la herramienta del MCP que liste conexiones/perfil (el `agent.md` original no documentaba cuál; verifica qué expone el servidor) y actualiza `Estado Conexión=Conectado`. Si no puedes verificarlo, pregunta al usuario; no asumas.
- **Texto:** `render_prompt(campaign, "greeting", name=<nombre>)`. Texto fijo.
- **Acción:** `authorize message` -> `send_message` -> `record` -> `update-lead` (`Saludo Enviado=true`, `Fecha Saludo`, `Contenido Saludo`, `Estado Lead=Contactando`) -> `add-interaction`.

## Fase 3 - Pregunta abierta / validación
- **Trigger:** saludo respondido, o 2 días hábiles sin respuesta al saludo.
- **Texto:** `render_prompt(campaign, "validation", name, company, trigger, topic)`.
  - `{trigger}`: un **hecho verificable** sobre la empresa (sedes nuevas, cambio de liderazgo, contratación intensiva, vacante de Nómina/RRHH detectada en la pestaña `Señales`). Si no hay hecho verificable, **no envíes**: pide el dato o salta el lead.
  - `{topic}`: según `Segmento ICP` (ver `references/matriz-ganchos.md`).
- **Reglas:** dato de valor concreto + **pregunta abierta** (no sí/no) + **sin CTA de llamada/demo**.
- **Acción:** `authorize message` -> enviar -> `record` -> `update-lead` (`Pregunta Abierta Enviada=true`, `Fecha Pregunta Abierta`, `Contenido Pregunta Abierta`, `Estado Lead=Esperando respuesta`) -> `add-interaction`.

## Fase 4 - Enriquecimiento (en paralelo, no bloquea envíos)
- **Trigger:** lead nuevo en la hoja.
- Email: Snov.io (`core.enrichment email`) -> `Email`, `Verificación Email` (`valid|invalid|unknown`). Necesita el **dominio real** de la empresa; no lo deduzcas del nombre. Sin resultado = campo vacío.
- Perfil: Apify (`profile`) -> resumen en `Notas`. Si falla o tarda >30 s, sigue con el siguiente y reintenta luego. (Históricamente el actor devolvía 403.)
- Señales: Clay solo recibe el lead por webhook (`clay`); las señales se leen en Clay. Desactivado por defecto en `campaigns/buk.json`.
- Un fallo de API nunca detiene el flujo del lead: se reintenta en la siguiente ejecución y se registra en `logs/agent_actions.log`.

## Fase 5 - Seguimientos (no responde)
- **Follow-up 1 (día 7 tras la pregunta abierta):** `Follow-up Count=0` y sin respuesta -> `render_prompt(campaign, "follow_ups/follow_up_1", name, topic)`. Tras enviarlo: `Follow-up Count=1`, `Fecha Último Follow-up`; `Estado Lead` no cambia. Interacción `follow_up_1`.
- **Breakup (día 14):** `Follow-up Count=1` -> `render_prompt(campaign, "follow_ups/breakup", name, topic)`. Luego `Follow-up Count=2`, `Estado Lead=En pausa` (no es descarte). Interacción `breakup`.
- `No interesado` solo si el lead lo dice explícitamente.
- Respeta mínimo 2 días hábiles entre mensajes. Es el último contacto automático.

## Fase 6 - Calificación
- **Trigger:** hay respuesta, o enriquecimiento suficiente.
- `python -m core.scorer ...` -> actualizar `Score`. Usa el score para **ordenar**: la fórmula no está calibrada (ver `campaigns/buk.json -> scoring._note`).
- `handoff_to_ae` es `null` mientras el usuario no defina `scoring.handoff_min_score`. No inventes un umbral: pregúntalo.

## Alertas (avisa al usuario)
Lead Tier 1 responde o acepta - una API falla 3+ veces para el mismo lead - te acercas al límite diario - un lead alcanza score de handoff - sesión de LinkedIn expirada (detener todo).

## Reporte al cerrar cada ejecución
Conexiones enviadas/aceptadas - saludos y preguntas abiertas - follow-ups/breakups - leads enriquecidos y score - errores o leads que necesitan atención manual - cuota restante (`status`).

## Programación (opcional, por verificar)
Idea: lunes a viernes 10:00 revisar aceptaciones y preparar saludos; enriquecimiento tras cada tanda. Antes de automatizar, corre varias semanas **supervisado**; una automatización que envía mensajes reales sin revisión sobre una cuenta personal es el escenario de mayor riesgo. Ver README.
