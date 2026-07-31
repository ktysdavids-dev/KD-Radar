from __future__ import annotations
"""KD Radar v2 — Paso 4: Redactar emails personalizados con Claude (multinicho).

El prompt se adapta al nicho del lead: productos y dolor del sector.
Cada email incluye identificación de la empresa y enlace de baja (LSSI).

Uso:
    python3 4_generar_emails.py            # todos los nichos pendientes
    python3 4_generar_emails.py talleres   # solo un nicho
"""
import json
import sys
import time

import anthropic

from config import (ANTHROPIC_API_KEY, BASE_URL, REMITENTE_NOMBRE,
                    EMPRESA_LEGAL, NICHOS, nicho_config)
from db import init_db, leads_por_estado, actualizar_lead, cargar_json, stats

MODELO = "claude-sonnet-4-6"

SYSTEM_BASE = """Eres el mejor copywriter de ventas B2B de España, escribiendo
para Ktys & Davids, agencia de desarrollo IA full-stack de David Amundarain.

Sector del destinatario: {sector}.
Productos a ofrecer para este sector: {productos}.
Dolor principal del sector: {dolor}.
Nuestros sistemas ya funcionan en producción en negocios reales de la
Comunidad Valenciana.

Reglas del email:
- Español de España, tono directo y cercano, cero humo corporativo.
- Máximo 130 palabras en el cuerpo.
- Primera frase: personalizada con UN dato concreto de SU negocio o su web
  (demuestra que no es un envío masivo genérico).
- Desarrolla 1-2 pain points como problema de negocio (tiempo o dinero
  perdido), no como lista técnica.
- CTA única: proponer una llamada de 10 minutos esta semana.
- Sin adjuntos, sin enlaces salvo los que se te indiquen, sin mayúsculas
  agresivas, sin palabras spam ("gratis!!!", "oferta limitada").
- Asunto: máximo 7 palabras, específico, sin clickbait.

Responde SOLO con JSON válido, sin markdown ni texto extra:
{{"asunto": "...", "cuerpo": "..."}}"""

PIE_LEGAL = """

--
{remitente}
{empresa} · ktysdavids.com
Recibes este email como comunicación comercial B2B dirigida al buzón público de tu negocio.
Si no quieres recibir más emails, date de baja aquí (un clic): {baja_url}"""


def redactar(cliente: anthropic.Anthropic, lead: dict) -> dict | None:
    cfg = NICHOS.get(lead.get("nicho") or "restaurantes",
                     NICHOS["restaurantes"])
    system = SYSTEM_BASE.format(sector=cfg["nombre"],
                                productos=cfg["productos"],
                                dolor=cfg["dolor"])
    contexto = {
        "negocio": lead["nombre"],
        "municipio": lead["municipio"],
        "rating_google": lead.get("rating"),
        "num_resenas": lead.get("num_resenas"),
        "web": lead.get("web"),
        "pain_points_detectados": cargar_json(lead.get("pain_points")) or [],
        "auditoria_tecnica": cargar_json(lead.get("auditoria")) or {},
    }
    try:
        r = cliente.messages.create(
            model=MODELO,
            max_tokens=700,
            system=system,
            messages=[{
                "role": "user",
                "content": ("Redacta el email para este negocio. "
                            "Datos reales de la auditoría:\n"
                            + json.dumps(contexto, ensure_ascii=False, indent=2)),
            }],
        )
        texto = r.content[0].text.strip()
        if texto.startswith("```"):
            texto = texto.strip("`").removeprefix("json").strip()
        datos = json.loads(texto)
        if not datos.get("asunto") or not datos.get("cuerpo"):
            return None
        return datos
    except (anthropic.APIError, json.JSONDecodeError, IndexError) as e:
        print(f"  [ERROR] {type(e).__name__}: {e}")
        return None


def main(nicho: str | None = None):
    if not ANTHROPIC_API_KEY:
        sys.exit("Falta ANTHROPIC_API_KEY en .env (console.anthropic.com -> API Keys)")
    init_db()
    if nicho is None:
        nicho = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if nicho:
        nicho_config(nicho)

    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pendientes = [l for l in leads_por_estado("auditado", nicho=nicho)
                  if l.get("email")]
    print(f"Leads pendientes de redacción: {len(pendientes)}")

    for i, lead in enumerate(pendientes, 1):
        datos = redactar(cliente, lead)
        if not datos:
            print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> FALLO, reintenta luego")
            continue
        baja_url = f"{BASE_URL}/baja/{lead['token_baja']}"
        cuerpo = datos["cuerpo"].rstrip() + PIE_LEGAL.format(
            remitente=REMITENTE_NOMBRE, empresa=EMPRESA_LEGAL, baja_url=baja_url)
        actualizar_lead(lead["id"],
                        email_asunto=datos["asunto"][:120],
                        email_cuerpo=cuerpo,
                        estado="redactado")
        print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> \"{datos['asunto']}\"")
        time.sleep(0.5)

    print("\nResumen:", stats(nicho=nicho) if nicho else stats())


if __name__ == "__main__":
    main()
