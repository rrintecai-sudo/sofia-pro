# HANDOFF — Construcción de Sofia Pro (arquitectura nueva, model-driven)

> Pega esto / pídele a Claude en la ventana nueva: **"Lee HANDOFF_SOFIA_PRO.md y continúa la construcción de Sofia Pro."**

## Contexto / decisión
Sofía actual (`sofia-maple-v3`) está hecha **code-driven**: el código mete bloques fijos (precios, ruteo, menús) y el modelo (Haiku 4.5) solo rellena. Esa rigidez causa **loops, evasiones y respuestas muertas** cuando un papá repregunta o insiste. Lo medimos con un simulador de papás reales (LLM) + juez Sonnet: ~3/15 conversaciones limpias.

**Experimento clave:** cambiamos el modelo principal de Haiku → Sonnet 4.6 en la actual. **No mejoró** (los fallos viven en el código, no en el modelo). Conclusión: el problema es **arquitectura**, no modelo.

**Decisión (Oscar):** construir una **Sofia Pro nueva, model-driven**, correrla EN PARALELO a la actual (canary/A-B), comparar con el mismo simulador. Modelo elegido: **claude-sonnet-4-6**.

## Principio de la arquitectura nueva
- **El modelo (Sonnet) CONDUCE la conversación** — natural, repreguntas, sin loops, sin bloques rígidos.
- **El código es dueño de los DATOS vía TOOLS** — el modelo LLAMA herramientas para precios/horarios/disponibilidad/agendar. Así los números/fechas **siempre salen de la BD, nunca se inventan.**
- Resumen: **modelo = conversación, tools = datos.** Lo mejor de los dos.

## Qué ya está hecho
1. ✅ `sofia-pro/` clonado de `sofia-maple-v3` (capa de datos, KB, web UI, Dockerfile, BD — todo reutilizable, NO reescribir).
2. ✅ Mapeada la interfaz y las funciones reutilizables (abajo).
3. ⏳ FALTA: escribir `app/core/agente.py` (el loop de agente), reconectar el webhook, deploy a servicio nuevo, probar con simulador.

## Interfaz a respetar (el webhook)
`app/api/webhook_web.py`: `POST /webhook/web` recibe `{content}` + cookie de sesión → hoy llama `procesar_turno(...)`. **Cámbialo para llamar al agente nuevo** y devolver el mismo shape: `{session_id, response, turn_number, tokens_input, tokens_output, tokens_cached, cost_usd, latency_ms}` (puedes simplificar: dropear `fase_journey`/`intent` o mandarlos como string fijo). `GET /chat` sirve `web/templates/chat.html` (NO tocar).

## Funciones reutilizables (firmas reales)
- `from app.tools.precios import get_precio, get_todos_precios` → `get_precio(nivel: str) -> PrecioResult|None`. `PrecioResult.bloque_costos()`, `.bloque_gastos_completo()` (desglose+total). Niveles BD: `maternal|kinder|primaria_baja|primaria_alta|secundaria`.
- `from app.tools.horarios import get_horario` → `get_horario(nivel: str) -> HorarioResult|None` (`.bloque()`). Sub-niveles: `kinder_1|kinder_2|kinder_3|primaria_baja|primaria_alta|secundaria|maternal`.
- `from app.tools.estancias import get_estancias, render_estancias_bloque` → `get_estancias(nivel=None) -> list`.
- `from app.tools.campus import get_campus_para_nivel, get_campus_by_id`.
- `from app.tools.becas import ...` (beca hermanos 10%/15% + socioeconómica — son OFICIALES).
- `from app.integrations.leads import create_lead` → `create_lead(parent_name, channel, conversation_session_id, parent_phone=, parent_email=, child_name=, child_age=, child_grade=, nivel=, notes=) -> lead_id|None`.
- `from app.integrations.appointments import create_appointment` → `create_appointment(lead_id, fecha_hora: datetime, duracion_min=60, notas=, campus_id=) -> id|None`.
- `from app.tools.availability_checker import proximos_dias_habiles, resumen_disponibilidad`.
- Persistencia: `from app.core.repository import get_repository` → `repo.insert_message(session_id, role, content, tokens_input=, tokens_output=, cost_usd=, model_used=, latency_ms=)`, `repo.list_recent_messages(session_id, limit) -> [{role, content}]`, `repo.count_turns(session_id)`.
- KB: `app/kb/sofia_kb_oficial.md` (cárgala COMPLETA en el system prompt; ~14k tokens, cacheable).
- Anthropic: `from anthropic import AsyncAnthropic; client = AsyncAnthropic(api_key=settings.anthropic_api_key)`. Usar `client.messages.create(model=settings.anthropic_model_principal, system=..., messages=..., tools=..., max_tokens=1024)` con tool-use loop (stop_reason == "tool_use" → ejecutar → re-llamar).

## Tools a definir (mínimo viable)
`consultar_costos(nivel, grado?, desglose?)`, `consultar_horario(nivel, grado?)`, `consultar_estancia()`, `consultar_campus(nivel?)`, `consultar_becas()`, `dias_disponibles_visita()`, `agendar_visita(nombre_papa, telefono, email?, nombre_hijo, edad_hijo, nivel, dia_iso, hora)`.

## Reglas críticas para el system prompt (anti los bugs de la vieja)
1. **NUNCA** digas un precio/horario/fecha de memoria → SIEMPRE por tool.
2. **Edad→grado:** maternal 0-2 (Cubs<1a, Baby ~1a, Infants ~1.5a, Toddlers 2a+), kinder 3-5 (K1=3,K2=4,K3=5), primaria 6-11 (1°=6…6°=11), secundaria 12-14.
3. **Contenido** (cómo es cada grado/programa) → de la KB, FIEL, sin inventar specifics.
4. **Si NO está en KB/tools** (# exacto alumnos, ratio, comedor, psicólogo, uniformes, lista deportes, transporte) → **HONESTO**: "te lo consigo / lo ves en la visita", ofrece capturar WhatsApp. **NUNCA inventes.**
5. **NO descuentos/becas** salvo los oficiales (hermanos 10%/15%, socioeconómica) — solo si preguntan.
6. **Dos+ hijos:** maneja cada uno, nombra sus grados.
7. **Conduce** hacia la cita de informes. Conciso, cálido, mexicano. **No repitas, no entres en loop.**

## Deploy (servicio NUEVO, paralelo)
- EasyPanel: crear servicio `sofia-pro` en el proyecto `maple-v3` (o nuevo) → URL nueva tipo `sofia-pro.cxjnjn.easypanel.host`.
- Credenciales EasyPanel/Supabase: en `../sofia-maple/.env.local` (EASYPANEL_URL, EASYPANEL_API_TOKEN) y `.env` de este repo (Supabase, Anthropic). MISMA Supabase que la vieja.
- Env var del modelo: `ANTHROPIC_MODEL_PRINCIPAL=claude-sonnet-4-6`.
- tRPC EasyPanel: `services.app.createService` / `updateEnv` / `deployService` (mismo patrón que la vieja; ver scripts en /tmp o pedírmelos).

## Pruebas (A/B)
- Simulador de papás reales: `/tmp/sofia_simulador.py` (15 personas + juez Sonnet). Cambiar `URL` a la de sofia-pro y correr. Baseline vieja: 3/15.
- Casos que la vieja fallaba (deben pasar en Pro): dos hijos por edad, "Cuarto grado", repreguntas de precio/cuotas (desglose), insistencia sin loop, datos que no tiene → defer honesto.

## Estado de la VIEJA (no tocar desde la ventana nueva)
`sofia-v3` está en `claude-haiku-4-5`, con muchos parches de hoy. URL: https://sofia-v3.cxjnjn.easypanel.host. Esa ventana sigue: parches + reportes de Gaby + keep-warm.
