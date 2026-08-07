# Equilibra — Checklist de lanzamiento

El sitio ya está en producción, pero todavía no se comunicó: nadie lo conoce y no hay pacientes reservando. El objetivo es **abrir la difusión a principios de septiembre de 2026** (fecha a confirmar por el dueño) con el sitio terminado.

Este documento define **qué significa "terminado"**. Sin esa definición, a tres semanas del lanzamiento no hay forma de decidir si algo entra o queda afuera.

## Cómo se usa

Un ítem está terminado cuando su criterio se puede **verificar**: corriendo un comando, o señalando una decisión escrita en `docs/`. "Se ve bien" no es un criterio.

Hay dos bloques y el orden importa:

1. **Decisiones del dueño** — bloquean trabajo. Nadie más las puede tomar.
2. **Trabajo técnico** — algunos ítems esperan una decisión; el resto puede avanzar ya.

Los comandos de verificación son siempre estos tres:

```bash
powershell -File scripts/verify.ps1    # build determinístico + contenido
python scripts/pack.py                 # empaquetado del deploy
python scripts/contract_test.py        # comportamiento del sitio publicado
```

---

## 1. Decisiones del dueño (bloqueantes)

Ninguna de estas la puede resolver quien programa. La regla 1 de `README.md` es explícita: no se publican sedes, precios ni profesionales sin confirmación del dueño.

| # | Decisión | Por qué bloquea | Estado |
|---|---|---|---|
| D1 | **Evaluación sin cargo o turno arancelado** | `05-operacion-comercial.md` obliga a elegir una sola y prohíbe mezclarlas. Define el texto del hero, de `#turnos` y de la FAQ | Pendiente |
| D2 | **Precios**: evaluación, par de plantillas, promo 2 pares, seña | Hoy el sitio no tiene ninguna señal de precio. Un paciente decide una compra de salud sin saber el orden de magnitud | Pendiente |
| D3 | **Obra social**: se trabaja, no se trabaja, o reintegro | Es de las primeras preguntas de un paciente argentino. Hoy la página no la contesta ni para decir que no | Pendiente |
| D4 | **Quién figura públicamente y con qué credencial** | Noelia: título en trámite, matrícula sin emitir. Francisco: estudiante, no puede figurar como quien realiza la evaluación | Pendiente |
| D5 | **Respaldo de Marcos**: se menciona o no | Es el único con experiencia documentada en esta especialidad. Se decidió no mostrarlo como parte del equipo; falta definir si se menciona la supervisión sin exponer su marca | Pendiente |
| D6 | **Sedes al día del lanzamiento** | Hoy la FAQ 10 dice Belgrano confirmado, Monte Castro y Mercedes en proceso. Tiene que ser exacto el día que llegue tráfico | Pendiente |
| D7 | **Medios y datos de cobro** (alias, MercadoPago, posnet) | Define si el iframe de turnos necesita capacidad de pago y qué se le promete al paciente | Pendiente |

> **D4 es el que más tiempo consume aguas abajo.** Con la matrícula emitida, la frase "la evaluación la realiza una kinesióloga matriculada" es publicable y es el argumento de credibilidad más fuerte disponible. Sin ella, no.

---

## 2. Trabajo técnico

### A. Credibilidad — el hueco más grande

Hoy el sitio no nombra a **ninguna persona**: cero menciones de equipo, profesional, kinesiología o matrícula, y ninguna de las 12 FAQs pregunta quién atiende. El paciente decide una compra de salud sin saber a quién le está creyendo.

| # | Tarea | Criterio de terminado | Depende de |
|---|---|---|---|
| A1 | `content/team.json` como fuente de los datos profesionales | El archivo existe y `verify.ps1` lo valida | — |
| A2 | Guarda en `build.ps1`: el build **falla** si se marca matrícula sin número | Se corre el build con el campo mal puesto y sale con código ≠ 0 | A1 |
| A3 | Sección "quiénes somos" antes de `#turnos` | La sección existe en `src/template.html` y `verify.ps1` pasa | A1, D4 |
| A4 | Dos FAQs nuevas: quién realiza el estudio, qué formación tiene | `verify.ps1` reporta 14 pares Q/A con paridad HTML ↔ JSON-LD | D4 |
| A5 | `entity_statement` y JSON-LD con la persona | El JSON-LD valida y `contract_test.py` pasa | D4 |

**A1 y A2 se pueden hacer hoy**, sin esperar la matrícula. Es el andamiaje: cuando llegue, es cambiar un campo y buildear. La guarda hace que el error que casi cometimos —publicar una credencial no emitida— sea **imposible de cometer**, no sólo evitado esta vez.

### B. Conversión

| # | Tarea | Criterio de terminado | Depende de |
|---|---|---|---|
| B1 | Señal de precio en la página | El texto publicado coincide con lo decidido en D1/D2 y con `05-operacion-comercial.md` | D1, D2 |
| B2 | Respuesta sobre obra social | Existe como FAQ y `verify.ps1` pasa | D3 |
| B3 | Explotar "la evaluación define si necesitás plantillas **o no**" | La idea aparece arriba de la página, no sólo enterrada en una FAQ | — |

> B3 no depende de nadie y es el argumento más desaprovechado que tienen. Decirle al paciente que puede irse sin comprar nada es lo que hace creíble todo lo demás. Hoy vive escondido en la FAQ 1.

### C. Correctitud técnica

| # | Tarea | Criterio de terminado | Estado |
|---|---|---|---|
| C1 | Regresión de `Permissions-Policy: payment` | `contract_test.py` da 11/11 | **Arreglado, empaquetado, sin deployar** |
| C2 | Rampa de HSTS hasta `max-age=2592000` | Ver calendario abajo; `HSTS_MAX_AGE_STAGE` acompaña cada paso | En curso (`300`) |
| C3 | Cache-busting de assets por hash de contenido | Se cambia una imagen, se deploya, y el navegador toma la nueva **sin purgar el CDN** | Pendiente |
| C4 | CSP, primero en `Report-Only` | La consola no muestra violaciones durante una semana antes de pasar a enforcement | Pendiente |

> **C3 es el que más dolor evita.** Los assets se sirven con `max-age=604800` (7 días) y **nombre fijo**: `paso-01.webp`, `logo_horizontal.svg`. Si se cambia una imagen sin cambiar el nombre, los visitantes ven la vieja durante una semana. Es la causa real del ritual de purgar el CDN en cada deploy, y ya costó tiempo dos veces —una en Equilibra y otra en Netiza—. Con nombres por hash, el caché largo pasa a jugar a favor.

### D. Verdad del contenido

| # | Tarea | Criterio de terminado | Depende de |
|---|---|---|---|
| D-1 | Sedes de la FAQ 10 exactas | Coincide con `03-sedes-y-estado.md` revalidado | D6 |
| D-2 | Coherencia precio/gratuidad en toda la página | No conviven "sin cargo" y "arancelada" en ninguna pieza | D1 |
| D-3 | `docs/` al día con lo publicado | Cada claim del sitio tiene respaldo en un doc | D1–D7 |

---

## 3. Calendario de la rampa de HSTS

HSTS es la única directiva del sitio que **el navegador recuerda**. Mientras vive en la cabeza de un visitante, ese navegador se niega a hablar HTTP con el dominio, y sacar el header del `.htaccess` no lo borra de ahí. Por eso sube por escalones, y el mes hasta el lanzamiento es justamente la ventana de observación que necesita.

| Paso | `max-age` | Equivale a | Cuándo |
|---|---|---|---|
| 1 | `300` | 5 minutos | Activo desde 2026-08-07 |
| 2 | `86400` | 1 día | Tras una semana limpia |
| 3 | `604800` | 1 semana | Tras una semana limpia |
| 4 | `2592000` | 30 días | Antes del lanzamiento |

En cada paso se sube `HSTS_MAX_AGE_STAGE` en `scripts/contract_test.py` **en el mismo commit**. El test asserta el techo además de la presencia, así que un salto no planificado falla a propósito.

`includeSubDomains` y `preload` quedan **fuera de este lanzamiento**. `preload` es prácticamente irreversible.

---

## 4. Puerta de lanzamiento

El sitio está listo cuando las tres cosas pasan sobre el sitio publicado:

- [ ] `powershell -File scripts/verify.ps1` termina en `All verification checks passed`
- [ ] `python scripts/contract_test.py` termina en `all checks passed` con código 0
- [ ] Las siete decisiones D1–D7 están escritas en `docs/`, no sólo habladas

Y a mano, una sola vez, en un teléfono real:

- [ ] Reservar un turno de punta a punta, incluida la seña
- [ ] Abrir el sitio desde el iPhone y desde un Android
- [ ] Escribir por WhatsApp desde el botón y verificar que llega

---

## 5. Fuera de alcance

Se dejan afuera a propósito, para que no aparezcan a último momento como sorpresa:

| Tema | Por qué no entra |
|---|---|
| Medir el costo del `backdrop-filter` sobre el canvas WebGL | Requiere un teléfono conectado por USB y `chrome://inspect`. El compositor headless no representa una GPU móvil, así que cualquier número medido desde una PC sería inventado |
| Proxy/API de turnos con backend | Necesita backend propio. Hoy no hay token expuesto en el frontend, así que no hay riesgo abierto |
| `preload` de HSTS | Prácticamente irreversible. No se toca cerca de un lanzamiento |
| Conectar GitHub → Hostinger | Se evaluó el 2026-08-06 y rompería el sitio: `dist/` está gitignoreado, el repo no tiene `index.html` y publicaría `docs/` |
