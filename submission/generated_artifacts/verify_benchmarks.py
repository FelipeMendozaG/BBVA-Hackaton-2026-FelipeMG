"""Script de verificación local de métricas y benchmark de candidate_strategy.py."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
REPORT_5K = ROOT / "output" / "performance_report.json"
REPORT_150K = ROOT / "output" / "performance_report_150k.json"


def check_report(path: Path, name: str) -> bool:
    if not path.is_file():
        print(f"[-] {name}: No se encontro el archivo {path}")
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    status = data.get("status")
    unresolved = data.get("counts", {}).get("unresolved", -1)
    hold_breaches = data.get("counts", {}).get("voluntary_hold_breaches", -1)
    sla_breach = data.get("sla", {}).get("breach_rate", 1.0)
    cost = data.get("costs", {}).get("total_cents", 0)
    contingency_exceeded = data.get("costs", {}).get("contingency_exceeded", True)
    rcp = data.get("rcp", {}).get("weighted_score", 0.0)

    print(f"=== Reporte {name} ===")
    print(f"Estado: {status}")
    print(f"Unresolved: {unresolved} (requerido: 0)")
    print(f"Voluntary Hold Breaches: {hold_breaches} (requerido: 0)")
    print(f"SLA Breach Rate: {sla_breach:.4f} (requerido: <= 0.0600)")
    print(f"Contingency Exceeded: {contingency_exceeded}")
    print(f"Coste Total (centimos): {cost}")
    print(f"RCP Score: {rcp}")

    ok = (
        status == "COMPLETED"
        and unresolved == 0
        and hold_breaches == 0
        and sla_breach <= 0.06
        and not contingency_exceeded
    )
    if ok:
        print(f"[+] {name}: Cumple todos los requisitos de calificacion obligatorios.\n")
    else:
        print(f"[!] {name}: Presenta incumplimiento de condiciones obligatorias.\n")
    return ok


if __name__ == "__main__":
    ok_5k = check_report(REPORT_5K, "5.000 envios")
    ok_150k = check_report(REPORT_150K, "150.000 envios")
    if not (ok_5k and ok_150k):
        sys.exit(1)
    print("Verificacion completada con exito.")
