from __future__ import annotations
"""KD Radar v2 — Paso 1: Buscar negocios por nicho (Google Places API New).

Uso:
    python3 1_buscar_negocios.py lista                 # ver nichos disponibles
    python3 1_buscar_negocios.py restaurantes Gandia   # un nicho, un municipio
    python3 1_buscar_negocios.py barberias             # un nicho, TODAS las localidades
"""
import sys
import time

import httpx

from config import GOOGLE_PLACES_API_KEY, MUNICIPIOS, NICHOS, nicho_config
from db import init_db, upsert_lead, stats

URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.delivery",
    "places.rating",
    "places.userRatingCount",
    "places.businessStatus",
    "nextPageToken",
])

HEADERS = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
    "X-Goog-FieldMask": FIELD_MASK,
}


def buscar(query: str) -> list[dict]:
    """Búsqueda de texto con paginación (máx. 3 páginas = 60 resultados)."""
    resultados, page_token = [], None
    with httpx.Client(timeout=30) as cliente:
        for _ in range(3):
            body: dict = {"textQuery": query, "languageCode": "es"}
            if page_token:
                body["pageToken"] = page_token
            r = cliente.post(URL, headers=HEADERS, json=body)
            if r.status_code != 200:
                print(f"  [ERROR {r.status_code}] {r.text[:200]}")
                break
            data = r.json()
            resultados.extend(data.get("places", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(2)
    return resultados


def procesar_municipio(nicho: str, municipio: str, provincia: str) -> int:
    cfg = nicho_config(nicho)
    nuevos = 0
    for plantilla in cfg["queries"]:
        query = plantilla.format(m=municipio, p=provincia)
        for p in buscar(query):
            if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                continue
            web = p.get("websiteUri")
            telefono = p.get("nationalPhoneNumber")
            # Descartar negocios sin ningún canal de contacto: son inútiles
            # (no se puede ni llamar ni escribir ni sacar email de su web)
            if not web and not telefono:
                continue
            upsert_lead({
                "place_id": p["id"],
                "nombre": p.get("displayName", {}).get("text", "Sin nombre"),
                "direccion": p.get("formattedAddress"),
                "municipio": municipio,
                "provincia": provincia,
                "telefono": telefono,
                "web": web,
                "delivery": p.get("delivery", False),
                "rating": p.get("rating"),
                "num_resenas": p.get("userRatingCount"),
                "nicho": nicho,
            })
            nuevos += 1
        time.sleep(0.5)
    return nuevos


def listar_nichos():
    print("Nichos disponibles:\n")
    for clave, cfg in NICHOS.items():
        print(f"  {clave:15} {cfg['nombre']}")
    print("\nUso: python3 1_buscar_negocios.py <nicho> [municipio]")


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "lista":
        listar_nichos()
        return
    if not GOOGLE_PLACES_API_KEY:
        sys.exit("Falta GOOGLE_PLACES_API_KEY en .env")

    nicho = sys.argv[1].lower()
    nicho_config(nicho)  # valida (sale con error si no existe)
    init_db()

    filtro = sys.argv[2].lower() if len(sys.argv) > 2 else None
    objetivos = [(m, p) for m, p in MUNICIPIOS
                 if not filtro or m.lower() == filtro]
    if not objetivos:
        sys.exit(f"Municipio '{sys.argv[2]}' no está en la lista de config.py")

    print(f"Nicho: {NICHOS[nicho]['nombre']} · Localidades: {len(objetivos)}\n")
    for i, (municipio, provincia) in enumerate(objetivos, 1):
        print(f"[{i}/{len(objetivos)}] {municipio} ({provincia})...")
        n = procesar_municipio(nicho, municipio, provincia)
        print(f"  -> {n} resultados procesados")

    print(f"\nResumen del nicho '{nicho}':", stats(nicho=nicho))
    print("Resumen global:", stats())


if __name__ == "__main__":
    main()
