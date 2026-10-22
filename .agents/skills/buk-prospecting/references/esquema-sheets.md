# Esquema de Google Sheets (libro de la campaña)

Pestañas (nombres en `campaigns/<campaña>.json -> sheets`): `Buk Leads`, `Buk Interacciones`, `Señales`.
`core/sheets_manager.py` escribe por **nombre de columna** leyendo la fila 1 real; no reordenes headers a mano sin revisar `check`.

## Buk Leads (30 columnas)
ID - Nombre Completo - Empresa - Cargo - Tier - Segmento ICP - Tipo Persona - Perfil LinkedIn - Email - Verificación Email - Teléfono - Estado Conexión - Fecha Conexión - Contenido Conexión - Saludo Enviado - Fecha Saludo - Contenido Saludo - Respuesta Saludo - Pregunta Abierta Enviada - Fecha Pregunta Abierta - Contenido Pregunta Abierta - Respuesta Pregunta Abierta - Fecha Respuesta - Follow-up Count - Fecha Último Follow-up - Score - Estado Lead - Próxima Acción - Notas - Última Actualización

Columnas opcionales que el diseño original usaba pero la hoja no tenía: `Fuente`, `Fecha Email`. Se añaden con `ensure --add-missing-columns --real`.

## Estados
- **Estado Conexión:** `Solicitud enviada` -> `Conectado`
- **Estado Lead:** `Nuevo` -> `Contactando` (tras saludo) -> `Esperando respuesta` (tras pregunta abierta) -> `En pausa` (tras breakup) | `No interesado` (solo si el lead lo dice)
- **Tipo Persona:** `Influenciador` | `Decision Maker`
- **Tier:** `Tier 1|2|3` (ajuste de la cuenta al ICP; el criterio exacto no está documentado en el material original)

## Buk Interacciones (9 columnas)
ID (`int-NNN`) - Lead_ID - Fecha - Tipo (documentados en el original: `connection_sent`, `follow_up_1`, `breakup`; para saludo y pregunta abierta usa `greeting` y `open_question` - nombres sugeridos, el original no los definía) - Dirección - Contenido - Respuesta - Sentimiento - Resumen
Obligatorios: Fecha, Tipo, Dirección, Contenido. El `Lead_ID` debe existir.

## Señales (14 columnas)
ID (`sig-NNN`) - Fecha_Detectada - Empresa - Cargo - Segmento_ICP_Sugerido - Que_Buscan - Herramientas_Mencionadas - Correo_Contacto - Es_Lead_Existente - Lead_ID_Relacionado - URL_Oferta - Fuente - Estado (`Nueva`|`Usada`) - Notas

## IDs
Prefijos en `campaigns/<campaña>.json -> id_prefixes` (`buk-NNN`, `int-NNN`, `sig-NNN`). Se generan automáticamente.
