from __future__ import annotations
"""KD Radar v2 — Paso 3: Auditoría de activos digitales (multinicho).

Analiza la web de cada lead y traduce las carencias a pain points del nicho
(citas telefónicas, reservas, pedidos, CRM/ERP anticuado...).

Uso:
    python3 3_auditar_digital.py            # todos los nichos pendientes
    python3 3_auditar_digital.py barberias  # solo un nicho
"""
import json
import sys
import time

import httpx
from bs4 import BeautifulSoup

from config import NICHOS, nicho_config
from db import init_db, leads_por_estado, actualizar_lead, stats

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PLATAFORMAS_DELIVERY = ["glovo", "ubereats", "uber-eats", "just-eat", "justeat"]
PEDIDO_PROPIO = ["carta.", "/pedido", "/pedir", "/order", "pedidos online",
                 "pide online", "haz tu pedido"]
# Plataformas y señales de cita/reserva online (todos los nichos)
CITAS_ONLINE = ["thefork", "eltenedor", "covermanager", "restoo", "booksy",
                "treatwell", "timify", "calendly", "resurva", "reservio",
                "bewe.", "flowww", "citaonline", "cita-online", "cita previa online",
                "reserva online", "reservar online", "pedir cita online",
                "book online", "appointlet", "setmore"]


def auditar_web(url: str) -> dict:
    a: dict = {"web_activa": False}
    try:
        inicio = time.monotonic()
        with httpx.Client(timeout=15, follow_redirects=True) as c:
            r = c.get(url if url.startswith("http") else "https://" + url,
                      headers={"User-Agent": UA})
        a["tiempo_carga_s"] = round(time.monotonic() - inicio, 2)
        if r.status_code != 200:
            a["error"] = f"HTTP {r.status_code}"
            return a
        a["web_activa"] = True
        a["https"] = str(r.url).startswith("https://")

        html = r.text.lower()
        soup = BeautifulSoup(r.text, "html.parser")

        a["movil_optimizada"] = bool(soup.find("meta", attrs={"name": "viewport"}))
        a["tiene_titulo_seo"] = bool(soup.title and soup.title.string
                                     and len(soup.title.string.strip()) > 5)
        a["tiene_meta_descripcion"] = bool(
            soup.find("meta", attrs={"name": "description"}))
        a["tiene_instagram"] = "instagram.com" in html
        a["tiene_facebook"] = "facebook.com" in html
        a["tiene_whatsapp"] = "wa.me" in html or "api.whatsapp.com" in html
        a["tiene_citas_online"] = any(k in html for k in CITAS_ONLINE)
        a["depende_plataformas_delivery"] = [
            p for p in PLATAFORMAS_DELIVERY if p in html]
        a["tiene_pedido_online_propio"] = any(k in html for k in PEDIDO_PROPIO) \
            and not a["depende_plataformas_delivery"]
    except httpx.HTTPError as e:
        a["error"] = type(e).__name__
    return a


def detectar_pain_points(lead: dict, a: dict) -> list[str]:
    """Traduce la auditoría a dolores de negocio según el nicho del lead."""
    nicho = lead.get("nicho") or "restaurantes"
    cfg = NICHOS.get(nicho, NICHOS["restaurantes"])
    dolores: list[str] = []

    if not a.get("web_activa"):
        dolores.append("Su web no responde o da error: pierden clientes que "
                       "les buscan online cada día")
        dolores.append(f"Dolor del sector: {cfg['dolor']}")
        return dolores

    # --- Específico de restaurantes (delivery) ---
    if cfg.get("usa_delivery") and lead.get("delivery"):
        if a.get("depende_plataformas_delivery"):
            plataformas = ", ".join(a["depende_plataformas_delivery"])
            dolores.append(
                f"Dependen de plataformas de delivery ({plataformas}) que se "
                "quedan hasta un 30% de comisión: un sistema de pedidos propio "
                "recupera ese margen")
        elif not a.get("tiene_pedido_online_propio"):
            dolores.append(
                "Todos los pedidos entran por teléfono: Nora los atiende y "
                "toma 24/7 sin colapsar al personal en hora punta")

    # --- Común a todos los nichos (citas/atención telefónica) ---
    if not a.get("tiene_citas_online"):
        dolores.append(
            f"Sin sistema de citas/reservas online: {cfg['dolor']}")
    if not a.get("tiene_whatsapp"):
        dolores.append(
            "Sin canal de WhatsApp automatizado para citas y recordatorios "
            "(Qenda): los recordatorios reducen los no-shows drásticamente")
    if not a.get("movil_optimizada"):
        dolores.append("La web no está optimizada para móvil (donde busca el "
                       "80% de sus clientes)")
    if not a.get("https"):
        dolores.append("La web no usa HTTPS: Google la penaliza y el navegador "
                       "la marca como no segura")
    if a.get("tiempo_carga_s", 0) > 4:
        dolores.append(f"La web tarda {a['tiempo_carga_s']}s en cargar: cada "
                       "segundo extra son clientes que abandonan")
    if not a.get("tiene_titulo_seo") or not a.get("tiene_meta_descripcion"):
        dolores.append("SEO básico sin trabajar: pierden visibilidad en "
                       "búsquedas locales frente a competidores")

    return dolores[:4]


def main(nicho: str | None = None):
    init_db()
    if nicho is None:
        nicho = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if nicho:
        nicho_config(nicho)
    pendientes = leads_por_estado("con_email", con_web=True, nicho=nicho)
    print(f"Leads pendientes de auditoría: {len(pendientes)}")

    for i, lead in enumerate(pendientes, 1):
        a = auditar_web(lead["web"])
        dolores = detectar_pain_points(lead, a)
        actualizar_lead(lead["id"],
                        auditoria=json.dumps(a, ensure_ascii=False),
                        pain_points=json.dumps(dolores, ensure_ascii=False),
                        estado="auditado")
        print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} "
              f"-> {len(dolores)} pain points")
        time.sleep(0.3)

    print("\nResumen:", stats(nicho=nicho) if nicho else stats())


if __name__ == "__main__":
    main()
