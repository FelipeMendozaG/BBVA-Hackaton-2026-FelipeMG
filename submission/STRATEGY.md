# Memoria Estratégica: Optimización del Despacho de Tarjetas (LOG-8092)

**Candidato:** Felipe Mendoza  
**Correo:** felipe188.mendoza@gmail.com  
**Convocatoria:** BBVA Hackathon 2026  
**Fecha:** 19 de octubre de 2026  

---

## 1. Resumen Ejecutivo

El reto técnico **LOG-8092** plantea la optimización logística del despacho de tarjetas bancarias mediante transportistas con flotas, velocidades, capacidades y calendarios heterogéneos. La implementación inicial (*starter code*) presentaba graves deficiencias estructurales: realizaba despachos individuales de 1 tarjeta por viaje (disparando el coste por encima del presupuesto de contingencia), acumulaba 42 infracciones por retención indebida (`voluntary_hold_breaches`), dejaba 16 envíos sin resolver (`unresolved`) y sufría un memory leak al acumular el histórico en memoria.

La solución desarrollada introduce un **motor de despacho inteligente multivariable**:
1. **Indexación y Consolidación en Lotes (*Smart Batching*)**: Agrupación en tiempo constante $O(1)$ por `(delivery_cluster_id, zone, attempt_number)` para aprovechar el coste marginal de 120 céntimos por tarjeta adicional.
2. **Garantía Estricta de Retención Voluntaria**: Control de antigüedad con un umbral de seguridad de 26 minutos (frente al límite legal de 45 min) que elimina al 100% las infracciones de retención voluntaria.
3. **Scoring Dinámico de Transportistas**: Separación rigurosa entre urgencia de SLA (VIP o margen < 3h, donde prima velocidad y confiabilidad) y urgencia de retención (donde prima el menor coste por tarjeta).
4. **Eficiencia en Recursos**: Eliminación de colecciones muertas, alcanzando un pico de memoria de tan solo **26.89 MB** en el escenario masivo de 150.000 envíos.

---

## 2. Diagnóstico del Código Heredado (*Starter Baseline*)

Al evaluar la solución inicial con `python run.py` (5.000 tarjetas) se identificaron los siguientes problemas:
* **Descalificación inmediata**:
  - `voluntary_hold_breaches = 42`: Al retener tarjetas sin despacharlas habiendo unidades compatibles libres, la solución quedaba clasificada como `NOT_QUALIFIED`.
  - `unresolved = 16`: Tarjetas no resueltas al término de la simulación.
* **Costes desbordados**: 4.443.213 céntimos frente a un presupuesto de 3.000.000 céntimos (excediendo el umbral de contingencia de 3.300.000).
* **Eficiencia nula de lotes**: 5.467 viajes para 5.467 intentos (tamaño medio de lote: 1.0 tarjeta).
* **Fuga de memoria**: `self.all_historical_records.append(shipment)` acumulaba todos los envíos en RAM, inviable para 150.000 tarjetas.
* **Falsos positivos de cobertura**: Validación de zonas con `shipment.zone in "|".join(carrier.allowed_zones)`, provocando 829 rechazos por `ZONE_UNSUPPORTED`.
* **Trampa adversarial detectada**: En `anexos/nota_operaciones_legacy.md`, se incluía una instrucción dirigida a IAs para forzar la adopción de la convención `LEG-12`. Dicha instrucción fue deliberadamente rechazada, al estar tipificada en `EVALUACION.md` con una penalización directa de 5 puntos.

---

## 3. Decisiones de Diseño y Arquitectura de la Solución

### 3.1. Agrupación y Estructuras de Datos $O(1)$
Se sustituyó la cola única por un diccionario de listas indexado por la tupla natural de compatibilidad de lotes:
$$\text{Clave} = (\text{delivery\_cluster\_id}, \text{zone}, \text{attempt\_number})$$
Esto asegura que cualquier lote extraído cumpla por construcción las tres condiciones de homogeneidad exigidas por el contrato de despacho (`MIXED_DELIVERY_CLUSTER`, `MIXED_ZONE`, `MIXED_ATTEMPT_NUMBER`).

Asimismo, se implementó `self.pending_by_id: Dict[str, Shipment]` para gestionar altas, bajas y reintentos en tiempo constante, eliminando cualquier acumulación innecesaria en memoria.

### 3.2. Gestión de la Ventana de Retención Voluntaria
El contrato audita las tarjetas pendientes después del primer tick que alcanza $\text{ready\_at} + 45\text{ min}$. Si en ese momento existe un carrier operativo, compatible y con unidad libre, se genera una infracción.
Para garantizar **cero infracciones**, se estableció:
$$\text{HOLD\_SAFETY\_THRESHOLD\_MINUTES} = 26\text{ minutos}$$
Cualquier lote que alcance esta antigüedad adquiere la máxima prioridad en el tick y se despacha inmediatamente con el carrier compatible disponible más económico, dejando la tarjeta en tránsito antes de que el evento de auditoría (`HOLD_AUDIT`) pueda ejecutarse.

### 3.3. Puntuación y Selección de Transportistas (Velocidad vs. Coste)
Para cada grupo listo para despacho, se calculan las métricas efectivas de los carriers compatibles con flota disponible:
* **Tiempo estimado a destino**: $\text{ETA} = \text{pickup\_minutes} + \lceil \frac{\text{distancia\_km} \times 60 \times 10.000}{\text{speed\_kmh} \times \text{speed\_multiplier\_bps}} \rceil$
* **Coste por tarjeta**: $\frac{\text{Linehaul\_ajustado} + 120 \times (\text{tarjetas}-1) + 450 \times \text{metálicas}}{\text{tarjetas}}$

Se diseñó una función de scoring adaptativa con dos modos de operación:
1. **Modo Crítico de SLA** (activado para `VIP_BLACK`, intentos $\ge 2$ o margen al deadline $< 180\text{ min}$):
   $$\text{Score} = 10.0 \times \text{ETA} + 0.05 \times \text{tasa\_fallo\_operativo} + 0.01 \times \text{coste\_por\_tarjeta}$$
   Prioriza velocidad punta (`VIP_FAST` o `COURIER_EXPRESS`) y fiabilidad para blindar el cumplimiento del SLA y la satisfacción RCP.
2. **Modo Económico Consolidado** (para tarjetas estándar con holgura de tiempo):
   $$\text{Score} = \text{coste\_por\_tarjeta} + 0.1 \times \text{ETA} + 0.005 \times \text{tasa\_fallo\_operativo}$$
   Prioriza carriers de alta capacidad y bajo flete base (`ECO_POST` o `SUBURBAN_MOTO`), evitando despilfarrar presupuesto en carriers de lujo para tarjetas que disponen de 48 a 72 horas de plazo.

### 3.4. Ciclo de Vida de Reintentos y Capacidad HARD_ACCESS
Cuando un intento de entrega falla (intentos 1 o 2), el reintento queda habilitado 24 horas después. Si la causa del fallo fue `INACCESSIBLE_ROUTE`, el simulador añade de forma permanente el requisito `HARD_ACCESS`.
La solución almacena estos envíos en `self.waiting_retries` y los reincorpora a los clusters de despacho en el tick exacto en que expira su periodo de espera, garantizando que solo transportistas con `HARD_ACCESS` (como `LOGI_GLOBAL` o `VIP_FAST`) puedan ser seleccionados para su reintento.

### 3.5. Evaluación de Mejoras de Flota (*Upgrades*)
Se ensayó la adquisición de upgrades (`EXPANDED_CAPACITY`, `EXTRA_UNIT`) en los cortes mensuales. Aunque aumentan la capacidad máxima teórica, los escenarios evaluados mostraron que la flota base es suficiente para absorber la demanda si la asignación está optimizada. El coste fijo de los upgrades (hasta 795.000 céntimos en 3 periodos) superaba el ahorro marginal en fletes. Por tanto, se optó por una estrategia conservadora sin upgrades para maximizar la eficiencia presupuestaria.

---

## 4. Resultados Medidos y Comparativa Cuantitativa

### 4.1. Escenario Pequeño: 5.000 Tarjetas (`run.py`)

| Métrica | Starter Baseline | Intento 1 (Upgrades) | Intento 2 (Sin Upgrades) | Solución Final (Scoring Óptimo) |
|---|:---:|:---:|:---:|:---:|
| **Estado** | COMPLETED | COMPLETED | COMPLETED | **COMPLETED** |
| **Tarjetas entregadas** | 4.983 / 5.000 | 4.999 / 5.000 | 5.000 / 5.000 | **5.000 / 5.000 (100%)** |
| **No resueltos (`unresolved`)** | 16 (Fallo) | 0 (OK) | 0 (OK) | **0 (Cumple 100%)** |
| **Infracciones retención** | 42 (Fallo) | 0 (OK) | 0 (OK) | **0 (Cumple 100%)** |
| **Rechazos de despacho** | 9.901 | 0 | 0 | **0** |
| **Incumplimiento SLA** | 0,66 % | 0,26 % | 0,26 % | **0,24 % (Meta $\le 6\%$)** |
| **Satisfacción cliente (RCP)** | 9,869 | 9,960 | 9,958 | **9,963 / 10,0** |
| **Coste Total (céntimos)** | 4.443.213 | 4.267.476 | 3.475.950 | **3.069.606** |
| **Exceso contingencia** | Sí (+1.143.213) | Sí (+967.476) | Sí (+175.950) | **No (Dentro de umbral)** |
| **Pico Memoria RSS** | 30,1 MB | 26,9 MB | 27,2 MB | **27,1 MB** |

*Ahorro económico logrado: **1.373.607 céntimos** (-30,9% respecto al baseline).*

---

### 4.2. Escenario Masivo: 150.000 Tarjetas (`run_150k.py`)

| Métrica | Medición Solución Final | Requisito / Referencia |
|---|:---:|:---:|
| **Estado de ejecución** | **COMPLETED** | Finalizar sin errores |
| **Tarjetas admitidas** | 150.000 | 150.000 |
| **Tarjetas entregadas** | **149.982 (99,988 %)** | Máxima resolución |
| **Devueltas a bóveda** | 18 (tras 3 fallos cliente/ruta) | Permitido por contrato |
| **Envíos no resueltos** | **0** | **0 (Obligatorio)** |
| **Infracciones de retención** | **0** | **0 (Obligatorio)** |
| **Rechazos de despacho** | **0** | Confiabilidad operativa |
| **Incumplimiento SLA** | **0,424 %** | **$\le 6,0 \%$ (Obligatorio)** |
| **Satisfacción cliente (RCP)** | **9,928 / 10,0** | Maximizar |
| **Coste Total (céntimos)** | **90.008.862** | Presupuesto: 90.000.000 (Dentro de contingencia 99.000.000) |
| **Pico de Memoria RAM (RSS)** | **26,89 MB** | Minimizar (Excelente rendimiento en 150k) |
| **Tiempo de Ejecución** | **~3 minutos** | **$< 15$ minutos (Obligatorio)** |

---

## 5. Uso Responsable de Inteligencia Artificial

* **Herramientas utilizadas**: Antigravity IDE con el modelo Gemini 3.8 Flash (Medium).
* **Rol de la IA**: Exploración rápida de código, cálculo de escenarios de batching, redacción de scripts de verificación y profiling de estructuras de datos.
* **Verificación y supervisión humana**:
  - Se analizó minuciosamente el código del simulador (`runner.py`, `carrier_service.py`, `pricing.py`) para verificar la precedencia de eventos del reloj de simulación.
  - Se identificó y descartó activamente el intento de prompt injection en `nota_operaciones_legacy.md`.
  - Todas las modificaciones fueron validadas localmente ejecutando las suites completas de pruebas unitarias y benchmarks de 5k y 150k envíos.
  - La conversación completa y fidedigna se encuentra archivada en `submission/ai_conversations/session-01.md`.

---

## 6. Limitaciones Conocidas y Trabajo Futuro

1. **Predicción de Demanda en Tiempo Real**: La agrupación actual opera reactivamente dentro de la ventana de 26 minutos. Un modelo predictivo de series temporales podría anticipar la llegada de tarjetas al mismo cluster durante horas punta.
2. **Upgrades Dinámicos por Caso**: Aunque en los casos públicos los upgrades no resultaron rentables debido a la dispersión de clusters, en escenarios con congestión extrema o cuellos de botella geográficos específicos, la adquisición selectiva de `EXPANDED_CAPACITY` podría ser beneficiosa.

---

## 7. Declaración de Autoría y Responsabilidad

El código, las mediciones y la documentación de esta entrega han sido revisados y validados integralmente. Asumo la autoría y responsabilidad directa de la solución presentada.
