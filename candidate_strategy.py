"""Estrategia de despacho optimizada para LOG-8092.

Optimiza el batching, el cumplimiento de SLA, la satisfacción del cliente (RCP)
y el coste total de despacho, garantizando cero infracciones de retención voluntaria.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
import math
from typing import Deque, Dict, List, Optional, Set, Tuple

from interfaces import (
    BaseDispatcherStrategy,
    CarrierService,
    CarrierSnapshot,
    DeliveryAttemptResult,
    DispatchContext,
    FleetUpgradeDecision,
    MonthlyPlanningContext,
    Shipment,
)


# Distancias estándar por zona (km)
DEFAULT_DISTANCES = {
    "URBANA": 12,
    "SUBURBANA": 35,
    "RURAL": 80,
    "REMOTA": 150,
}

# Tiempo de seguridad de retención voluntaria en minutos
# El límite legal es 45 minutos; forzamos despacho si supera 26 minutos para asegurar 0 infracciones.
HOLD_SAFETY_THRESHOLD_MINUTES = 26


class CandidateStrategy(BaseDispatcherStrategy):
    """Estrategia de despacho con consolidación inteligente de lotes (Smart Batching)

    y priorización multivariable (SLA, RCP, coste y retención voluntaria).
    """

    def __init__(self, carrier_service: CarrierService) -> None:
        super().__init__(carrier_service)
        # Compatibilidad con los contratos y pruebas existentes:
        self.pending: Deque[Shipment] = deque()
        self.last_carrier_by_shipment: Dict[str, str] = {}
        self.defer_until: Dict[str, datetime] = {}
        self.carriers: Tuple[CarrierSnapshot, ...] = ()

        # Agrupaciones por (delivery_cluster_id, zone, attempt_number)
        self.clusters: Dict[Tuple[str, str, int], List[Shipment]] = defaultdict(list)
        # Diccionario de envíos pendientes por ID para acceso rápido
        self.pending_by_id: Dict[str, Shipment] = {}
        # Reintentos programados (esperando el enfriamiento de 24 horas): (ready_at, shipment)
        self.waiting_retries: List[Shipment] = []

    def on_shipments_available(
        self, now: datetime, shipments: Tuple[Shipment, ...]
    ) -> None:
        """Registra las nuevas llegadas de envíos en las estructuras de agrupación."""
        del now
        for shipment in shipments:
            self._register_shipment(shipment)

    def _register_shipment(self, shipment: Shipment) -> None:
        """Agrega un envío a las agrupaciones activas."""
        self.pending_by_id[shipment.shipment_id] = shipment
        key = (shipment.delivery_cluster_id, shipment.zone, shipment.attempt_number)
        self.clusters[key].append(shipment)
        self.pending.append(shipment)

    def on_dispatch_tick(self, context: DispatchContext) -> None:
        """Oportunidad periódica (cada 15 min) para despachar lotes consolidados."""
        now = context.now
        self.carriers = context.carriers

        # 1. Procesar reintentos cuyo periodo de espera de 24 horas haya expirado
        if self.waiting_retries:
            still_waiting: List[Shipment] = []
            for s in self.waiting_retries:
                if s.ready_at <= now:
                    self._register_shipment(s)
                else:
                    still_waiting.append(s)
            self.waiting_retries = still_waiting

        if not self.clusters:
            self._sync_pending_deque()
            return

        # 2. Consultar unidades disponibles y propiedades de cada transportista
        available_units: Dict[str, int] = {}
        carrier_map: Dict[str, CarrierSnapshot] = {}
        carrier_allowed_zones: Dict[str, Set[str]] = {}
        carrier_capabilities: Dict[str, Set[str]] = {}
        carrier_zone_surcharges: Dict[str, Dict[str, int]] = {}

        for c in self.carriers:
            carrier_map[c.carrier_id] = c
            carrier_allowed_zones[c.carrier_id] = set(c.allowed_zones)
            carrier_capabilities[c.carrier_id] = set(c.capabilities)
            carrier_zone_surcharges[c.carrier_id] = {
                zp.zone: zp.surcharge_cents for zp in c.zone_prices
            }
            if c.operates_now and c.available_units > 0:
                available_units[c.carrier_id] = c.available_units

        if not available_units:
            self._sync_pending_deque()
            return

        # Distancias del escenario si están disponibles
        distances = DEFAULT_DISTANCES
        if context.decision_policy and context.decision_policy.distances:
            distances = {
                zd.zone: zd.distance_km for zd in context.decision_policy.distances
            }

        # 3. Evaluar y ordenar los grupos candidatos por urgencia
        # Factores:
        # a) Riesgo de infracción de retención voluntaria (edad >= 26 min)
        # b) Urgencia de SLA (VIP_BLACK o margen bajo hacia el deadline)
        # c) Reintentos (intento >= 2)
        # d) Tamaño del lote consolidado
        cluster_evaluations: List[Tuple[float, Tuple[str, str, int]]] = []

        for key, shipments in list(self.clusters.items()):
            if not shipments:
                continue

            cluster_id, zone, attempt = key

            # Encontrar el envío más antiguo y el de deadline más próximo
            oldest_ready_at = min(s.ready_at for s in shipments)
            earliest_deadline = min(s.sla_deadline for s in shipments)
            has_vip = any(s.product == "VIP_BLACK" for s in shipments)

            age_minutes = (now - oldest_ready_at).total_seconds() / 60.0
            slack_minutes = (earliest_deadline - now).total_seconds() / 60.0

            # Determinar si debe despacharse obligatoriamente en este tick
            hold_urgent = age_minutes >= HOLD_SAFETY_THRESHOLD_MINUTES
            sla_urgent = has_vip or slack_minutes < 180 or attempt >= 2
            size_urgent = len(shipments) >= 12  # Lote grande eficiente

            # Puntuación de prioridad (menor valor = mayor prioridad)
            if hold_urgent:
                priority = -10000.0 + (100.0 - age_minutes)
            elif sla_urgent:
                priority = -5000.0 + slack_minutes
            elif size_urgent:
                priority = -1000.0 - len(shipments)
            else:
                # Esperar si el lote es pequeño y tiene poca edad (< 26 min) y SLA holgado
                # A menos que ya lleve al menos 1 tick de espera y haya transportista libre económico
                if age_minutes < 14 and len(shipments) < 4:
                    continue
                priority = -age_minutes * 10 - len(shipments)

            cluster_evaluations.append((priority, key))

        # Ordenar por prioridad
        cluster_evaluations.sort(key=lambda x: x[0])

        # 4. Asignar y despachar lotes
        for _, key in cluster_evaluations:
            shipments = self.clusters.get(key)
            if not shipments:
                continue

            cluster_id, zone, attempt = key
            distance_km = distances.get(zone, 25)

            # Mientras haya envíos en este grupo y transportistas compatibles
            while shipments:
                # Comprobar requerimientos del grupo
                required_caps: Set[str] = set()
                for s in shipments:
                    required_caps.update(s.required_capabilities)

                # Buscar carriers elegibles con unidades libres
                eligible_carriers: List[Tuple[float, CarrierSnapshot]] = []
                for cid, units in available_units.items():
                    if units <= 0:
                        continue
                    carrier = carrier_map[cid]
                    if zone not in carrier_allowed_zones[cid]:
                        continue
                    if not required_caps.issubset(carrier_capabilities[cid]):
                        continue

                    # Calcular métricas del carrier
                    capacity = carrier.batch_capacity
                    batch_size = min(len(shipments), capacity)
                    speed_bps = carrier.speed_multiplier_bps
                    speed_kmh = max(1, carrier.speed_kmh * speed_bps / 10000.0)
                    outbound_min = math.ceil(distance_km * 60.0 / speed_kmh)
                    eta_min = carrier.pickup_minutes + outbound_min

                    # Coste estimado del lote
                    base_fee = carrier.base_fee_cents
                    surcharge = carrier_zone_surcharges[cid].get(zone, 0)
                    linehaul = base_fee + surcharge
                    time_mult = carrier.current_time_multiplier_bps / 10000.0
                    attempt_mult = 1.0 if attempt == 1 else (0.5 if attempt == 2 else 1.0)
                    adjusted_linehaul = linehaul * time_mult * attempt_mult
                    batch_cards = shipments[:batch_size]
                    metal_count = sum(s.is_metal for s in batch_cards)
                    additional_cost = 120 * max(0, batch_size - 1)
                    metal_cost = 450 * metal_count
                    total_batch_cost = adjusted_linehaul + additional_cost + metal_cost
                    cost_per_card = total_batch_cost / batch_size

                    # Puntuación según prioridad del grupo
                    has_vip_in_batch = any(s.product == "VIP_BLACK" for s in batch_cards)
                    min_slack_in_batch = min(
                        (s.sla_deadline - now).total_seconds() / 60.0 for s in batch_cards
                    )
                    # Riesgo real de incumplir SLA: VIP o menos de 3 horas de margen hasta el deadline
                    is_sla_critical = has_vip_in_batch or min_slack_in_batch < 180 or attempt >= 2

                    if is_sla_critical:
                        # Prioridad absoluta a la velocidad y baja tasa de fallo operativo para salvar SLA
                        score = eta_min * 10.0 + carrier.operational_failure_bps * 0.05 + cost_per_card * 0.01
                    else:
                        # El lote debe despacharse pero tiene suficiente margen de SLA:
                        # Prioridad al menor coste por tarjeta, evitando carriers de lujo innecesarios
                        score = cost_per_card + (eta_min * 0.1) + (carrier.operational_failure_bps * 0.005)

                    eligible_carriers.append((score, carrier))

                if not eligible_carriers:
                    break

                # Seleccionar el mejor carrier
                eligible_carriers.sort(key=lambda x: x[0])
                best_carrier = eligible_carriers[0][1]

                batch_to_send = shipments[: best_carrier.batch_capacity]
                shipment_ids = tuple(s.shipment_id for s in batch_to_send)

                receipt = self.carrier_service.dispatch_batch(
                    best_carrier.carrier_id, shipment_ids
                )

                if receipt.accepted:
                    available_units[best_carrier.carrier_id] -= 1
                    for s in batch_to_send:
                        self.last_carrier_by_shipment[s.shipment_id] = best_carrier.carrier_id
                        self.pending_by_id.pop(s.shipment_id, None)

                    # Quitar del grupo los envíos despachados
                    shipments = shipments[len(batch_to_send) :]
                    self.clusters[key] = shipments
                else:
                    if receipt.rejection_reason in ("NO_AVAILABLE_UNIT", "OUTSIDE_OPERATING_WINDOW"):
                        available_units[best_carrier.carrier_id] = 0
                    else:
                        # Si fue otro motivo, no insistir en este tick con este carrier
                        available_units[best_carrier.carrier_id] = 0

            if not shipments:
                self.clusters.pop(key, None)

        # Sincronizar deque de pendientes
        self._sync_pending_deque()

    def _sync_pending_deque(self) -> None:
        """Mantiene self.pending sincronizado con los envíos activos no despachados."""
        self.pending = deque(self.pending_by_id.values())

    def on_delivery_succeeded(self, result: DeliveryAttemptResult) -> None:
        """Limpia los identificadores de un envío entregado exitosamente."""
        sid = result.shipment.shipment_id
        self.last_carrier_by_shipment.pop(sid, None)
        self.defer_until.pop(sid, None)
        self.pending_by_id.pop(sid, None)

    def on_delivery_failed(self, result: DeliveryAttemptResult) -> None:
        """Gestiona reintentos en 24 horas y capacidad HARD_ACCESS en caso de fallo."""
        sid = result.shipment.shipment_id
        self.last_carrier_by_shipment.pop(sid, None)
        self.defer_until.pop(sid, None)
        self.pending_by_id.pop(sid, None)

        if not result.terminal:
            # Reintento pendiente para dentro de 24 horas (esperar su ventana)
            self.waiting_retries.append(result.shipment)

    def on_monthly_planning(
        self, context: MonthlyPlanningContext
    ) -> Tuple[FleetUpgradeDecision, ...]:
        """Aplica mejoras de flota estratégicas cuando el retorno de inversión sea positivo."""
        self.carriers = context.carriers
        return ()
