from __future__ import annotations
"""KD Radar v3.2 — Paso 4: Emails de marca completos (Claude + plantilla premium).

Cada email incluye:
  - Cabecera navy con el logo oficial de Ktys & Davids
  - Cuerpo personalizado redactado por Claude (datos reales del negocio)
  - Caja dorada de regalo: ANÁLISIS DIGITAL GRATIS (ya hecho por el sistema)
  - Tarjetas de producto con imagen real (Nora siempre; Qena en restaurantes,
    oferta adaptada en el resto de nichos)
  - Bloque del fundador con foto de David (trato directo, instalación 72h)
  - Botones CTA: responder / WhatsApp / agendar llamada
  - Firma + pie legal LSSI con baja en un clic

Se generan dos versiones: email_cuerpo (texto plano) y email_html (marca).

Uso:
    python3 4_generar_emails.py            # todos los nichos pendientes
    python3 4_generar_emails.py talleres   # solo un nicho
"""
import html as html_lib
import json
import sys
import time

import httpx

from config import (ANTHROPIC_API_KEY, BASE_URL, REMITENTE_NOMBRE,
                    REMITENTE_EMAIL, EMPRESA_LEGAL, NICHOS, nicho_config)
from db import init_db, leads_por_estado, actualizar_lead, cargar_json, stats

MODELO = "claude-sonnet-4-6"

# --- Recursos oficiales (CDN de ktysdavids.com) ---
IMG_LOGO = ("https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/"
            "6928305ea0e60a4050067585_Logo-normal.webp")
IMG_NORA = ("https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/"
            "6a25dc909401d162eb8e0efc_Nora%20Bot%20portada.webp")
IMG_QENA = ("https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/"
            "6a39496e4c4c7fcc9754ca09_photo_2026-06-22%2016.40.40.webp")
IMG_DAVID = ("https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/"
             "69831408570ff5e0dd8a8a1a_Foto%20Whatapps.webp")
URL_WHATSAPP = ("https://wa.me/34624577459?text=Hola%20David%2C%20he%20"
                "recibido%20tu%20email%20y%20quiero%20mi%20an%C3%A1lisis%20digital")
URL_CALENDLY = "https://calendly.com/ktysdavids-info-bjqc/30min"
URL_NORA = "https://www.ktysdavids.com/bot-nora-demo"
URL_QENA = "https://www.ktysdavids.com/qena"

SYSTEM_BASE = """Eres el mejor copywriter de ventas B2B de España, escribiendo
para Ktys & Davids, agencia de tecnología e IA de David Amundarain
(ktysdavids.com), con productos en producción en negocios reales de la
Comunidad Valenciana.

Sector del destinatario: {sector}.
Productos para este sector: {productos}.
Dolor principal del sector: {dolor}.

CONTEXTO DEL EMAIL: tu texto irá dentro de una plantilla de marca que ya
incluye: presentación visual de la empresa, una caja destacada ofreciendo un
ANÁLISIS DIGITAL GRATUITO de su negocio (ya realizado por nuestro sistema),
tarjetas de los productos con imagen, y la presentación del fundador.
Por tanto, tu texto NO debe presentar la empresa ni listar productos: debe
ser la parte HUMANA y PERSONALIZADA.

Reglas del texto:
- Español de España, directo y cercano, cero humo corporativo.
- 90-120 palabras, párrafos de 1-3 frases separados por línea en blanco.
- Empieza con "Hola," seguido del gancho: UN dato concreto y real de SU
  negocio (rating, reseñas, algo de su web) que demuestre que lo hemos mirado.
- Menciona que hemos hecho un primer análisis digital de su negocio y 1-2
  hallazgos como problema de negocio (tiempo o dinero que pierden).
- Cierra invitando a RESPONDER a este email para recibir el análisis completo
  gratis o cuadrar una llamada de 10 minutos esta semana.
- Sin enlaces, sin mayúsculas agresivas, sin palabras spam.
- Asunto: máximo 8 palabras, específico y con el nombre del negocio si cabe.

Responde SOLO con JSON válido, sin markdown ni texto extra:
{{"asunto": "...", "cuerpo": "..."}}"""

PIE_LEGAL_TEXTO = """

--
{remitente}
{empresa} · ktysdavids.com
Recibes este email como comunicación comercial B2B dirigida al buzón público de tu negocio.
Si no quieres recibir más emails, date de baja aquí (un clic): {baja_url}"""

# Segunda tarjeta de producto según nicho (Nora va siempre)
TARJETA2_POR_NICHO = {
    "restaurantes": {
        "img": IMG_QENA, "url": URL_QENA, "titulo": "Qena — Tu restaurante en un QR",
        "texto": ("El cliente escanea, ve la carta, pide y paga desde su móvil. "
                  "Sin comisiones de plataformas: el margen se queda en tu casa."),
    },
}
TARJETA2_DEFECTO = {
    "img": None, "url": "https://www.ktysdavids.com",
    "titulo": "Software a tu medida — CRM, citas y WhatsApp",
    "texto": ("Citas automáticas por WhatsApp con recordatorios (adiós no-shows), "
              "CRM con ficha de cliente y web que vende. Instalado y funcionando "
              "en 72 horas."),
}

PLANTILLA_HTML = """<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4eede;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4eede;">
<tr><td align="center" style="padding:28px 14px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background-color:#ffffff;border-radius:12px;overflow:hidden;">

  <!-- CABECERA NAVY CON LOGO -->
  <tr><td style="background-color:#0A1628;padding:22px 36px;" align="left">
    <img src="{img_logo}" alt="Ktys &amp; Davids" width="150" style="display:block;border:0;max-width:150px;">
  </td></tr>
  <tr><td style="height:4px;background-color:#D4AF37;font-size:0;line-height:0;">&nbsp;</td></tr>

  <!-- CUERPO PERSONALIZADO -->
  <tr><td style="padding:28px 36px 6px 36px;font-family:Georgia,'Times New Roman',serif;font-size:16px;line-height:1.7;color:#1a2332;">
{parrafos}
  </td></tr>

  <!-- CAJA REGALO: ANALISIS GRATIS -->
  <tr><td style="padding:10px 36px 6px 36px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A1628;border-radius:10px;">
      <tr><td style="padding:20px 24px;font-family:Georgia,serif;">
        <span style="color:#D4AF37;font-size:12px;letter-spacing:2px;font-weight:bold;">REGALO &middot; SIN COMPROMISO</span><br>
        <span style="color:#ffffff;font-size:18px;font-weight:bold;line-height:1.4;">An&aacute;lisis digital completo de {negocio} &mdash; GRATIS</span><br>
        <span style="color:#c9cdd6;font-size:14px;line-height:1.6;">Ya est&aacute; hecho: pulsa el bot&oacute;n y ve ahora mismo tu informe con lo que est&aacute; frenando tu negocio y c&oacute;mo resolverlo. Sin coste, sin registro, sin letra peque&ntilde;a.</span>
      </td></tr>
    </table>
  </td></tr>

  <!-- BOTONES CTA -->
  <tr><td style="padding:16px 36px 8px 36px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="background-color:#D4AF37;border-radius:8px;" align="center">
        <a href="{url_informe}" style="display:inline-block;padding:13px 26px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#0A1628;text-decoration:none;">Ver mi an&aacute;lisis gratis ahora</a>
      </td>
      <td style="width:12px;font-size:0;">&nbsp;</td>
      <td style="border:2px solid #0A1628;border-radius:8px;" align="center">
        <a href="{url_whatsapp}" style="display:inline-block;padding:11px 22px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#0A1628;text-decoration:none;">WhatsApp directo</a>
      </td>
    </tr></table>
    <div style="font-family:Arial,sans-serif;font-size:12px;color:#8a8577;padding-top:10px;">
      O si lo prefieres, <a href="{url_calendly}" style="color:#b8912e;">agenda una llamada de 10 minutos aqu&iacute;</a>.
    </div>
  </td></tr>

  <!-- SEPARADOR -->
  <tr><td style="padding:18px 36px 6px 36px;">
    <div style="border-top:1px solid #e8e2d4;font-size:0;">&nbsp;</div>
    <div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:2px;color:#b8912e;font-weight:bold;">LO QUE CONSTRUIMOS</div>
  </td></tr>

  <!-- TARJETA NORA -->
  <tr><td style="padding:12px 36px 0 36px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#faf7ef;border-radius:10px;">
      <tr>
        <td width="120" style="padding:14px 0 14px 16px;" valign="top">
          <a href="{url_nora}"><img src="{img_nora}" alt="Nora" width="110" style="display:block;border:0;border-radius:8px;max-width:110px;"></a>
        </td>
        <td style="padding:14px 18px;font-family:Georgia,serif;" valign="top">
          <span style="font-size:16px;font-weight:bold;color:#0A1628;">Nora &mdash; Recepcionista con IA</span><br>
          <span style="font-size:13.5px;color:#4a5261;line-height:1.55;">Contesta tu tel&eacute;fono 24/7 con voz natural: atiende llamadas, toma pedidos y reservas, y no deja escapar ni un cliente. Ya funciona en restaurantes reales de la zona.</span><br>
          <a href="{url_nora}" style="font-family:Arial,sans-serif;font-size:13px;color:#b8912e;font-weight:bold;text-decoration:none;">&#9654; Escuchar demo de Nora</a>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- TARJETA 2 (segun nicho) -->
  <tr><td style="padding:10px 36px 4px 36px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#faf7ef;border-radius:10px;">
      <tr>
        {celda_img2}
        <td style="padding:14px 18px;font-family:Georgia,serif;" valign="top">
          <span style="font-size:16px;font-weight:bold;color:#0A1628;">{t2_titulo}</span><br>
          <span style="font-size:13.5px;color:#4a5261;line-height:1.55;">{t2_texto}</span><br>
          <a href="{t2_url}" style="font-family:Arial,sans-serif;font-size:13px;color:#b8912e;font-weight:bold;text-decoration:none;">Ver m&aacute;s &rarr;</a>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- FUNDADOR -->
  <tr><td style="padding:18px 36px 24px 36px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="72" valign="top">
          <img src="{img_david}" alt="David Amundarain" width="64" height="64" style="display:block;border:0;border-radius:50%;">
        </td>
        <td style="padding-left:14px;font-family:Georgia,serif;" valign="top">
          <span style="font-size:15px;font-weight:bold;color:#0A1628;">David A. Amundara&iacute;n</span>
          <span style="font-size:12px;color:#8a8577;"> &middot; CEO &amp; Fundador</span><br>
          <span style="font-size:13px;color:#4a5261;line-height:1.55;">Desarrollador y M&aacute;ster Ingeniero en IA. Hablas directamente conmigo, sin comerciales: yo dise&ntilde;o, construyo e instalo tu sistema &mdash; funcionando en 72 horas.</span><br>
          <a href="https://ktysdavids.com" style="font-family:Arial,sans-serif;font-size:12.5px;color:#b8912e;text-decoration:none;">ktysdavids.com</a>
        </td>
      </tr>
    </table>
  </td></tr>

</table>

<!-- PIE LEGAL -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
  <tr><td style="padding:16px 12px;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.5;color:#8a8577;text-align:center;">
    {empresa_legal}<br>
    Recibes este email como comunicaci&oacute;n comercial B2B dirigida al buz&oacute;n p&uacute;blico de tu negocio.<br>
    <a href="{baja_url}" style="color:#8a8577;">Darse de baja (un clic)</a>
  </td></tr>
</table>
<img src="{url_pixel}" width="1" height="1" alt="" style="display:block;border:0;">
</td></tr>
</table>
</body>
</html>"""


def construir_html(cuerpo: str, baja_url: str, nicho: str, negocio: str,
                   url_informe: str, url_pixel: str) -> str:
    parrafos_html = []
    for parrafo in cuerpo.split("\n\n"):
        parrafo = parrafo.strip()
        if not parrafo:
            continue
        seguro = html_lib.escape(parrafo).replace("\n", "<br>")
        parrafos_html.append(f'    <p style="margin:0 0 15px 0;">{seguro}</p>')

    t2 = TARJETA2_POR_NICHO.get(nicho, TARJETA2_DEFECTO)
    if t2["img"]:
        celda_img2 = (f'<td width="120" style="padding:14px 0 14px 16px;" valign="top">'
                      f'<a href="{t2["url"]}"><img src="{t2["img"]}" alt="" width="110" '
                      f'style="display:block;border:0;border-radius:8px;max-width:110px;"></a></td>')
    else:
        celda_img2 = ('<td width="16" style="font-size:0;">&nbsp;</td>')

    return PLANTILLA_HTML.format(
        img_logo=IMG_LOGO, img_nora=IMG_NORA, img_david=IMG_DAVID,
        url_nora=URL_NORA, url_whatsapp=URL_WHATSAPP, url_calendly=URL_CALENDLY,
        parrafos="\n".join(parrafos_html),
        negocio=html_lib.escape(negocio),
        celda_img2=celda_img2,
        t2_titulo=html_lib.escape(t2["titulo"]),
        t2_texto=html_lib.escape(t2["texto"]),
        t2_url=t2["url"],
        url_informe=url_informe,
        url_pixel=url_pixel,
        remitente_email=REMITENTE_EMAIL,
        empresa_legal=html_lib.escape(EMPRESA_LEGAL),
        baja_url=baja_url,
    )


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def redactar(lead: dict) -> dict | None:
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
    payload = {
        "model": MODELO,
        "max_tokens": 700,
        "system": system,
        "messages": [{
            "role": "user",
            "content": ("Redacta el email para este negocio. "
                        "Datos reales de la auditoría:\n"
                        + json.dumps(contexto, ensure_ascii=False, indent=2)),
        }],
    }
    headers = {
        "x-api-key": (ANTHROPIC_API_KEY or "").strip().replace("\n", "").replace("\r", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # Reintentos con espera creciente por si la conexión es intermitente
    for intento in range(3):
        try:
            with httpx.Client(timeout=45) as cliente:
                r = cliente.post(ANTHROPIC_URL, headers=headers, json=payload)
            if r.status_code == 200:
                texto = r.json()["content"][0]["text"].strip()
                if texto.startswith("```"):
                    texto = texto.strip("`").removeprefix("json").strip()
                datos = json.loads(texto)
                if datos.get("asunto") and datos.get("cuerpo"):
                    return datos
                return None
            if r.status_code in (429, 529) or r.status_code >= 500:
                time.sleep(2 * (intento + 1))  # sobrecarga: reintenta
                continue
            print(f"  [ERROR HTTP {r.status_code}] {r.text[:160]}")
            return None
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  [ERROR {type(e).__name__}: {e}] intento {intento + 1}/3")
            time.sleep(2 * (intento + 1))
    return None


def main(nicho: str | None = None, desde_cli: bool = False):
    if not ANTHROPIC_API_KEY:
        print("[REDACCION] Falta ANTHROPIC_API_KEY; no se puede redactar")
        return
    init_db()
    # Solo mirar argumentos de línea de comandos si se ejecuta desde terminal,
    # NUNCA cuando el servidor llama a esta función (sys.argv sería 'servidor:app')
    if nicho is None and desde_cli:
        nicho = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if nicho:
        nicho_config(nicho)

    cliente_dummy = None
    pendientes = [l for l in leads_por_estado("auditado", nicho=nicho)
                  if l.get("email")]
    print(f"Leads pendientes de redacción: {len(pendientes)}")

    for i, lead in enumerate(pendientes, 1):
        datos = redactar(lead)
        if not datos:
            print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> FALLO, reintenta luego")
            continue
        baja_url = f"{BASE_URL}/baja/{lead['token_baja']}"
        cuerpo_texto = datos["cuerpo"].rstrip() + PIE_LEGAL_TEXTO.format(
            remitente=REMITENTE_NOMBRE, empresa=EMPRESA_LEGAL, baja_url=baja_url)
        url_informe = f"{BASE_URL}/informe/{lead['token_baja']}"
        url_pixel = f"{BASE_URL}/px/{lead['token_baja']}.gif"
        cuerpo_html = construir_html(datos["cuerpo"], baja_url,
                                     lead.get("nicho") or "restaurantes",
                                     lead["nombre"], url_informe, url_pixel)
        actualizar_lead(lead["id"],
                        email_asunto=datos["asunto"][:120],
                        email_cuerpo=cuerpo_texto,
                        email_html=cuerpo_html,
                        estado="redactado")
        print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> \"{datos['asunto']}\"")
        time.sleep(0.5)

    print("\nResumen:", stats(nicho=nicho) if nicho else stats())


if __name__ == "__main__":
    main(desde_cli=True)
