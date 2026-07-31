from __future__ import annotations
"""KD Radar v3 — Motor de prospección server-side.

Ejecuta el pipeline completo desde el servidor (Railway) sin terminal:
elige automáticamente los siguientes (nicho, municipio) pendientes,
captura, extrae, valida, audita y redacta. Alimenta directamente
/api/lote para el envío diario de n8n.

Rotación: recorre cada nicho por todas las localidades antes de pasar
al siguiente nicho. Nunca repite un combo ya prospectado.
"""
import importlib
import threading
import traceback

from config import ANTHROPIC_API_KEY, MUNICIPIOS, NICHOS
from db import init_db, registrar_prospeccion, prospecciones_hechas, stats

# Los módulos empiezan por dígito: se importan vía importlib
mod_buscar = importlib.import_module("1_buscar_negocios")
mod_extraer = importlib.import_module("2_extraer_emails")
mod_validar = importlib.import_module("5_validar_emails")
mod_auditar = importlib.import_module("3_auditar_digital")
# La redacción (Claude) se importa en el momento de usarla: si faltara la
# dependencia o la clave, la captación sigue funcionando igualmente.

_lock = threading.Lock()
estado_motor: dict = {"ocupado": False, "ultima_ejecucion": None, "error": None}


def siguientes_combos(cuantos: int) -> list[tuple[str, str, str]]:
    """Rotación NICHO DIARIO (round-robin): cada ejecución ataca el nicho
    con menos municipios completados -> día 1 restaurantes, día 2 barberías,
    día 3 estética... y al completar la vuelta, siguiente tanda de municipios
    del primer nicho. Nunca repite un combo ya prospectado."""
    hechas = prospecciones_hechas()
    conteo = {n: 0 for n in NICHOS}
    for n, _m in hechas:
        if n in conteo:
            conteo[n] += 1
    total_municipios = len(MUNICIPIOS)
    candidatos = [n for n in NICHOS if conteo[n] < total_municipios]
    if not candidatos:
        return []
    orden = list(NICHOS)
    candidatos.sort(key=lambda n: (conteo[n], orden.index(n)))
    nicho = candidatos[0]
    pendientes = [(nicho, m, p) for m, p in MUNICIPIOS
                  if (nicho, m) not in hechas]
    return pendientes[:cuantos]


def ejecutar_prospeccion(municipios_por_dia: int = 5) -> dict:
    """Ejecuta un ciclo completo de prospección. Devuelve el resumen."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "motivo": "Ya hay una prospección en curso"}
    estado_motor.update(ocupado=True, error=None)
    try:
        init_db()
        combos = siguientes_combos(municipios_por_dia)
        if not combos:
            return {"ok": True, "mensaje": "Todos los nichos y localidades "
                    "ya están prospectados. Añade nichos en config.py."}

        nicho = combos[0][0]
        procesados = []
        for n, municipio, provincia in combos:
            print(f"[MOTOR] Buscando {n} en {municipio}...")
            mod_buscar.procesar_municipio(n, municipio, provincia)
            registrar_prospeccion(n, municipio)
            procesados.append(municipio)

        print("[MOTOR] Extrayendo emails...")
        mod_extraer.main()
        print("[MOTOR] Validando emails (MX)...")
        mod_validar.main()
        print("[MOTOR] Auditando webs...")
        mod_auditar.main(nicho=nicho)

        if ANTHROPIC_API_KEY:
            print("[MOTOR] Redactando emails con Claude...")
            try:
                mod_redactar = importlib.import_module("4_generar_emails")
                mod_redactar.main(nicho=nicho)
            except Exception as e:
                print(f"[MOTOR] Redacción falló ({type(e).__name__}: {e}); "
                      "la captación queda completa igualmente")
        else:
            print("[MOTOR] Redacción saltada: falta ANTHROPIC_API_KEY")

        resumen = {"ok": True, "nicho": nicho,
                   "nicho_nombre": NICHOS[nicho]["nombre"],
                   "municipios": procesados,
                   "stats_nicho": stats(nicho=nicho),
                   "stats_global": stats()}
        estado_motor["ultima_ejecucion"] = resumen
        return resumen
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        estado_motor["error"] = error
        traceback.print_exc()
        return {"ok": False, "motivo": error}
    finally:
        estado_motor["ocupado"] = False
        _lock.release()
