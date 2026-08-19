# Equilibra — Sedes y estado de apertura

## Regla de oro

Solo se comunica como **confirmada** la sede que ya está lista para atender con los datos exactos provistos por el dueño.  
Hoy hay **exactamente dos sedes confirmadas**. Nada más.

---

## Sedes confirmadas

### Kinest Salud — Monte Castro, CABA

- **Nombre público:** Kinest Salud
- **Dirección:** Dr. David Peña 4235, Monte Castro, CABA
- **Atención:** Sábados de 10 a 18 hs
- **Excepción 22/08/2026 (inicio):** 14 a 18 hs. Horarios libres: 15:00, 15:30, 16:00, 16:30, 17:30, 18:00.
- **Notas:** Es el centro que recibe a los pacientes de Equilibra ese día.

### Equilibra — Ituzaingó, Buenos Aires

- **Nombre público:** Equilibra
- **Dirección:** José Pacífico Otero 826, Ituzaingó, Buenos Aires
- **Atención:** Lunes de 8 a 12 hs (último turno 11:30 hs)
- **Notas:** Posible ampliación a miércoles de 8 a 12 hs más adelante. **No publicar todavía.**

---

## Lo que NO se publica nunca

- **Floresta** — no es una sede. La localidad real del punto es Monte Castro.
- **Belgrano** — no se atiende ahí. Cualquier mención anterior era falsa.
- **Mercedes** — eventual, sin confirmar. Fuera de toda comunicación.

---

## Cómo hablar de las sedes (versión actualizada)

**Versión corta (web / ads):**
> Atendemos en Kinest Salud (Monte Castro) los sábados y en Equilibra (Ituzaingó) los lunes. Consultá disponibilidad.

**Versión para FAQ / JSON-LD:**
Deben aparecer las dos sedes con su nombre público, dirección exacta y horarios.

**Regla estricta:**
Ambas sedes deben estar en el texto visible **y** en los datos estructurados. El grep de verificación no debe encontrar "belgrano|mercedes|floresta" en content/ ni src/.

---

## Checklist antes de considerar "sedes listas"

- [x] Dirección exacta confirmada por el dueño
- [x] Días y horarios confirmados
- [x] Nombre público por sede (Kinest Salud / Equilibra)
- [x] Ambos en texto visible y JSON-LD
- [ ] Verificar que `verify.ps1` pasa y el grep de palabras prohibidas da cero resultados

Este documento reemplaza toda la información anterior que era incorrecta.
