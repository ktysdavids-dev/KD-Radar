from __future__ import annotations
"""KD Radar v2 — Pipeline completo en un solo comando.

Ejecuta en orden: buscar -> extraer emails -> validar -> auditar ->
redactar (si hay clave Anthropic) -> exportar.

Uso:
    python3 pipeline.py restaurantes Gandia   # nicho + un municipio
    python3 pipeline.py barberias             # nicho + TODAS las localidades
    python3 pipeline.py lista                 # ver nichos disponibles
"""
import subprocess
import sys

from config import ANTHROPIC_API_KEY, NICHOS


def ejecutar(descripcion: str, comando: list[str]) -> bool:
    print(f"\n{'=' * 60}\n>>> {descripcion}\n{'=' * 60}")
    resultado = subprocess.run([sys.executable] + comando)
    if resultado.returncode != 0:
        print(f"\n[AVISO] '{descripcion}' terminó con errores. "
              "Puedes relanzar el pipeline: continúa donde se quedó.")
        return False
    return True


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "lista":
        print("Nichos disponibles:\n")
        for clave, cfg in NICHOS.items():
            print(f"  {clave:15} {cfg['nombre']}")
        print("\nUso: python3 pipeline.py <nicho> [municipio]")
        return

    nicho = sys.argv[1].lower()
    if nicho not in NICHOS:
        sys.exit(f"Nicho '{nicho}' no existe. Ejecuta: python3 pipeline.py lista")
    municipio = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"KD RADAR — Pipeline: {NICHOS[nicho]['nombre']}"
          + (f" · {municipio}" if municipio else " · todas las localidades"))

    args_buscar = ["1_buscar_negocios.py", nicho] + ([municipio] if municipio else [])
    if not ejecutar("1/6 Buscar negocios (Google Places)", args_buscar):
        sys.exit(1)
    ejecutar("2/6 Extraer emails (aviso legal, contacto)", ["2_extraer_emails.py"])
    ejecutar("3/6 Validar emails (sintaxis + MX)", ["5_validar_emails.py"])
    ejecutar("4/6 Auditar activos digitales", ["3_auditar_digital.py", nicho])

    if ANTHROPIC_API_KEY:
        ejecutar("5/6 Redactar emails con Claude", ["4_generar_emails.py", nicho])
    else:
        print("\n[SALTADO] 5/6 Redacción: falta ANTHROPIC_API_KEY en .env")

    ejecutar("6/6 Exportar CSVs", ["6_exportar.py", nicho])

    print("\nPipeline completado. CSVs en ./exports/ "
          "(ábrelos con: open exports)")


if __name__ == "__main__":
    main()
