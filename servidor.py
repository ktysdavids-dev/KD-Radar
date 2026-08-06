from __future__ import annotations
# VERSION_KDRADAR = "v4-2027-01-CRM-completo"  <-- si ves esta linea, es el codigo NUEVO
"""KD Radar v3 — Servidor API (FastAPI) para Railway + n8n.

Endpoints (auth por cabecera X-API-Key, salvo /baja y /salud):
  POST /api/prospectar        -> lanza la captación automática en segundo plano
                                 (rotación de nichos/municipios). Para n8n diario.
  GET  /api/prospectar/estado -> estado del motor (ocupado, última ejecución)
  GET  /api/lote              -> lote diario listo para enviar (para n8n)
  POST /api/enviado/{id}      -> n8n marca un lead como enviado
  GET  /api/stats             -> métricas del embudo (global y por nicho)
  GET  /baja/{token}          -> baja voluntaria LSSI (público)
  GET  /salud                 -> healthcheck Railway (público)

Variables de entorno en Railway:
  RADAR_API_KEY  (obligatoria: protege la API; ponla también en n8n)
  DB_PATH=/data/kd_radar.db  (con un volumen montado en /data)
  GOOGLE_PLACES_API_KEY, ANTHROPIC_API_KEY, BASE_URL, LOTE_DIARIO...

Arranque Railway: uvicorn servidor:app --host 0.0.0.0 --port $PORT
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response

from config import (LOTE_DIARIO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
                    REMITENTE_NOMBRE, REMITENTE_EMAIL, BASE_URL)
from db import (init_db, conexion, lote_para_envio, actualizar_lead,
                excluir_email, stats, stats_por_nicho, ahora)
from motor import ejecutar_prospeccion, estado_motor, siguientes_combos

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")

app = FastAPI(title="KD Radar", docs_url=None, redoc_url=None)
init_db()


def verificar(x_api_key: str | None):
    """Auth simple por cabecera. Si RADAR_API_KEY no está definida (desarrollo
    local), no exige clave pero avisa por consola."""
    if not RADAR_API_KEY:
        print("[AVISO] RADAR_API_KEY no definida: API sin protección "
              "(solo aceptable en local)")
        return
    if x_api_key != RADAR_API_KEY:
        raise HTTPException(401, "X-API-Key inválida o ausente")


@app.get("/salud")
def salud():
    return {"ok": True}


@app.post("/api/prospectar")
def api_prospectar(tareas: BackgroundTasks,
                   municipios: int = 5,
                   x_api_key: str | None = Header(default=None)):
    """Lanza la prospección automática en segundo plano y responde al instante
    (la captación tarda de minutos a horas; n8n no debe esperarla)."""
    verificar(x_api_key)
    if estado_motor["ocupado"]:
        raise HTTPException(409, "Ya hay una prospección en curso")
    municipios = max(1, min(municipios, 15))
    proximos = siguientes_combos(municipios)
    if not proximos:
        return {"ok": True, "mensaje": "Todo prospectado. Añade nichos en config.py."}
    tareas.add_task(ejecutar_prospeccion, municipios)
    return {"ok": True,
            "lanzado": True,
            "nicho": proximos[0][0],
            "municipios": [m for _, m, _ in proximos],
            "nota": "Ejecutando en segundo plano. Consulta /api/prospectar/estado."}


@app.get("/api/prospectar/estado")
def api_prospectar_estado(x_api_key: str | None = Header(default=None)):
    verificar(x_api_key)
    return estado_motor


@app.get("/api/stats")
def api_stats(x_api_key: str | None = Header(default=None)):
    verificar(x_api_key)
    return {"global": stats(), "por_nicho": stats_por_nicho()}


@app.get("/api/lote")
def api_lote(limite: int = LOTE_DIARIO,
             x_api_key: str | None = Header(default=None)):
    verificar(x_api_key)
    limite = max(1, min(limite, 100))
    leads = lote_para_envio(limite)
    return [
        {
            "id": l["id"],
            "nombre": l["nombre"],
            "nicho": l.get("nicho"),
            "municipio": l["municipio"],
            "email": l["email"],
            "email_asunto": l["email_asunto"],
            "email_cuerpo": l["email_cuerpo"],
            "email_html": l.get("email_html"),
        }
        for l in leads
    ]


@app.post("/api/enviado/{lead_id}")
def api_enviado(lead_id: int, x_api_key: str | None = Header(default=None)):
    verificar(x_api_key)
    with conexion() as con:
        fila = con.execute("SELECT id, estado FROM leads WHERE id = ?",
                           (lead_id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Lead no encontrado")
    if fila["estado"] == "excluido":
        raise HTTPException(409, "Lead excluido: no marcar como enviado")
    actualizar_lead(lead_id, estado="enviado")
    with conexion() as con:
        asunto = con.execute("SELECT email_asunto FROM leads WHERE id=?",
                             (lead_id,)).fetchone()["email_asunto"]
        con.execute("INSERT INTO envios (lead_id, asunto, fecha) VALUES (?,?,?)",
                    (lead_id, asunto, ahora()))
    return {"ok": True, "id": lead_id, "fecha": ahora()}


# Catálogo: cada dolor detectable -> solución concreta + producto + precio base.
# El precio base es el de mercado; en el informe se aplica -40% (David no tiene
# oficina, ni empleados, ni infraestructura -> precios imbatibles).
SOLUCIONES = {
    "sin_pedido_propio": {
        "dolor": "Los pedidos entran solo por teléfono o dependéis de plataformas que se quedan hasta un 30% de comisión",
        "solucion": "Nora (recepcionista IA) contesta y toma pedidos 24/7, y Qena os da carta QR con pedido propio sin comisiones",
        "producto": "Nora + Qena", "precio": 890},
    "sin_citas_online": {
        "dolor": "No tenéis sistema de reservas/citas online: cada llamada perdida en hora punta es un cliente que se va a la competencia",
        "solucion": "Nora atiende el teléfono con voz natural y agenda las citas/reservas sola, sin que soltéis lo que estáis haciendo",
        "producto": "Nora", "precio": 690},
    "sin_whatsapp": {
        "dolor": "Sin WhatsApp automatizado: los no-shows (citas que no aparecen) os cuestan dinero cada semana",
        "solucion": "Qenda envía recordatorios automáticos por WhatsApp y reduce drásticamente las citas perdidas",
        "producto": "Qenda", "precio": 390},
    "web_lenta": {
        "dolor": "Vuestra web tarda demasiado en cargar: cada segundo de más son clientes que la cierran antes de veros",
        "solucion": "Web rápida optimizada, carga en menos de 2 segundos y preparada para convertir visitas en clientes",
        "producto": "Web KD", "precio": 490},
    "sin_movil": {
        "dolor": "La web no está bien optimizada para móvil, y ahí es donde os busca el 80% de vuestros clientes",
        "solucion": "Rediseño responsive que se ve perfecto en el móvil y facilita que os llamen o reserven",
        "producto": "Web KD", "precio": 490},
    "sin_https_seo": {
        "dolor": "Web sin HTTPS o sin SEO básico: Google os penaliza y perdéis visibilidad frente a competidores de la zona",
        "solucion": "Certificado seguro + SEO local para que aparezcáis cuando os buscan en Google Maps y búsquedas cercanas",
        "producto": "Web KD + SEO", "precio": 340},
    "sin_web": {
        "dolor": "No tenéis web propia o no responde: perdéis a todos los clientes que os buscan online cada día",
        "solucion": "Web profesional con reservas, pedido y WhatsApp integrado, lista y funcionando en 72 horas",
        "producto": "Web KD completa", "precio": 690},
}

# Mapea las claves de la auditoría a las soluciones del catálogo
def _dolores_del_lead(auditoria: dict, lead: dict) -> list[dict]:
    """Devuelve la lista de soluciones aplicables según lo detectado en la web."""
    a = auditoria or {}
    items: list[dict] = []
    usados = set()

    def add(clave):
        if clave not in usados and clave in SOLUCIONES:
            items.append(SOLUCIONES[clave]); usados.add(clave)

    if a.get("sin_web") or not a.get("web_activa"):
        add("sin_web")
    else:
        delivery = a.get("depende_plataformas_delivery")
        if (lead.get("nicho") == "restaurantes"
                and (delivery or not a.get("tiene_pedido_online_propio"))):
            add("sin_pedido_propio")
        if not a.get("tiene_citas_online"):
            add("sin_citas_online")
        if not a.get("tiene_whatsapp"):
            add("sin_whatsapp")
        if a.get("tiempo_carga_s", 0) > 4:
            add("web_lenta")
        if not a.get("movil_optimizada"):
            add("sin_movil")
        if not a.get("https") or not a.get("tiene_titulo_seo") or not a.get("tiene_meta_descripcion"):
            add("sin_https_seo")

    # Si la web está tan bien que no detectó nada, ofrecer mejora de conversión
    if not items:
        add("sin_citas_online")
    return items[:4]


NICHO_OFERTA = {
    "restaurantes": ["Nora contesta el teléfono 24/7 y toma pedidos y reservas",
                     "Qena: carta QR y pedidos propios sin comisiones de plataformas"],
}
OFERTA_DEFECTO = ["Nora contesta el teléfono 24/7 y da citas con voz natural",
                  "Citas y recordatorios automáticos por WhatsApp (adiós no-shows)",
                  "CRM con ficha de cliente, instalado y funcionando en 72 horas"]

CHECKS_INFORME = [
    ("web_activa", "Web activa y accesible"),
    ("https", "Conexión segura (HTTPS)"),
    ("movil_optimizada", "Optimizada para móvil"),
    ("tiene_citas_online", "Reservas / citas online"),
    ("tiene_whatsapp", "Canal de WhatsApp en la web"),
    ("tiene_pedido_online_propio", "Pedidos online propios (sin comisiones)"),
    ("tiene_instagram", "Instagram enlazado"),
    ("tiene_titulo_seo", "SEO básico (título)"),
    ("tiene_meta_descripcion", "SEO básico (descripción)"),
]




@app.get("/informe/{token}", response_class=HTMLResponse)
def informe(token: str):
    """Informe de análisis digital personalizado (público). Marca el lead como
    caliente al abrirse. Incluye puntos de dolor reales, solución por cada uno,
    presupuesto estimado con 40% de descuento y botón de descarga PDF."""
    from db import cargar_json
    from urllib.parse import quote
    with conexion() as con:
        fila = con.execute(
            "SELECT * FROM leads WHERE token_baja = ?", (token,)).fetchone()
    if not fila:
        raise HTTPException(404, "Informe no encontrado")
    lead = dict(fila)
    if not lead.get("visito_informe"):
        actualizar_lead(lead["id"], visito_informe=ahora())

    auditoria = cargar_json(lead.get("auditoria")) or {}
    items = _dolores_del_lead(auditoria, lead)
    nombre = lead["nombre"]
    municipio = lead.get("municipio") or ""

    base_total = sum(it["precio"] for it in items) or 690
    con_dto = round(base_total * 0.60)
    est_min = int(round(con_dto * 0.85 / 10) * 10)
    est_max = int(round(con_dto * 1.15 / 10) * 10)

    bloques = ""
    for i, it in enumerate(items, 1):
        precio_dto = round(it["precio"] * 0.60)
        bloques += (
            '<div style="background:#faf7ef;border-radius:12px;padding:18px 20px;'
            'margin-bottom:14px;border-left:4px solid #D4AF37;">'
            '<div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:1px;'
            'color:#c0392b;font-weight:bold;">&#9888; PROBLEMA ' + str(i) + '</div>'
            '<p style="color:#1a2332;font-size:15px;line-height:1.5;margin:6px 0 12px 0;'
            'font-weight:bold;">' + it['dolor'] + '</p>'
            '<div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:1px;'
            'color:#3fae6a;font-weight:bold;">&#10003; C&Oacute;MO LO RESOLVEMOS</div>'
            '<p style="color:#4a5261;font-size:14.5px;line-height:1.55;margin:6px 0 0 0;">'
            + it['solucion'] + '</p>'
            '<div style="margin-top:12px;padding-top:12px;border-top:1px dashed #e0d9c6;'
            'font-family:Arial,sans-serif;font-size:13px;color:#8a8577;">'
            + it['producto'] + ' &middot; <span style="text-decoration:line-through;">'
            + str(it['precio']) + '&euro;</span> <span style="color:#0A1628;'
            'font-weight:bold;">desde ' + str(precio_dto) + '&euro;</span> '
            '<span style="color:#b8912e;">(-40%)</span></div></div>')

    filas_check = ""
    for clave, etiqueta in CHECKS_INFORME:
        if clave not in auditoria:
            continue
        ok = bool(auditoria.get(clave))
        icono = "&#10003;" if ok else "&#10007;"
        color = "#3fae6a" if ok else "#c0392b"
        filas_check += ('<tr><td style="padding:8px 12px;color:' + color +
                        ';font-weight:bold;width:26px;">' + icono + '</td>'
                        '<td style="padding:8px 0;color:#1a2332;font-size:14.5px;">'
                        + etiqueta + '</td></tr>')
    seccion_estado = ('<h2 style="color:#0A1628;font-size:18px;border-bottom:1px solid '
                      '#e8e2d4;padding-bottom:6px;margin-top:8px;">Estado actual de tu '
                      'presencia digital</h2><table style="width:100%;border-collapse:'
                      'collapse;">' + filas_check + '</table>') if filas_check else ""

    wa_txt = quote("Hola David, he visto el análisis de " + nombre + " y quiero que hablemos")
    url_wa = "https://wa.me/34624577459?text=" + wa_txt
    url_pdf = BASE_URL + "/informe/" + token + "/pdf"
    url_cal = "https://calendly.com/ktysdavids-info-bjqc/30min"

    return """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>An&aacute;lisis digital &middot; """ + nombre + """</title></head>
<body style="margin:0;background:#f4eede;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:660px;margin:0 auto;padding:22px 14px;">
  <div style="background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(10,22,40,.08);">
    <div style="background:#0A1628;padding:24px 32px;text-align:center;">
      <img src="https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/6928305ea0e60a4050067585_Logo-normal.webp" alt="Ktys &amp; Davids" width="150" style="display:block;margin:0 auto;">
    </div>
    <div style="height:4px;background:#D4AF37;"></div>
    <div style="padding:28px 30px;">
      <div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:2px;color:#b8912e;font-weight:bold;">AN&Aacute;LISIS DIGITAL GRATUITO</div>
      <h1 style="color:#0A1628;font-size:27px;margin:6px 0 2px 0;">""" + nombre + """</h1>
      <p style="color:#8a8577;font-family:Arial,sans-serif;font-size:13px;margin:0 0 22px 0;">""" + municipio + """ &middot; Informe preliminar &middot; Ktys &amp; Davids</p>

      """ + seccion_estado + """

      <h2 style="color:#0A1628;font-size:19px;border-bottom:1px solid #e8e2d4;padding-bottom:6px;margin-top:28px;">Lo que hemos encontrado (y c&oacute;mo lo arreglamos)</h2>
      <p style="color:#8a8577;font-size:14px;margin:8px 0 18px 0;">Estos """ + str(len(items)) + """ puntos son los que ahora mismo te est&aacute;n costando clientes:</p>
      """ + bloques + """

      <div style="background:#0A1628;border-radius:14px;padding:26px 28px;margin-top:26px;text-align:center;">
        <div style="color:#D4AF37;font-family:Arial,sans-serif;font-size:11px;letter-spacing:2px;font-weight:bold;">PRESUPUESTO ESTIMADO &middot; DESCUENTO 40% APLICADO</div>
        <div style="color:#ffffff;font-size:15px;margin:12px 0 4px 0;">Soluci&oacute;n completa para """ + nombre + """, estimado:</div>
        <div style="color:#D4AF37;font-family:Georgia,serif;font-size:38px;font-weight:bold;line-height:1.1;">""" + str(est_min) + """&euro; &ndash; """ + str(est_max) + """&euro;</div>
        <div style="color:#8a95a8;font-family:Arial,sans-serif;font-size:12px;margin:6px 0 0 0;text-decoration:line-through;">precio de mercado: """ + str(base_total) + """&euro;</div>
        <p style="color:#c9cdd6;font-size:13.5px;line-height:1.6;margin:16px 0 0 0;">Es una estimaci&oacute;n orientativa. El precio final lo cerramos juntos seg&uacute;n lo que de verdad necesites &mdash; sin pagar de m&aacute;s por cosas que no usas.</p>
      </div>

      <div style="background:#faf7ef;border-radius:12px;padding:20px 22px;margin-top:16px;">
        <div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:1px;color:#b8912e;font-weight:bold;">&iquest;POR QU&Eacute; PUEDO OFRECERTE ESTOS PRECIOS?</div>
        <p style="color:#4a5261;font-size:14.5px;line-height:1.6;margin:8px 0 0 0;">Trabajo desde mi ordenador, sin oficina, sin empleados y sin infraestructura que pagar. Eso significa que <strong>mis precios son siempre de los m&aacute;s econ&oacute;micos del mercado</strong> &mdash; te llega el ahorro directo a ti, con trato personal conmigo, el fundador.</p>
      </div>

      <div style="background:#f0ede3;border-radius:10px;padding:14px 18px;margin-top:16px;text-align:center;">
        <span style="font-family:Arial,sans-serif;font-size:13.5px;color:#0A1628;">&#9889; Instalado y funcionando en <strong>72 horas</strong> &middot; Trato directo con David, sin comerciales</span>
      </div>

      <div style="text-align:center;margin-top:28px;">
        <a href=\"""" + url_wa + """\" style="display:inline-block;background:#D4AF37;color:#0A1628;font-family:Arial,sans-serif;font-weight:bold;font-size:17px;padding:16px 32px;border-radius:10px;text-decoration:none;">Quiero hablar con David &#128172;</a>
        <div style="margin-top:14px;">
          <a href=\"""" + url_cal + """\" style="font-family:Arial,sans-serif;font-size:14px;color:#b8912e;text-decoration:none;">&#128197; O agenda una llamada de 10 min aqu&iacute;</a>
        </div>
        <div style="margin-top:18px;">
          <a href=\"""" + url_pdf + """\" target="_blank" style="font-family:Arial,sans-serif;font-size:13px;color:#8a8577;text-decoration:none;border:1px solid #d8d1c0;border-radius:8px;padding:9px 18px;display:inline-block;">&#128424; Imprimir o guardar como PDF</a>
        </div>
      </div>
    </div>
  </div>
  <p style="text-align:center;font-family:Arial,sans-serif;font-size:11px;color:#8a8577;margin-top:16px;">Ktys &amp; Davids Productions S.L. &middot; ktysdavids.com<br>Multiplicamos decisiones, no riesgos.</p>
</div>
</body></html>"""


@app.get("/api/lead/{lead_id}/ver-informe", response_class=HTMLResponse)
def api_ver_informe(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Muestra el informe de un lead para uso INTERNO (David, antes de llamar).
    NO marca el informe como visto: el chip 'vio informe' solo debe activarse
    cuando lo abre el cliente desde el email/WhatsApp, no cuando lo revisas tú."""
    verificar(x_api_key)
    with conexion() as con:
        fila = con.execute("SELECT token_baja, visito_informe FROM leads WHERE id=?",
                           (lead_id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Lead no encontrado")
    ya_visto = fila["visito_informe"]  # guardamos el estado previo
    resp = informe(fila["token_baja"])
    # Deshacer el marcado que hace informe(): si el cliente no lo había visto,
    # lo dejamos como estaba (sin marcar), porque esta apertura es tuya.
    if not ya_visto:
        actualizar_lead(lead_id, visito_informe=None)
    html = resp.body.decode("utf-8") if hasattr(resp, "body") else str(resp)
    # Banner discreto arriba avisando que es la vista interna
    banner = ('<div style="background:#0A1628;color:#D4AF37;text-align:center;'
              'padding:8px;font-family:Arial,sans-serif;font-size:12px;letter-spacing:1px;">'
              'VISTA INTERNA · no cuenta como visto por el cliente</div>')
    # Insertar el banner justo después de abrir el <body>
    idx = html.find(">", html.find("<body"))
    if idx != -1:
        html = html[:idx+1] + banner + html[idx+1:]
    return HTMLResponse(content=html)


@app.get("/informe/{token}/pdf", response_class=HTMLResponse)
def informe_pdf(token: str):
    """Versión imprimible del informe: abre el diálogo de impresión del
    navegador para que el usuario lo guarde como PDF. Funciona en cualquier
    dispositivo sin depender de librerías del servidor."""
    with conexion() as con:
        fila = con.execute("SELECT token_baja FROM leads WHERE token_baja=?",
                           (token,)).fetchone()
    if not fila:
        raise HTTPException(404, "Informe no encontrado")
    # Reutiliza el HTML del informe y le añade el auto-print
    resp = informe(token)
    html = resp.body.decode("utf-8") if hasattr(resp, "body") else str(resp)
    script_print = ("<script>window.onload=function(){"
                    "setTimeout(function(){window.print();},500);};</script>")
    html = html.replace("</body>", script_print + "</body>")
    return HTMLResponse(content=html)


# GIF transparente de 1x1 (tracking de aperturas de email)
_PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
              b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
              b"\x00\x00\x02\x02D\x01\x00;")


@app.get("/px/{token}.gif")
def pixel(token: str):
    """Marca la apertura del email (píxel invisible). Endpoint público."""
    with conexion() as con:
        fila = con.execute(
            "SELECT id, email_abierto FROM leads WHERE token_baja = ?",
            (token,)).fetchone()
    if fila and not fila["email_abierto"]:
        actualizar_lead(fila["id"], email_abierto=ahora())
    return Response(content=_PIXEL_GIF, media_type="image/gif",
                    headers={"Cache-Control": "no-store, max-age=0"})


ESTADOS_MANUALES = {"respondido", "cliente", "descartado", "redactado",
                    "enviado", "auditado", "sin_email"}


def _movil_es(telefono: str | None) -> str | None:
    """Devuelve 34XXXXXXXXX si es móvil español (6xx/7xx), si no None."""
    if not telefono:
        return None
    import re as _re
    d = _re.sub(r"\D", "", telefono)
    n = d[-9:]
    return "34" + n if n and n[0] in ("6", "7") else None


# Mensaje de WhatsApp por nicho (gancho corto + enlace al informe)
_WA_MENSAJE = {
    "restaurantes": ("Hola! Soy David, de Ktys & Davids 👋 He echado un vistazo a "
                     "{nombre} y os he preparado un análisis rápido de vuestra "
                     "presencia online. He visto un par de cosas que os están "
                     "costando clientes (sobre todo con las llamadas en hora punta). "
                     "Te lo dejo aquí, tardas 30 seg en verlo 👇\n{informe}\n\n"
                     "Trabajo con restaurantes de la zona automatizando pedidos y "
                     "reservas con IA. ¿Le echas un ojo y me dices?"),
    "barberias": ("Hola! Soy David, de Ktys & Davids 👋 Le he echado un vistazo a "
                  "{nombre} y os preparé un análisis rápido. Vi que se os pueden "
                  "estar escapando citas cuando estáis atendiendo. Míralo aquí, "
                  "son 30 seg 👇\n{informe}\n\nAutomatizo las citas por teléfono y "
                  "WhatsApp para barberías. ¿Te cuadra que hablemos?"),
    "estetica": ("Hola! Soy David, de Ktys & Davids 👋 Preparé un análisis rápido "
                 "de {nombre}. Vi cosas mejorables para que no se os escapen citas "
                 "ni reservas. Te lo dejo aquí 👇\n{informe}\n\nAutomatizo citas y "
                 "recordatorios por WhatsApp. ¿Le echas un ojo?"),
    "talleres": ("Hola! Soy David, de Ktys & Davids 👋 Le eché un vistazo a "
                 "{nombre} y os preparé un análisis. Vi que se pueden perder "
                 "llamadas de clientes mientras estáis en faena. Míralo 👇\n"
                 "{informe}\n\nAutomatizo la recepción de llamadas y citas para "
                 "talleres. ¿Hablamos?"),
}
_WA_DEFECTO = ("Hola! Soy David, de Ktys & Davids 👋 He preparado un análisis "
               "rápido de {nombre} con un par de mejoras para que no se os "
               "escapen clientes. Te lo dejo aquí, 30 seg 👇\n{informe}\n\n"
               "Automatizo la atención al cliente con IA. ¿Le echas un ojo?")


def _whatsapp_url(lead: dict) -> str | None:
    from urllib.parse import quote
    movil = _movil_es(lead.get("telefono"))
    if not movil:
        return None
    plantilla = _WA_MENSAJE.get(lead.get("nicho") or "", _WA_DEFECTO)
    informe = f"{BASE_URL}/informe/{lead.get('token_baja')}" if lead.get("token_baja") else ""
    mensaje = plantilla.format(nombre=lead.get("nombre") or "vuestro negocio",
                               informe=informe)
    return f"https://wa.me/{movil}?text={quote(mensaje)}"


@app.get("/api/leads")
def api_leads(x_api_key: str | None = Header(default=None)):
    """CRM: todos los leads con sus señales (enviado, abierto, informe, baja)."""
    verificar(x_api_key)
    with conexion() as con:
        filas = con.execute(
            """SELECT id, nombre, nicho, municipio, provincia, telefono, email,
                      web, rating, num_resenas, estado, email_abierto,
                      visito_informe, llamado, notas, recordatorio,
                      resultado_llamada, token_baja,
                      consentimiento, consent_fuente, cita_texto,
                      auditoria, pain_points, actualizado_en
               FROM leads ORDER BY
                 CASE WHEN visito_informe IS NOT NULL THEN 0
                      WHEN email_abierto IS NOT NULL THEN 1 ELSE 2 END,
                 actualizado_en DESC""").fetchall()
        resultado = []
        for f in filas:
            d = dict(f)
            d["whatsapp_url"] = _whatsapp_url(d)
            # ¿tiene informe? Sí si fue auditado (tiene auditoría o puntos de dolor).
            # Esto funciona para leads con o sin email (antes fallaba con solo tel).
            tiene_aud = bool((d.get("auditoria") or "").strip()
                             and (d.get("auditoria") or "").strip() not in ("null", "{}", "[]"))
            tiene_pain = bool((d.get("pain_points") or "").strip()
                              and (d.get("pain_points") or "").strip() not in ("null", "[]"))
            d["tiene_informe"] = tiene_aud or tiene_pain
            # No exponer los datos crudos de auditoría en el listado (pesan mucho)
            d.pop("auditoria", None)
            d.pop("pain_points", None)
            d.pop("token_baja", None)
            resultado.append(d)
        return resultado


@app.post("/api/lead/{lead_id}/estado/{nuevo}")
def api_cambiar_estado(lead_id: int, nuevo: str,
                       x_api_key: str | None = Header(default=None)):
    """CRM: marcar manualmente un lead (respondido / cliente / descartado)."""
    verificar(x_api_key)
    if nuevo not in ESTADOS_MANUALES:
        raise HTTPException(400, f"Estado no permitido. Usa: {sorted(ESTADOS_MANUALES)}")
    with conexion() as con:
        if not con.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(404, "Lead no encontrado")
    actualizar_lead(lead_id, estado=nuevo)
    return {"ok": True, "id": lead_id, "estado": nuevo}


@app.get("/api/interesados")
def api_interesados(x_api_key: str | None = Header(default=None)):
    """Leads calientes: han abierto su informe. Prioridad de llamada."""
    verificar(x_api_key)
    with conexion() as con:
        filas = con.execute(
            """SELECT id, nombre, nicho, municipio, telefono, email,
                      visito_informe, estado
               FROM leads WHERE visito_informe IS NOT NULL
               ORDER BY visito_informe DESC""").fetchall()
        return [dict(f) for f in filas]


@app.post("/api/lead/{lead_id}/toggle-cliente")
def api_toggle_cliente(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Marca como cliente, o si ya lo es, lo revierte a su estado natural
    (enviado si ya se le envió, o auditado/sin_email según tenga email)."""
    verificar(x_api_key)
    with conexion() as con:
        l = con.execute("SELECT estado, email, email_html FROM leads WHERE id=?",
                        (lead_id,)).fetchone()
    if not l:
        raise HTTPException(404, "Lead no encontrado")
    l = dict(l)
    if l["estado"] == "cliente":
        # Revertir: si tenía email redactado -> redactado; si email -> auditado; si no -> sin_email
        nuevo = ("redactado" if l.get("email_html")
                 else "auditado" if l.get("email") else "sin_email")
    else:
        nuevo = "cliente"
    actualizar_lead(lead_id, estado=nuevo)
    # Cliente = relación contractual: permiso de llamada automático + sync a Sonar
    if nuevo == "cliente":
        with conexion() as con:
            tel = (con.execute("SELECT telefono FROM leads WHERE id=?", (lead_id,))
                   .fetchone() or {"telefono": None})["telefono"]
        if tel:
            _conceder_permiso(lead_id, True, "cliente_actual")
            _sync_consent_sonar(tel, True, "cliente_actual")
    return {"ok": True, "estado": nuevo, "es_cliente": nuevo == "cliente"}


@app.get("/api/exportar.csv")
def api_exportar_csv(solo_contacto: int = 1,
                     x_api_key: str | None = Header(default=None)):
    """Descarga los leads en CSV (para Excel/Numbers). Por defecto solo los
    que tienen algún contacto (email o teléfono)."""
    verificar(x_api_key)
    import csv, io
    filtro = ("WHERE (email IS NOT NULL AND email!='') OR "
              "(telefono IS NOT NULL AND telefono!='')") if solo_contacto else ""
    with conexion() as con:
        filas = con.execute(
            f"""SELECT nombre, nicho, municipio, provincia, email, telefono,
                       web, rating, num_resenas, estado, llamado,
                       email_abierto, visito_informe, creado_en
                FROM leads {filtro} ORDER BY nicho, municipio, nombre""").fetchall()
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM para que Excel/Numbers lea bien los acentos
    w = csv.writer(buf)
    w.writerow(["Negocio", "Nicho", "Municipio", "Provincia", "Email",
                "Teléfono", "Web", "Rating", "Reseñas", "Estado",
                "Llamado", "Abrió email", "Vio informe", "Capturado"])
    for r in filas:
        r = dict(r)
        w.writerow([r["nombre"], r["nicho"], r["municipio"], r["provincia"],
                    r["email"] or "", r["telefono"] or "", r["web"] or "",
                    r["rating"] or "", r["num_resenas"] or "", r["estado"],
                    "Sí" if r["llamado"] else "", "Sí" if r["email_abierto"] else "",
                    "Sí" if r["visito_informe"] else "", (r["creado_en"] or "")[:10]])
    from fastapi.responses import Response as R
    return R(content=buf.getvalue(), media_type="text/csv",
             headers={"Content-Disposition": "attachment; filename=kd-radar-leads.csv"})


RESULTADOS_LLAMADA = {"", "contactado", "no_contesta", "volver_llamar",
                      "no_interesado", "cita_agendada", "buzon"}


@app.post("/api/lead/{lead_id}/auditar")
def api_auditar_lead(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Genera la auditoría y los puntos de dolor de un lead concreto.
    Si tiene web, la analiza; si no, genera dolores genéricos del nicho.
    Útil para leads añadidos a mano que no pasaron por el pipeline."""
    verificar(x_api_key)
    import importlib
    aud = importlib.import_module("3_auditar_digital")
    with conexion() as con:
        fila = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Lead no encontrado")
    lead = dict(fila)
    web = (lead.get("web") or "").strip()
    if web:
        a = aud.auditar_web(web)
    else:
        # Sin web: auditoría mínima que dispara los dolores genéricos del nicho
        a = {"web_activa": False, "sin_web": True}
    dolores = aud.detectar_pain_points(lead, a)
    import json as _json
    actualizar_lead(lead_id,
                    auditoria=_json.dumps(a, ensure_ascii=False),
                    pain_points=_json.dumps(dolores, ensure_ascii=False),
                    estado="auditado" if lead.get("email") else lead.get("estado"))
    return {"ok": True, "id": lead_id, "pain_points": len(dolores),
            "tiene_web": bool(web),
            "mensaje": f"Auditoría lista: {len(dolores)} puntos de dolor detectados"}


@app.post("/api/lead/{lead_id}/gestion")
def api_gestion(lead_id: int, datos: dict,
                x_api_key: str | None = Header(default=None)):
    """Guarda la gestión comercial de un lead: notas, resultado de la llamada,
    y recordatorio (fecha/hora para volver a llamar). Todo lo que necesitas
    para no perder el hilo con 100 llamadas al día."""
    verificar(x_api_key)
    with conexion() as con:
        if not con.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(404, "Lead no encontrado")
    campos = {}
    if "notas" in datos:
        campos["notas"] = (datos["notas"] or "").strip() or None
    if "resultado" in datos:
        res = (datos["resultado"] or "").strip()
        if res not in RESULTADOS_LLAMADA:
            raise HTTPException(400, f"Resultado no válido: {sorted(RESULTADOS_LLAMADA)}")
        campos["resultado_llamada"] = res or None
        # Marcar como llamado automáticamente si hay un resultado
        if res:
            campos["llamado"] = ahora()
    if "recordatorio" in datos:
        # Espera ISO 'YYYY-MM-DDTHH:MM' o vacío para quitarlo
        campos["recordatorio"] = (datos["recordatorio"] or "").strip() or None
    if "cita_texto" in datos:
        campos["cita_texto"] = (datos["cita_texto"] or "").strip() or None
    if "marcar_llamado" in datos:
        campos["llamado"] = ahora() if datos["marcar_llamado"] else None
    if not campos:
        raise HTTPException(400, "Nada que guardar")
    actualizar_lead(lead_id, **campos)
    return {"ok": True, "id": lead_id}


@app.post("/api/lead/{lead_id}/editar")
def api_editar_lead(lead_id: int, datos: dict,
                    x_api_key: str | None = Header(default=None)):
    """Edita los datos de un lead (nombre, email, teléfono, municipio, nicho, web)."""
    verificar(x_api_key)
    with conexion() as con:
        if not con.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(404, "Lead no encontrado")
    campos = {}
    for k in ("nombre", "email", "telefono", "municipio", "nicho", "web"):
        if k in datos:
            v = (datos[k] or "").strip()
            campos[k] = v or None
    if "email" in campos and campos["email"]:
        campos["email"] = campos["email"].lower()
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    actualizar_lead(lead_id, **campos)
    return {"ok": True, "id": lead_id}


@app.post("/api/lead/{lead_id}/enviar")
def api_enviar_directo(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Envía AHORA el email a un lead concreto (botón del panel).
    El lead debe tener email y estar redactado (con email_html/cuerpo listo)."""
    verificar(x_api_key)
    if not SMTP_PASS:
        raise HTTPException(400, "Falta configurar SMTP_PASS en Railway (contraseña del buzón IONOS)")
    with conexion() as con:
        l = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not l:
        raise HTTPException(404, "Lead no encontrado")
    lead = dict(l)
    if not lead.get("email"):
        raise HTTPException(400, "Este lead no tiene email")
    if lead.get("estado") == "excluido":
        raise HTTPException(409, "Lead dado de baja")
    cuerpo_html = lead.get("email_html")
    asunto = lead.get("email_asunto")
    if not cuerpo_html or not asunto:
        raise HTTPException(400, "Este email aún no está redactado. Pulsa 'Redactar ahora' primero.")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"] = f"{REMITENTE_NOMBRE} <{REMITENTE_EMAIL}>"
        msg["To"] = lead["email"]
        msg["Reply-To"] = REMITENTE_EMAIL
        if lead.get("email_cuerpo"):
            msg.attach(MIMEText(lead["email_cuerpo"], "plain", "utf-8"))
        msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        raise HTTPException(502, f"Error enviando: {type(e).__name__}: {e}")
    actualizar_lead(lead_id, estado="enviado")
    with conexion() as con:
        con.execute("INSERT INTO envios (lead_id, asunto, campana, fecha) VALUES (?,?,?,?)",
                    (lead_id, asunto, "manual-panel", ahora()))
    return {"ok": True, "enviado_a": lead["email"]}


@app.post("/api/lead/{lead_id}/llamado")
def api_marcar_llamado(lead_id: int, valor: int = 1,
                       x_api_key: str | None = Header(default=None)):
    """Marca/desmarca un lead como llamado (seguimiento comercial)."""
    verificar(x_api_key)
    with conexion() as con:
        if not con.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(404, "Lead no encontrado")
    actualizar_lead(lead_id, llamado=ahora() if valor else None)
    return {"ok": True, "llamado": bool(valor)}


@app.post("/api/limpiar-sin-contacto")
def api_limpiar(x_api_key: str | None = Header(default=None)):
    """Borra los leads sin email NI teléfono (mismo criterio que el panel).
    Da igual si tienen web: sin canal de contacto directo no sirven."""
    verificar(x_api_key)
    with conexion() as con:
        n = con.execute(
            """DELETE FROM leads
               WHERE (telefono IS NULL OR telefono='')
                 AND (email IS NULL OR email='')
                 AND estado NOT IN ('cliente','respondido')"""
        ).rowcount
    return {"ok": True, "borrados": n,
            "mensaje": f"{n} leads sin contacto eliminados."}


@app.delete("/api/lead/{lead_id}")
def api_borrar_lead(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Borra un lead concreto (botón X del panel)."""
    verificar(x_api_key)
    with conexion() as con:
        n = con.execute("DELETE FROM leads WHERE id = ?", (lead_id,)).rowcount
    if n == 0:
        raise HTTPException(404, "Lead no encontrado")
    return {"ok": True, "borrado": lead_id}


@app.post("/api/lead/nuevo")
def api_nuevo_lead(datos: dict, x_api_key: str | None = Header(default=None)):
    """Añade un contacto a mano desde el panel. Requiere nombre y al menos
    email o teléfono. Si trae email, entra directo al circuito de envío.
    Si marcas 'tengo su permiso', el lead queda llamable por el bot ya
    (alta manual = contacto directo; el permiso se registra con fecha y
    origen y se sincroniza con KD Sonar automáticamente)."""
    verificar(x_api_key)
    import secrets as _s
    nombre = (datos.get("nombre") or "").strip()
    email = (datos.get("email") or "").strip().lower() or None
    telefono = (datos.get("telefono") or "").strip() or None
    if not nombre:
        raise HTTPException(400, "Falta el nombre del negocio")
    if not email and not telefono:
        raise HTTPException(400, "Pon al menos email o teléfono")
    con_permiso = bool(datos.get("consentimiento")) and bool(telefono)
    estado = "auditado" if email else "sin_email"
    pid = f"manual-{_s.token_urlsafe(8)}"
    with conexion() as con:
        con.execute(
            """INSERT INTO leads (place_id, nombre, municipio, provincia, nicho,
                                  telefono, email, web, estado, token_baja,
                                  consentimiento, consent_fuente, consent_fecha,
                                  creado_en, actualizado_en)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, nombre,
             (datos.get("municipio") or "").strip() or None, "Valencia",
             (datos.get("nicho") or "restaurantes").strip(),
             telefono, email, (datos.get("web") or "").strip() or None,
             estado, _s.token_urlsafe(16),
             1 if con_permiso else 0,
             "alta_manual" if con_permiso else None,
             ahora() if con_permiso else None,
             ahora(), ahora()))
    sonar = _sync_consent_sonar(telefono, True, "alta_manual") if con_permiso else "no_aplica"
    return {"ok": True, "mensaje": f"'{nombre}' añadido"
            + (" con permiso de llamada ✓" if con_permiso else "")
            + (" (entrará en la próxima redacción y envío)" if email
               else " (lista de WhatsApp)"),
            "sonar": sonar}


@app.post("/api/reset")
def api_reset(confirmar: str = "", x_api_key: str | None = Header(default=None)):
    """Borra TODOS los leads y el registro de prospección (empezar de cero).
    Requiere ?confirmar=SI para evitar borrados accidentales.
    Mantiene la lista de exclusiones (bajas) por cumplimiento LSSI."""
    verificar(x_api_key)
    if confirmar != "SI":
        raise HTTPException(400, "Añade ?confirmar=SI para borrar todos los leads")
    with conexion() as con:
        n = con.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        con.execute("DELETE FROM leads")
        con.execute("DELETE FROM prospeccion_log")
        con.execute("DELETE FROM envios")
    return {"ok": True, "borrados": n, "mensaje": "CRM vaciado. Listo para empezar de cero."}


@app.post("/api/redactar")
def api_redactar(tareas: BackgroundTasks,
                 nicho: str | None = None,
                 x_api_key: str | None = Header(default=None)):
    """Redacta en segundo plano TODOS los leads auditados que tengan email
    (reintenta los que fallaron). Desbloquea el envío sin esperar el ciclo."""
    verificar(x_api_key)
    import importlib
    mod = importlib.import_module("4_generar_emails")

    with conexion() as con:
        q = ("SELECT COUNT(*) n FROM leads WHERE estado='auditado' "
             "AND email IS NOT NULL AND email != ''")
        params: tuple = ()
        if nicho:
            q += " AND nicho = ?"
            params = (nicho,)
        pendientes = con.execute(q, params).fetchone()["n"]

    if pendientes == 0:
        return {"ok": True, "mensaje": "No hay leads auditados con email "
                "pendientes de redactar."}
    tareas.add_task(mod.main, nicho, False)
    return {"ok": True, "lanzado": True, "pendientes": pendientes,
            "nota": "Redactando en segundo plano. Mira los Deploy Logs y el panel."}


@app.get("/panel", response_class=HTMLResponse)
def panel():
    """Panel CRM (navy/gold). La clave se pide en pantalla y viaja como
    X-API-Key en cada petición; nunca se incrusta en el HTML."""
    return """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KD Radar · CRM</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600;700&family=Inter+Tight:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--navy:#0A1628;--gold:#D4AF37;--gold2:#b8912e;--cream:#f4eede;--card:#101d33;--card2:#0d1829;--line:#22314e;--txt:#e9ecf3;--mut:#8d97ab;--green:#3fae6a;--blue:#5fa8e0;--red:#e08585}
*{box-sizing:border-box;margin:0}
body{background:var(--navy);color:var(--txt);font-family:'Inter Tight',system-ui,sans-serif;min-height:100vh}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--navy);z-index:10}
header img{height:32px}
header h1{font-family:Fraunces,serif;font-size:19px;color:#fff}
header h1 span{color:var(--gold)}
.wrap{max-width:1280px;margin:0 auto;padding:18px 16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.stat b{display:block;font-family:Fraunces,serif;font-size:24px;color:var(--gold);line-height:1.1}
.stat i{font-style:normal;font-size:11px;color:var(--mut);letter-spacing:.5px;text-transform:uppercase}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
input[type=search],select{background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:9px 12px;font-size:14px;font-family:inherit}
input[type=search]{min-width:240px;flex:1}
.tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px}
.tab{background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:99px;padding:8px 15px;font-size:13px;cursor:pointer;white-space:nowrap;transition:.15s}
.tab:hover{border-color:var(--gold2)}
.tab.on{background:var(--gold);color:var(--navy);font-weight:700;border-color:var(--gold)}
.tab b{opacity:.7;font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th{font-size:10.5px;letter-spacing:1px;color:var(--mut);text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);text-transform:uppercase}
td{padding:11px 12px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--card2)}
.nom{font-weight:600;color:#fff;display:block;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{font-size:12px;color:var(--mut)}
.email{color:var(--txt);font-size:13px}
.tel{color:var(--mut);font-size:13px}
.nodato{color:#4a5568;font-style:italic;font-size:12px}
.chip{display:inline-block;font-size:10.5px;padding:3px 8px;border-radius:99px;margin:1px;white-space:nowrap;font-weight:600}
.c-hot{background:#3a2b06;color:var(--gold);border:1px solid var(--gold2)}
.c-open{background:#12324a;color:var(--blue)}
.c-env{background:#173a2a;color:var(--green)}
.c-baja{background:#3d1a1a;color:var(--red)}
.c-red{background:#26304a;color:#aab6d8}
.c-mail{background:#1a3320;color:#7bd99a}
.c-tel{background:#2a2438;color:#c4a8e0}
.acc{background:none;border:1px solid var(--line);color:var(--mut);border-radius:7px;padding:5px 8px;font-size:12px;cursor:pointer;margin:1px;text-decoration:none;display:inline-block}
.acc:hover{border-color:var(--gold);color:var(--gold)}
.acc.wa{border-color:#2a5a3a;color:#7bd99a}
a{color:var(--gold2)}
#login{max-width:380px;margin:14vh auto;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;text-align:center}
#login h2{font-family:Fraunces,serif;margin-bottom:6px;color:#fff}
#login p{color:var(--mut);font-size:13px;margin-bottom:14px}
#login input{width:100%;margin-bottom:12px}
.btn{background:var(--gold);color:var(--navy);border:none;border-radius:10px;padding:11px 20px;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit}
.hide{display:none}
.empty{padding:40px;text-align:center;color:var(--mut)}
.count{font-size:12px;color:var(--mut);margin-bottom:8px}
@media(max-width:760px){.hidem{display:none}.nom{max-width:180px}}
</style></head>
<body>
<header>
  <img src="https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/6928305ea0e60a4050067585_Logo-normal.webp" alt="K&D">
  <h1>KD Radar <span>· CRM</span></h1>
</header>

<div id="login">
  <h2>Acceso al panel</h2>
  <p>Introduce tu clave de acceso (RADAR_API_KEY).</p>
  <input id="clave" type="password" placeholder="Clave" onkeydown="if(event.key==='Enter')entrar()">
  <button class="btn" onclick="entrar()">Entrar</button>
  <p id="err" style="color:#e08585"></p>
</div>

<div class="wrap hide" id="app">
  <div class="stats" id="stats"></div>
  <div id="avisoRec" style="display:none;background:#3a2b06;border:1px solid var(--gold2);color:var(--gold);border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:14px"></div>
  <div id="monitor" style="display:none;background:#0d2038;border:1px solid #2a4a6a;color:#cfe4f7;border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:14px"></div>
  <div class="bar">
    <input type="search" id="buscar" placeholder="Buscar negocio, municipio, email o teléfono..." oninput="pintar()">
    <select id="fnicho" onchange="pintar()"><option value="">Todos los nichos</option></select>
    <select id="fmunicipio" onchange="pintar()"><option value="">Todos los municipios</option></select>
    <select id="forden" onchange="pintar()">
      <option value="">Orden por defecto</option>
      <option value="az">Nombre A-Z</option>
      <option value="za">Nombre Z-A</option>
      <option value="rating">Mejor valorados</option>
      <option value="municipio">Por municipio</option>
    </select>
    <button class="acc" style="border-color:#3a5a3a;color:#7bd99a" onclick="descargarCSV()">⬇ Descargar CSV</button>
    <button class="acc" style="border-color:#3a5a3a;color:#7bd99a" onclick="abrirNuevo()">➕ Añadir contacto</button>
    <button class="acc" style="border-color:var(--gold2);color:var(--gold)" onclick="redactarAhora()">✍️ Redactar ahora</button>
    <button class="acc" style="border-color:#5a3a3a;color:#e08585" onclick="limpiar()">🗑 Limpiar sin contacto</button>
  </div>

  <div id="modalGestion" onclick="if(event.target===this)cerrarGestion()" style="display:none;position:fixed;inset:0;background:rgba(4,8,16,.8);align-items:center;justify-content:center;z-index:50">
    <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;max-width:480px;width:94%;max-height:90vh;overflow-y:auto">
      <h3 style="font-family:Fraunces,serif;color:#fff;margin-bottom:4px" id="g_titulo">Gestión de llamada</h3>
      <p id="g_sub" style="color:var(--mut);font-size:13px;margin-bottom:16px"></p>

      <label style="color:var(--mut);font-size:12px;letter-spacing:.5px">¿CÓMO FUE LA LLAMADA?</label>
      <select id="g_resultado" style="width:100%;margin:6px 0 14px 0;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:10px 12px">
        <option value="">— Sin marcar —</option>
        <option value="contactado">✓ Hablé con el dueño/encargado</option>
        <option value="no_contesta">No contesta</option>
        <option value="buzon">Saltó el buzón</option>
        <option value="volver_llamar">↻ Volver a llamar</option>
        <option value="cita_agendada">📅 Cita/demo agendada</option>
        <option value="no_interesado">✗ No le interesa</option>
      </select>

      <label style="color:var(--mut);font-size:12px;letter-spacing:.5px">NOTAS (con quién hablé, qué dijo, qué necesita...)</label>
      <textarea id="g_notas" rows="4" placeholder="Ej: Hablé con Pepe, el dueño. Le interesa Nora pero quiere pensarlo. Volver a llamar el viernes por la mañana." style="width:100%;margin:6px 0 14px 0;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:10px 12px;font-family:inherit;resize:vertical"></textarea>

      <label style="color:var(--mut);font-size:12px;letter-spacing:.5px">📅 CITA — día y franja acordados (si hay cita)</label>
      <input id="g_cita" placeholder="Ej: miércoles por la tarde, sobre las 17:00" style="width:100%;margin:6px 0 14px 0;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:10px 12px;font-family:inherit">
      <label style="color:var(--mut);font-size:12px;letter-spacing:.5px">⏰ RECORDATORIO — ¿cuándo vuelvo a llamar?</label>
      <input id="g_recordatorio" type="datetime-local" style="width:100%;margin:6px 0 6px 0;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:10px 12px;font-family:inherit">
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px">
        <button type="button" class="acc" onclick="recRapido(2)">En 2h</button>
        <button type="button" class="acc" onclick="recRapido(24)">Mañana</button>
        <button type="button" class="acc" onclick="recRapido(24,10)">Mañana 10:00</button>
        <button type="button" class="acc" onclick="recRapido(24,16)">Mañana 16:00</button>
        <button type="button" class="acc" onclick="recRapido(72)">En 3 días</button>
        <button type="button" class="acc" onclick="document.getElementById('g_recordatorio').value=''">Quitar</button>
      </div>

      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button type="button" class="acc" onclick="cerrarGestion()">Cancelar</button>
        <button type="button" class="btn" onclick="guardarGestion()">Guardar gestión</button>
      </div>
      <p id="g_err" style="color:#e08585;font-size:13px;margin-top:8px"></p>
    </div>
  </div>

  <div id="modalEditar" onclick="if(event.target===this)cerrarEditar()" style="display:none;position:fixed;inset:0;background:rgba(4,8,16,.75);align-items:center;justify-content:center;z-index:50">
    <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;max-width:420px;width:92%">
      <h3 style="font-family:Fraunces,serif;color:#fff;margin-bottom:12px">Editar lead</h3>
      <input id="e_nombre" placeholder="Nombre" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="e_email" placeholder="Email" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="e_tel" placeholder="Teléfono" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="e_municipio" placeholder="Municipio" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="e_web" placeholder="Web" style="width:100%;margin-bottom:14px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button type="button" class="acc" onclick="cerrarEditar()">Cancelar</button>
        <button type="button" class="btn" onclick="guardarEditar()">Guardar</button>
      </div>
      <p id="e_err" style="color:#e08585;font-size:13px;margin-top:8px"></p>
    </div>
  </div>

  <div id="modal" onclick="if(event.target===this)cerrarNuevo()" style="display:none;position:fixed;inset:0;background:rgba(4,8,16,.75);align-items:center;justify-content:center;z-index:50">
    <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;max-width:420px;width:92%">
      <h3 style="font-family:Fraunces,serif;color:#fff;margin-bottom:12px">Añadir contacto</h3>
      <input id="n_nombre" placeholder="Nombre del negocio *" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_email" placeholder="Email" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_tel" placeholder="Teléfono" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_municipio" placeholder="Municipio" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_web" placeholder="Web (para analizar y detectar puntos de dolor)" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <label style="display:block;background:#0d2b1a;border:1px solid #2a5a3a;border-radius:8px;padding:10px 12px;margin-bottom:8px;font-size:12.5px;color:var(--txt);cursor:pointer;line-height:1.5"><input type="checkbox" id="n_consent" style="vertical-align:-2px"> 📞 <b>Tengo su permiso para que le llame el bot</b> — me lo ha pedido, es contacto directo o cliente. Queda registrado con fecha y origen. (Los leads captados en frío nunca llevan permiso: art. 66 LGT.)</label>
      <select id="n_nicho" style="width:100%;margin-bottom:14px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px"></select>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button type="button" class="acc" onclick="cerrarNuevo()">Cancelar</button>
        <button type="button" class="btn" onclick="guardarNuevo()">Guardar</button>
      </div>
      <p id="n_err" style="color:#e08585;font-size:13px;margin-top:8px"></p>
    </div>
  </div>
  <div class="tabs" id="tabs"></div>
  <div class="count" id="count"></div>
  <div style="overflow-x:auto"><table>
    <thead><tr><th>Negocio</th><th>Contacto</th><th class="hidem">Señales</th><th>Acciones</th></tr></thead>
    <tbody id="cuerpo"></tbody>
  </table></div>
</div>

<script>
let LEADS=[], FILTRO='todos';
// Pestañas: separan por CALIDAD de contacto y por estado comercial
const TABS=[
  ['todos','Todos'],
  ['completos','✅ Con email'],
  ['solotel','📱 Solo teléfono'],
  ['sincontacto','⚠️ Sin contacto'],
  ['recordatorios','⏰ Recordatorios'],
  ['llamados','📞 Llamados'],
  ['sinllamar','☎️ Sin llamar'],
  ['listos','✍️ Listos p/ enviar'],
  ['enviados','📤 Enviados'],
  ['calientes','🔥 Calientes'],
  ['abiertos','👀 Abrieron'],
  ['respondidos','💬 Respondidos'],
  ['citas','📅 Citas'],
  ['clientes','⭐ Clientes'],
  ['bajas','🚫 Bajas'],
];
const clave=()=>localStorage.getItem('kd_clave')||'';
async function api(ruta,opts={}){
  const r=await fetch(ruta,{...opts,headers:{'X-API-Key':clave(),...(opts.headers||{})}});
  if(r.status===401) throw new Error('clave');
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.detail||('Error '+r.status));
  return j;
}
async function entrar(){
  const v=document.getElementById('clave').value.trim();
  localStorage.setItem('kd_clave',v);
  try{ await cargar(); }catch(e){ document.getElementById('err').textContent='Clave incorrecta'; }
}
function tieneEmail(l){ return l.email && l.email.trim()!==''; }
function tieneTel(l){ return l.telefono && l.telefono.trim()!==''; }
async function cargar(){
  LEADS=await api('/api/leads');
  document.getElementById('login').classList.add('hide');
  document.getElementById('app').classList.remove('hide');
  const nichos=[...new Set(LEADS.map(l=>l.nicho).filter(Boolean))].sort();
  document.getElementById('fnicho').innerHTML='<option value="">Todos los nichos</option>'+
    nichos.map(n=>`<option>${n}</option>`).join('');
  const muns=[...new Set(LEADS.map(l=>l.municipio).filter(Boolean))].sort();
  document.getElementById('fmunicipio').innerHTML='<option value="">Todos los municipios</option>'+
    muns.map(m=>`<option>${m}</option>`).join('');
  // Aviso de recordatorios vencidos (para no perder ninguna llamada)
  const ahora=new Date();
  const vencidos=LEADS.filter(l=>l.recordatorio && new Date(l.recordatorio)<=ahora && l.estado!=='excluido').length;
  const aviso=document.getElementById('avisoRec');
  if(vencidos>0){
    aviso.style.display='block';
    aviso.innerHTML=`⏰ Tienes <b>${vencidos}</b> ${vencidos===1?'llamada pendiente':'llamadas pendientes'} para ahora. <span style="text-decoration:underline;cursor:pointer" onclick="FILTRO='recordatorios';pintar();document.getElementById('cuerpo').scrollIntoView()">Ver recordatorios →</span>`;
  } else { aviso.style.display='none'; }
  pintar();
}
function pasaFiltroTab(l,tab){
  if(l.estado==='excluido') return tab==='bajas';
  switch(tab){
    case 'todos': return true;
    case 'completos': return tieneEmail(l);
    case 'solotel': return !tieneEmail(l) && tieneTel(l);
    case 'sincontacto': return !tieneEmail(l) && !tieneTel(l);
    case 'recordatorios': return !!l.recordatorio && l.estado!=='excluido';
    case 'llamados': return !!l.llamado;
    case 'sinllamar': return tieneTel(l) && !l.llamado && l.estado!=='excluido';
    case 'listos': return l.estado==='redactado';
    case 'enviados': return ['enviado','respondido','cliente'].includes(l.estado);
    case 'calientes': return !!l.visito_informe && l.estado!=='cliente';
    case 'abiertos': return !!l.email_abierto;
    case 'respondidos': return l.estado==='respondido';
    case 'citas': return l.resultado_llamada==='cita_agendada';
    case 'clientes': return l.estado==='cliente';
    case 'bajas': return l.estado==='excluido';
    default: return true;
  }
}
function pasaBusqueda(l){
  const q=document.getElementById('buscar').value.toLowerCase().trim();
  const fn=document.getElementById('fnicho').value;
  const fm=document.getElementById('fmunicipio').value;
  if(fn && l.nicho!==fn) return false;
  if(fm && l.municipio!==fm) return false;
  if(q && !`${l.nombre} ${l.municipio||''} ${l.email||''} ${l.telefono||''}`.toLowerCase().includes(q)) return false;
  return true;
}
function chips(l){
  let h='';
  if(l.estado==='cliente') h+='<span class="chip c-hot">⭐ CLIENTE</span>';
  if(l.estado==='respondido') h+='<span class="chip c-env">💬 Respondió</span>';
  if(l.visito_informe) h+='<span class="chip c-hot">🔥 Vio informe</span>';
  if(l.email_abierto) h+='<span class="chip c-open">👀 Abrió</span>';
  if(l.estado==='enviado') h+='<span class="chip c-env">📤 Enviado</span>';
  if(l.estado==='redactado') h+='<span class="chip c-red">✍️ Listo</span>';
  if(l.estado==='excluido') h+='<span class="chip c-baja">🚫 Baja</span>';
  if(l.recordatorio){
    const venc = new Date(l.recordatorio) <= new Date();
    const et = {contactado:'✓ Contactado',no_contesta:'No contesta',volver_llamar:'↻ Volver a llamar',no_interesado:'✗ No interesado',cita_agendada:'📅 Cita',buzon:'Buzón'};
    const fecha = new Date(l.recordatorio).toLocaleString('es-ES',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    h+=`<span class="chip" style="background:${venc?'#4a1a1a':'#1a3a4a'};color:${venc?'#ff9a9a':'#7cc4ef'};border:1px solid ${venc?'#e08585':'#3a5a6a'}">⏰ ${venc?'¡AHORA! ':''}${fecha}</span>`;
  }
  const resNames={contactado:'✓ Contactado',no_contesta:'No contesta',volver_llamar:'↻ Rellamar',no_interesado:'✗ No interesa',cita_agendada:'📅 Cita agendada',buzon:'Buzón',no_llamar:'🚫 No llamar',enviar_email:'✉ Pidió email',numero_equivocado:'☎ Nº equivocado'};
  if(l.resultado_llamada && resNames[l.resultado_llamada]) h+=`<span class="chip" style="background:#2a2438;color:#c4a8e0">${resNames[l.resultado_llamada]}</span>`;
  if(l.llamado) h+='<span class="chip" style="background:#3d2a1a;color:#e0a585">📞 Llamado</span>';
  if(l.resultado_llamada==='cita_agendada') h+=`<span class="chip" style="background:#3a2b06;color:var(--gold);border:1px solid var(--gold2)">📅 CITA${l.cita_texto?': '+l.cita_texto:''}</span>`;
  if(l.consentimiento) h+='<span class="chip" style="background:#0d3320;color:#7bd99a;border:1px solid #2a5a3a">📞✔ Permiso bot</span>';
  if(tieneEmail(l)) h+='<span class="chip c-mail">✉ Email</span>';
  else if(tieneTel(l)) h+='<span class="chip c-tel">📱 Tel</span>';
  return h||'<span class="nodato">—</span>';
}
function movil(tel){ if(!tel)return null; const d=tel.replace(/\\D/g,''); const n=d.slice(-9); return (n[0]==='6'||n[0]==='7')?'34'+n:null; }
function pintar(){
  // contadores de cada pestaña (respetando búsqueda/filtros)
  const base=LEADS.filter(pasaBusqueda);
  document.getElementById('tabs').innerHTML=TABS.map(([k,t])=>{
    const n=base.filter(l=>pasaFiltroTab(l,k)).length;
    return `<button class="tab ${FILTRO===k?'on':''}" onclick="FILTRO='${k}';pintar()">${t} <b>${n}</b></button>`;
  }).join('');
  // stats globales
  const s={
    total:LEADS.length,
    email:LEADS.filter(tieneEmail).length,
    tel:LEADS.filter(l=>!tieneEmail(l)&&tieneTel(l)).length,
    listos:LEADS.filter(l=>l.estado==='redactado').length,
    enviados:LEADS.filter(l=>['enviado','respondido','cliente'].includes(l.estado)).length,
    calientes:LEADS.filter(l=>l.visito_informe && l.estado!=='excluido' && l.estado!=='cliente').length,
    clientes:LEADS.filter(l=>l.estado==='cliente').length,
  };
  document.getElementById('stats').innerHTML=
    `<div class="stat"><b>${s.total}</b><i>Leads</i></div>
     <div class="stat"><b>${s.email}</b><i>✉ Con email</i></div>
     <div class="stat"><b>${s.tel}</b><i>📱 Solo tel</i></div>
     <div class="stat"><b>${s.listos}</b><i>✍️ Listos</i></div>
     <div class="stat"><b>${s.enviados}</b><i>📤 Enviados</i></div>
     <div class="stat"><b>${s.calientes}</b><i>🔥 Calientes</i></div>
     <div class="stat"><b>${s.clientes}</b><i>⭐ Clientes</i></div>`;
  // filas visibles
  const vis=base.filter(l=>pasaFiltroTab(l,FILTRO));
  const orden=document.getElementById('forden').value;
  if(orden==='az') vis.sort((a,b)=>(a.nombre||'').localeCompare(b.nombre||''));
  else if(orden==='za') vis.sort((a,b)=>(b.nombre||'').localeCompare(a.nombre||''));
  else if(orden==='rating') vis.sort((a,b)=>(b.rating||0)-(a.rating||0));
  else if(orden==='municipio') vis.sort((a,b)=>(a.municipio||'').localeCompare(b.municipio||'')||(a.nombre||'').localeCompare(b.nombre||''));
  // Recordatorios: ordenar por fecha (los más urgentes primero)
  if(FILTRO==='recordatorios'){
    vis.sort((a,b)=> new Date(a.recordatorio) - new Date(b.recordatorio));
  }
  document.getElementById('count').textContent=`${vis.length} resultados`;
  const el=document.getElementById('cuerpo');
  el.innerHTML=vis.map(l=>{
    const m=movil(l.telefono);
    const contacto = tieneEmail(l)
      ? `<span class="email">${l.email}</span><br><span class="tel">${l.telefono||''}</span>`
      : (tieneTel(l) ? `<span class="tel">📱 ${l.telefono}</span>` : '<span class="nodato">sin contacto</span>');
    return `<tr>
      <td><span class="nom" title="${l.nombre}">${l.nombre}</span>
          <span class="sub">${l.nicho||''} · ${l.municipio||''} ${l.rating?('· '+l.rating+'★'):''}</span></td>
      <td>${contacto}</td>
      <td class="hidem">${chips(l)}</td>
      <td>
        ${tieneEmail(l) && l.estado!=='excluido' ? `<button class="acc" style="border-color:#3a5a3a;color:#7bd99a" title="Enviar email ahora" onclick="enviarEmail(${l.id})">✉ Enviar</button>` : ''}
        ${l.tiene_informe?`<button class="acc" style="border-color:#2a4a5a;color:#7cc4ef" title="Ver el informe de este lead (uso interno, no cuenta como visto)" onclick="verInforme(${l.id})">👁 Ver informe</button>`:''}
        <button class="acc" style="border-color:#5a4a2a;color:#e0c085" title="Analizar web y detectar puntos de dolor (para su informe)" onclick="auditarLead(${l.id})">🔍 ${l.tiene_informe?'Re-auditar':'Auditar'}</button>
        ${l.whatsapp_url?`<a class="acc wa" href="${l.whatsapp_url}" target="_blank" title="WhatsApp con mensaje e informe listo">WhatsApp</a>`:''}
        ${tieneTel(l)?`<button class="acc" style="${l.llamado?'border-color:#5a3a3a;color:#e08585':'border-color:#3a4a5a;color:#7cc4ef'}" title="${l.llamado?'Llamado ✓':'Marcar llamado'}" onclick="marcarLlamado(${l.id},${l.llamado?0:1})">${l.llamado?'📞 Llamado':'📞 Llamar'}</button>`:''}
        ${tieneTel(l)&&l.estado!=='excluido'?`<button class="acc" style="${l.consentimiento?'border-color:#2a5a3a;color:#7bd99a':'border-color:#5a5230;color:#d8c98a'}" title="${l.consentimiento?'Permiso de llamada registrado ('+(l.consent_fuente||'')+'). Clic para revocar.':'Registrar que ME HA PEDIDO la llamada (respondió LLÁMAME, verbal, WhatsApp...). Queda con fecha y origen.'}" onclick="togglePermiso(${l.id},${l.consentimiento?0:1})">${l.consentimiento?'📞✔':'📞 Permiso'}</button>`:''}
        ${tieneTel(l)&&l.estado!=='excluido'?`<button class="acc" style="border-color:var(--gold2);color:var(--gold)" title="El bot (Alba) le llama AHORA para agendar la visita" onclick="llamarBot(${l.id})">🤖 Bot</button>`:''}
        ${tieneTel(l)?`<button class="acc" style="border-color:#3a4a5a;color:#7cc4ef" title="Gestión de llamada" onclick="gestionLead(${l.id})">📋 Gestión</button>`:''}
        <button class="acc" title="Editar" onclick="editarLead(${l.id})">✎</button>
        <button class="acc" style="${l.estado==='cliente'?'border-color:var(--gold);color:var(--gold);background:#3a2b06':''}" title="${l.estado==='cliente'?'Es cliente (clic para quitar)':'Marcar cliente'}" onclick="toggleCliente(${l.id})">${l.estado==='cliente'?'⭐ Cliente':'☆'}</button>
        <button class="acc" title="Borrar" onclick="borrarLead(${l.id})">✕</button>
      </td></tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">Sin leads en esta vista.</td></tr>';
}
async function descargarCSV(){
  try{
    const r=await fetch('/api/exportar.csv?solo_contacto=1',{headers:{'X-API-Key':clave()}});
    if(!r.ok){ alert('Error al descargar'); return; }
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download='kd-radar-leads.csv';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }catch(e){ alert('Error al descargar el CSV'); }
}
async function verInforme(id){
  try{
    const r=await fetch('/api/lead/'+id+'/ver-informe',{headers:{'X-API-Key':clave()}});
    if(!r.ok){ alert('No se pudo abrir el informe. ¿Está auditado?'); return; }
    const html=await r.text();
    const w=window.open('','_blank');
    if(w){ w.document.open(); w.document.write(html); w.document.close(); }
    else alert('Permite las ventanas emergentes para ver el informe.');
  }catch(e){ alert('Error al abrir el informe'); }
}
async function auditarLead(id){
  const l=LEADS.find(x=>x.id===id);
  const conWeb=l&&l.web;
  if(!confirm(conWeb?'¿Analizar la web de "'+l.nombre+'" y generar su informe con puntos de dolor?':'Este lead no tiene web. ¿Generar un informe con los puntos de dolor típicos de su sector? (Puedes añadir su web con el botón ✎ para un análisis más preciso)')) return;
  try{
    const r=await api('/api/lead/'+id+'/auditar',{method:'POST'});
    await cargar();
    if(confirm('✅ '+r.mensaje+'\\n\\n¿Quieres ver el informe ahora?')){
      verInforme(id);
    }
  }catch(e){ alert('⚠️ '+(e.message||'Error al auditar')); }
}
async function togglePermiso(id,valor){
  const l=LEADS.find(x=>x.id===id); if(!l) return;
  if(valor&&!confirm('📞 ¿Confirmas que "'+l.nombre+'" TE HA PEDIDO la llamada del bot?\\n\\n(Respondió LLÁMAME al email, te lo pidió en persona o por WhatsApp, o es contacto directo.)\\n\\nQuedará registrado con fecha y origen, y sincronizado con el bot.')) return;
  if(!valor&&!confirm('¿Revocar el permiso de llamada de "'+l.nombre+'"?')) return;
  try{
    await api('/api/lead/'+id+'/consentimiento',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({valor:!!valor,fuente:'panel_manual'})});
    await cargar();
  }catch(e){ alert('⚠️ '+(e.message||'Error registrando el permiso')); }
}
let monitorTimer=null;
async function llamarBot(id){
  const l=LEADS.find(x=>x.id===id);
  if(!l) return;
  const conPermiso=l.estado==='cliente'||l.consentimiento==1;
  if(!confirm('🤖 ¿Lanzar llamada del bot (Alba) a "'+l.nombre+'" AHORA?\\n\\n'+(conPermiso?'✓ Tiene permiso registrado: se llamará directamente.':'⚠ SIN permiso registrado: el bot la bloqueará. Usa antes el botón 📞 Permiso si te ha pedido la llamada, o márcalo ⭐ cliente.'))) return;
  const box=document.getElementById('monitor');
  box.style.display='block';
  box.innerHTML='🤖 Preparando llamada a <b>'+l.nombre+'</b>…';
  try{
    const r=await api('/api/lead/'+id+'/llamar-sonar',{method:'POST'});
    if(!r.ok){
      const motivos={sin_consentimiento:'⛔ Sin consentimiento: necesita LLÁMAME o ser cliente (art. 66 LGT)',optout:'⛔ Este número pidió no recibir llamadas',fuera_de_ventana_horaria:'⏰ Fuera de la ventana de llamadas ('+(r.detalle||'L-V 10:00-19:30')+')',sin_telefono:'⛔ El lead no tiene teléfono',excluido:'⛔ Lead dado de baja'};
      box.innerHTML=(motivos[r.motivo]||('⚠️ No se pudo llamar: '+(r.detalle||r.motivo||'error')))+' <span style="cursor:pointer;float:right" onclick="cerrarMonitor()">✕</span>';
      return;
    }
    monitorizar(r.call_id, l.nombre);
  }catch(e){
    box.innerHTML='⚠️ '+(e.message||'Error lanzando la llamada')+' <span style="cursor:pointer;float:right" onclick="cerrarMonitor()">✕</span>';
  }
}
function cerrarMonitor(){
  document.getElementById('monitor').style.display='none';
  if(monitorTimer){ clearInterval(monitorTimer); monitorTimer=null; }
}
function monitorizar(callId,nombre){
  const box=document.getElementById('monitor');
  const link='https://dashboard.retellai.com/call-history?history='+callId;
  const estadosTxt={iniciando:'📡 Iniciando…',iniciada:'📡 Marcando…',en_curso:'🔊 EN LLAMADA (escúchala en vivo en Retell)',finalizada:'⏳ Colgó — analizando resultado…',analizada:'✅ Analizada'};
  box.style.display='block';
  box.innerHTML='🤖 Llamando a <b>'+nombre+'</b> · <span id="mon_estado">📡 Iniciando…</span> · <a href="'+link+'" target="_blank" style="color:var(--gold)">Ver/escuchar en Retell →</a> <span style="cursor:pointer;float:right" onclick="cerrarMonitor()">✕</span>';
  if(monitorTimer) clearInterval(monitorTimer);
  let n=0;
  monitorTimer=setInterval(async()=>{
    n++;
    try{
      const s=await api('/api/sonar/llamada/'+callId);
      const est=s.estado||'iniciando';
      document.getElementById('mon_estado').textContent=estadosTxt[est]||est;
      if(est==='analizada'||n>72){
        clearInterval(monitorTimer); monitorTimer=null;
        const resNombres={cita_agendada:'📅 CITA AGENDADA',volver_a_llamar:'↻ Volver a llamar',enviar_email:'✉ Pidió email',no_interesado:'✗ No interesado',numero_equivocado:'☎ Nº equivocado',no_llamar:'🚫 No llamar más'};
        box.innerHTML='🤖 Llamada a <b>'+nombre+'</b> terminada → <b style="color:var(--gold)">'+(resNombres[s.resultado]||s.resultado||'sin resultado')+'</b> · <a href="'+link+'" target="_blank" style="color:var(--gold)">Transcripción y audio →</a> <span style="cursor:pointer;float:right" onclick="cerrarMonitor()">✕</span>';
        cargar();
      }
    }catch(e){}
  },5000);
}
let gestionId=null;
function gestionLead(id){
  const l=LEADS.find(x=>x.id===id); if(!l) return;
  gestionId=id;
  document.getElementById('g_titulo').textContent='Gestión · '+l.nombre;
  document.getElementById('g_sub').textContent=(l.telefono||'')+' · '+(l.municipio||'')+' · '+(l.nicho||'');
  document.getElementById('g_resultado').value=l.resultado_llamada||'';
  document.getElementById('g_notas').value=l.notas||'';
  document.getElementById('g_cita').value=l.cita_texto||'';
  // recordatorio: convertir ISO a formato datetime-local (sin segundos ni Z)
  document.getElementById('g_recordatorio').value=l.recordatorio ? l.recordatorio.slice(0,16) : '';
  document.getElementById('g_err').textContent='';
  document.getElementById('modalGestion').style.display='flex';
}
function cerrarGestion(){ document.getElementById('modalGestion').style.display='none'; }
function recRapido(horas, horaFija){
  const d=new Date(); d.setHours(d.getHours()+horas);
  if(horaFija!==undefined){ d.setHours(horaFija,0,0,0); }
  // formato YYYY-MM-DDTHH:MM en hora local
  const p=n=>String(n).padStart(2,'0');
  document.getElementById('g_recordatorio').value=`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function guardarGestion(){
  const datos={
    resultado:document.getElementById('g_resultado').value,
    notas:document.getElementById('g_notas').value,
    cita_texto:document.getElementById('g_cita').value,
    recordatorio:document.getElementById('g_recordatorio').value,
  };
  try{
    await api('/api/lead/'+gestionId+'/gestion',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(datos)});
    cerrarGestion(); await cargar();
  }catch(e){ document.getElementById('g_err').textContent=e.message||'Error'; }
}
let editandoId=null;
function editarLead(id){
  const l=LEADS.find(x=>x.id===id); if(!l) return;
  editandoId=id;
  document.getElementById('e_nombre').value=l.nombre||'';
  document.getElementById('e_email').value=l.email||'';
  document.getElementById('e_tel').value=l.telefono||'';
  document.getElementById('e_municipio').value=l.municipio||'';
  document.getElementById('e_web').value=l.web||'';
  document.getElementById('e_err').textContent='';
  document.getElementById('modalEditar').style.display='flex';
}
function cerrarEditar(){ document.getElementById('modalEditar').style.display='none'; }
async function guardarEditar(){
  const datos={
    nombre:document.getElementById('e_nombre').value,
    email:document.getElementById('e_email').value,
    telefono:document.getElementById('e_tel').value,
    municipio:document.getElementById('e_municipio').value,
    web:document.getElementById('e_web').value,
  };
  try{
    await api('/api/lead/'+editandoId+'/editar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(datos)});
    cerrarEditar(); await cargar();
  }catch(e){ document.getElementById('e_err').textContent=e.message||'Error'; }
}
async function marcar(id,estado){ try{ await api(`/api/lead/${id}/estado/${estado}`,{method:'POST'}); await cargar(); }catch(e){} }
async function toggleCliente(id){
  try{ await api(`/api/lead/${id}/toggle-cliente`,{method:'POST'}); await cargar(); }catch(e){ alert('Error'); }
}
async function marcarLlamado(id,valor){
  try{ await api(`/api/lead/${id}/llamado?valor=${valor}`,{method:'POST'}); await cargar(); }catch(e){ alert('Error'); }
}
async function enviarEmail(id){
  const l=LEADS.find(x=>x.id===id);
  if(!confirm('¿Enviar el email ahora a "'+(l?l.nombre:'')+'" ('+(l?l.email:'')+')?')) return;
  try{
    const r=await api(`/api/lead/${id}/enviar`,{method:'POST'});
    alert('✅ Email enviado a '+r.enviado_a); await cargar();
  }catch(e){ alert('⚠️ '+e.message); }
}
async function borrarLead(id){
  const l=LEADS.find(x=>x.id===id);
  if(!confirm('¿Borrar "'+(l?l.nombre:'este lead')+'" definitivamente?')) return;
  try{ await api('/api/lead/'+id,{method:'DELETE'}); await cargar(); }catch(e){ alert('Error al borrar'); }
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') cerrarNuevo(); });
async function limpiar(){
  if(!confirm('¿Borrar todos los leads sin ningún contacto (ni email ni teléfono)? No se pueden recuperar.')) return;
  try{ const r=await api('/api/limpiar-sin-contacto',{method:'POST'}); alert(r.mensaje); await cargar(); }catch(e){ alert('Error'); }
}
async function redactarAhora(){
  try{ const r=await api('/api/redactar',{method:'POST'});
    alert(r.pendientes!==undefined ? `Redactando ${r.pendientes} leads en segundo plano. Refresca en unos minutos.` : (r.mensaje||'Lanzado'));
  }catch(e){ alert('Error al lanzar la redacción'); }
}
function abrirNuevo(){
  const nichos=[...new Set(LEADS.map(l=>l.nicho).filter(Boolean))];
  const base=nichos.length?nichos:['restaurantes','barberias','estetica','talleres','fisioterapia','veterinarias','autoescuelas','opticas','gimnasios'];
  document.getElementById('n_nicho').innerHTML=base.map(n=>`<option>${n}</option>`).join('');
  document.getElementById('n_err').textContent='';
  document.getElementById('modal').style.display='flex';
}
function cerrarNuevo(){ document.getElementById('modal').style.display='none'; }
async function guardarNuevo(){
  const datos={
    nombre:document.getElementById('n_nombre').value,
    email:document.getElementById('n_email').value,
    telefono:document.getElementById('n_tel').value,
    municipio:document.getElementById('n_municipio').value,
    web:document.getElementById('n_web').value,
    nicho:document.getElementById('n_nicho').value,
    consentimiento:document.getElementById('n_consent').checked?1:0,
  };
  try{
    const r=await fetch('/api/lead/nuevo',{method:'POST',headers:{'X-API-Key':clave(),'Content-Type':'application/json'},body:JSON.stringify(datos)});
    const j=await r.json();
    if(!r.ok){ document.getElementById('n_err').textContent=j.detail||'Error'; return; }
    cerrarNuevo();
    ['n_nombre','n_email','n_tel','n_municipio'].forEach(i=>document.getElementById(i).value='');
    document.getElementById('n_consent').checked=false;
    await cargar();
  }catch(e){ document.getElementById('n_err').textContent='Error de conexión'; }
}
if(clave()) cargar().catch(()=>{});
</script>
</body></html>"""


@app.get("/baja/{token}", response_class=HTMLResponse)
def baja(token: str):
    with conexion() as con:
        fila = con.execute(
            "SELECT email, nombre FROM leads WHERE token_baja = ?",
            (token,)).fetchone()
    if not fila or not fila["email"]:
        raise HTTPException(404, "Enlace de baja no válido")
    excluir_email(fila["email"], motivo="baja_voluntaria")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Baja confirmada</title>
<style>
  body {{ background:#0A1628; color:#f4eede; font-family:system-ui,sans-serif;
         display:grid; place-items:center; min-height:100vh; margin:0; }}
  .card {{ max-width:420px; padding:2.5rem; text-align:center; }}
  h1 {{ color:#D4AF37; font-size:1.4rem; }}
  p {{ opacity:.85; line-height:1.5; }}
</style></head>
<body><div class="card">
  <h1>Baja confirmada</h1>
  <p>El buzón <strong>{fila['email']}</strong> no volverá a recibir
     comunicaciones comerciales de Ktys &amp; Davids.</p>
  <p>Gracias por tu tiempo.</p>
</div></body></html>"""


# =====================================================================
# INTEGRACIÓN KD SONAR (bot de llamadas salientes) — v1
# ---------------------------------------------------------------------
# KD Sonar llama a los leads y, al terminar cada llamada, empuja aquí el
# resultado (POST /api/sonar/resultado). KD Radar queda como CRM único:
# resultado, cita, notas y opt-outs se ven en el panel de siempre.
# Además expone GET /api/sonar/lote para que Sonar importe leads sin CSV.
# Si la llamada acaba en cita, se envía automáticamente el email de
# confirmación por SMTP (IONOS), registrado en la tabla de envíos.
# =====================================================================
import json as _json

# Resultados extra que introduce el bot (se suman a los del panel manual)
RESULTADOS_LLAMADA.update({"no_llamar", "enviar_email", "numero_equivocado"})

_MAPA_RESULTADO_SONAR = {
    "cita_agendada": "cita_agendada",
    "volver_a_llamar": "volver_llamar",
    "enviar_email": "enviar_email",
    "no_interesado": "no_interesado",
    "numero_equivocado": "numero_equivocado",
    "no_llamar": "no_llamar",
}


def _solo_digitos(texto: str | None) -> str:
    return "".join(c for c in (texto or "") if c.isdigit())


def _lead_por_telefono(telefono: str | None) -> dict | None:
    """Busca el lead comparando los últimos 9 dígitos (formato ES),
    tolerante a prefijos +34/0034, espacios y guiones."""
    cola = _solo_digitos(telefono)[-9:]
    if len(cola) < 9:
        return None
    with conexion() as con:
        filas = con.execute(
            "SELECT * FROM leads WHERE telefono IS NOT NULL AND telefono != ''"
        ).fetchall()
    for f in filas:
        if _solo_digitos(f["telefono"])[-9:] == cola:
            return dict(f)
    return None


def _puntos_dolor_texto(lead: dict, maximo: int = 3, tope_chars: int = 420) -> str:
    """Convierte el JSON de pain_points en una frase corta para el bot."""
    try:
        dolores = _json.loads(lead.get("pain_points") or "[]")
    except (ValueError, TypeError):
        dolores = []
    frases = [d for d in dolores if isinstance(d, str) and d.strip()][:maximo]
    texto = "; ".join(frases)
    if not texto:
        texto = "varios puntos de mejora detectados en su presencia digital"
    return texto[:tope_chars]


def _enviar_confirmacion_cita(lead: dict, fecha_texto: str,
                              email_destino: str) -> str:
    """Envía el email de confirmación de la visita. Devuelve 'enviado' o el error."""
    if not SMTP_PASS:
        return "sin_smtp_configurado"
    if not email_destino:
        return "sin_email"
    nombre = lead.get("nombre") or "su negocio"
    fecha = (fecha_texto or "").strip() or "en la fecha acordada por teléfono"
    baja = (f'{BASE_URL}/baja/{lead["token_baja"]}'
            if lead.get("token_baja") else "")
    texto = (
        f"Hola,\n\nLe confirmamos la visita de David Amundarain "
        f"({REMITENTE_NOMBRE}) a {nombre}: {fecha}.\n\n"
        "Es una visita informativa breve (20-30 minutos), sin coste y sin "
        "compromiso, para enseñarle lo detectado en la revisión digital de su "
        "negocio.\n\nSi necesita cambiar el día o la hora, responda a este "
        f"correo.\n\nUn saludo,\n{REMITENTE_NOMBRE}\n{REMITENTE_EMAIL}"
    )
    html = f"""\
<div style="background:#f4eede;padding:32px 16px;font-family:Georgia,'Times New Roman',serif;color:#0c0905">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e6ddc9;border-radius:10px;overflow:hidden">
    <div style="background:#0c0905;padding:20px 28px">
      <span style="color:#cda450;font-size:20px;letter-spacing:.5px">Ktys &amp; Davids</span>
    </div>
    <div style="padding:28px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6">
      <p style="margin:0 0 14px">Hola,</p>
      <p style="margin:0 0 14px">Le confirmamos la visita de <strong>David Amundarain</strong>
      ({REMITENTE_NOMBRE}) a <strong>{nombre}</strong>:</p>
      <p style="margin:0 0 18px;background:#f4eede;border-left:4px solid #cda450;padding:12px 16px;font-size:16px">
        <strong>{fecha}</strong></p>
      <p style="margin:0 0 14px">Es una visita informativa breve (20&ndash;30 minutos),
      sin coste y sin compromiso, para ense&ntilde;arle lo detectado en la
      revisi&oacute;n digital de su negocio.</p>
      <p style="margin:0 0 14px">Si necesita cambiar el d&iacute;a o la hora,
      responda a este correo.</p>
      <p style="margin:22px 0 0">Un saludo,<br><strong>{REMITENTE_NOMBRE}</strong><br>
      <a href="mailto:{REMITENTE_EMAIL}" style="color:#0c0905">{REMITENTE_EMAIL}</a></p>
    </div>
    <div style="padding:14px 28px;background:#faf6ec;border-top:1px solid #e6ddc9;
                font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#8a8272">
      Ktys &amp; Davids Productions S.L.
      {('&middot; <a href="' + baja + '" style="color:#8a8272">No deseo recibir m&aacute;s correos</a>') if baja else ''}
    </div>
  </div>
</div>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Confirmación de su visita — {REMITENTE_NOMBRE}"
        msg["From"] = f"{REMITENTE_NOMBRE} <{REMITENTE_EMAIL}>"
        msg["To"] = email_destino
        msg["Reply-To"] = REMITENTE_EMAIL
        msg.attach(MIMEText(texto, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {e}"
    with conexion() as con:
        con.execute(
            "INSERT INTO envios (lead_id, asunto, campana, fecha) VALUES (?,?,?,?)",
            (lead["id"], "Confirmación de visita", "confirmacion-cita", ahora()),
        )
    return "enviado"


@app.post("/api/sonar/resultado")
def api_sonar_resultado(datos: dict,
                        x_api_key: str | None = Header(default=None)):
    """Recibe de KD Sonar el resultado de una llamada y actualiza el CRM.

    Body: { telefono, resultado, fecha_cita_texto, email_confirmado,
            notas, solicita_no_llamar, call_id }
    """
    verificar(x_api_key)
    telefono = (datos.get("telefono") or "").strip()
    if not telefono:
        raise HTTPException(400, "Falta 'telefono'")

    lead = _lead_por_telefono(telefono)
    if not lead:
        return {"ok": False, "motivo": "lead_no_encontrado", "telefono": telefono}

    resultado_sonar = (datos.get("resultado") or "").strip().lower()
    solicita_no_llamar = bool(datos.get("solicita_no_llamar"))
    if solicita_no_llamar:
        resultado_sonar = "no_llamar"
    resultado_radar = _MAPA_RESULTADO_SONAR.get(resultado_sonar, "contactado")

    fecha_cita = (datos.get("fecha_cita_texto") or "").strip()
    email_dictado = (datos.get("email_confirmado") or "").strip().lower()
    notas_sonar = (datos.get("notas") or "").strip()
    call_id = (datos.get("call_id") or "").strip()

    # Nota de historial (se acumula sobre las notas existentes del panel)
    partes = [f"[Sonar {ahora()[:16]}] {resultado_radar}"]
    if fecha_cita:
        partes.append(f"Cita: {fecha_cita}")
    if email_dictado:
        partes.append(f"Email en llamada: {email_dictado}")
    if notas_sonar:
        partes.append(notas_sonar)
    if call_id:
        partes.append(f"call {call_id}")
    nota_nueva = " · ".join(partes)
    notas_total = f"{(lead.get('notas') or '').strip()}\n{nota_nueva}".strip()[:4000]

    campos: dict = {
        "resultado_llamada": resultado_radar,
        "llamado": ahora(),
        "notas": notas_total,
    }
    if resultado_radar == "cita_agendada" and fecha_cita:
        campos["cita_texto"] = fecha_cita
    # Si el lead no tenía email y el bot capturó uno, lo guardamos
    if email_dictado and "@" in email_dictado and not (lead.get("email") or "").strip():
        campos["email"] = email_dictado

    email_confirmacion = "no_aplica"
    if resultado_radar == "no_llamar":
        campos["estado"] = "excluido"
        if (lead.get("email") or "").strip():
            excluir_email(lead["email"], motivo="no_llamar_telefono")
    elif resultado_radar == "cita_agendada":
        destino = (campos.get("email") or lead.get("email") or "").strip()
        email_confirmacion = _enviar_confirmacion_cita(lead, fecha_cita, destino)

    actualizar_lead(lead["id"], **campos)
    return {
        "ok": True,
        "lead_id": lead["id"],
        "resultado": resultado_radar,
        "email_confirmacion": email_confirmacion,
    }


@app.get("/api/sonar/lote")
def api_sonar_lote(limite: int = 100, incluir_clientes: int = 1,
                   solo_enviados: int = 1,
                   x_api_key: str | None = Header(default=None)):
    """Leads con teléfono listos para importar en KD Sonar (sin CSV).

    Por defecto: leads en estado 'enviado' (ya recibieron el email frío) y
    'cliente' (relación actual). Los clientes van con consent=1; el resto con
    consent=0 — el consentimiento real lo registra Sonar cuando responden
    LLÁMAME. Los excluidos nunca salen.
    """
    verificar(x_api_key)
    limite = max(1, min(limite, 500))
    estados = []
    if solo_enviados:
        estados.append("enviado")
    else:
        estados += ["enviado", "auditado", "redactado", "con_email", "sin_email"]
    if incluir_clientes:
        estados.append("cliente")
    marcadores = ",".join("?" for _ in estados)
    with conexion() as con:
        filas = con.execute(
            f"""SELECT * FROM leads
                WHERE telefono IS NOT NULL AND telefono != ''
                  AND estado != 'excluido'
                  AND (estado IN ({marcadores}) OR consentimiento = 1)
                ORDER BY CASE WHEN estado='cliente' THEN 0
                              WHEN consentimiento=1 THEN 1 ELSE 2 END,
                         num_resenas DESC
                LIMIT ?""",
            (*estados, limite),
        ).fetchall()
    salida = []
    for f in filas:
        lead = dict(f)
        es_cliente = lead.get("estado") == "cliente"
        con_permiso = es_cliente or lead.get("consentimiento") == 1
        salida.append({
            "id": lead["id"],
            "empresa": lead.get("nombre") or "Negocio",
            "contacto": "",
            "telefono": lead.get("telefono") or "",
            "email": lead.get("email") or "",
            "sector": lead.get("nicho") or "",
            "municipio": lead.get("municipio") or "",
            "puntos_dolor": _puntos_dolor_texto(lead),
            "consent": 1 if con_permiso else 0,
            "consent_source": ("cliente_actual" if es_cliente
                               else (lead.get("consent_fuente") or "panel_radar")
                               if con_permiso else ""),
            "estado_radar": lead.get("estado"),
        })
    return salida


# ---------------------------------------------------------------------
# Llamada bajo demanda desde el panel (botón 🤖 Bot) + monitorización
# ---------------------------------------------------------------------
import httpx as _httpx

SONAR_URL = os.getenv("SONAR_URL", "").rstrip("/")
SONAR_API_KEY = os.getenv("SONAR_API_KEY", "")


def _sonar_configurado() -> bool:
    return bool(SONAR_URL and SONAR_API_KEY)


@app.post("/api/lead/{lead_id}/llamar-sonar")
def api_llamar_sonar(lead_id: int, x_api_key: str | None = Header(default=None)):
    """Lanza AHORA una llamada del bot (KD Sonar / Alba) a este lead.

    El gate legal vive en Sonar: solo llamará si hay consentimiento
    (cliente o LLÁMAME) y dentro de la ventana horaria. Aquí solo
    preparamos los datos frescos del lead y transmitimos la orden.
    """
    verificar(x_api_key)
    if not _sonar_configurado():
        raise HTTPException(400, "Faltan SONAR_URL / SONAR_API_KEY en Railway (servicio KD Radar)")
    with conexion() as con:
        f = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not f:
        raise HTTPException(404, "Lead no encontrado")
    lead = dict(f)
    if not (lead.get("telefono") or "").strip():
        return {"ok": False, "motivo": "sin_telefono"}
    if lead.get("estado") == "excluido":
        return {"ok": False, "motivo": "excluido"}
    payload = {
        "telefono": lead["telefono"],
        "empresa": lead.get("nombre") or "",
        "contacto": "",
        "sector": lead.get("nicho") or "",
        "puntos_dolor": _puntos_dolor_texto(lead),
        "email": lead.get("email") or "",
        "consent": 1 if (lead.get("estado") == "cliente"
                         or lead.get("consentimiento") == 1) else 0,
    }
    try:
        r = _httpx.post(f"{SONAR_URL}/calls/lead", json=payload,
                        headers={"X-API-Key": SONAR_API_KEY}, timeout=25)
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"No se pudo contactar con KD Sonar: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"KD Sonar {r.status_code}: {r.text[:200]}")
    return r.json()


@app.get("/api/sonar/llamada/{call_id}")
def api_estado_llamada(call_id: str, x_api_key: str | None = Header(default=None)):
    """Estado en vivo de una llamada del bot (proxy a KD Sonar para el panel)."""
    verificar(x_api_key)
    if not _sonar_configurado():
        raise HTTPException(400, "Faltan SONAR_URL / SONAR_API_KEY")
    try:
        r = _httpx.get(f"{SONAR_URL}/calls/{call_id}",
                       headers={"X-API-Key": SONAR_API_KEY}, timeout=15)
    except _httpx.HTTPError as e:
        raise HTTPException(502, f"No se pudo contactar con KD Sonar: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"KD Sonar {r.status_code}: {r.text[:200]}")
    return r.json()


# ---------------------------------------------------------------------
# Registro de PERMISO de llamada (consentimiento) en el propio CRM
# ---------------------------------------------------------------------
# El permiso se registra con fecha y origen (registro maestro para la AEPD)
# y se sincroniza automáticamente con KD Sonar. Vías legítimas: cliente,
# alta manual con permiso, LLÁMAME, o petición verbal/WhatsApp registrada
# con el botón del panel. Los leads captados en frío NUNCA llevan permiso.

def _migrar_consentimiento():
    with conexion() as con:
        cols = {f["name"] for f in con.execute("PRAGMA table_info(leads)")}
        if "consentimiento" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN consentimiento INTEGER DEFAULT 0")
        if "consent_fuente" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN consent_fuente TEXT")
        if "consent_fecha" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN consent_fecha TEXT")
        if "cita_texto" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN cita_texto TEXT")


_migrar_consentimiento()


def _sync_consent_sonar(telefono: str, valor: bool, fuente: str) -> str:
    """Empuja el permiso a KD Sonar. Best-effort: si falla, el registro
    maestro queda en Radar y el lote/llamada lo re-sincroniza después."""
    if not _sonar_configurado():
        return "sonar_no_configurado"
    try:
        r = _httpx.post(f"{SONAR_URL}/consents/by-phone",
                        json={"telefono": telefono, "consent": bool(valor),
                              "source": fuente},
                        headers={"X-API-Key": SONAR_API_KEY}, timeout=15)
        return "sincronizado" if r.status_code < 400 else f"error_{r.status_code}"
    except _httpx.HTTPError as e:
        return f"error: {type(e).__name__}"


def _conceder_permiso(lead_id: int, valor: bool, fuente: str) -> None:
    campos = {"consentimiento": 1 if valor else 0,
              "consent_fuente": fuente if valor else None,
              "consent_fecha": ahora() if valor else None}
    actualizar_lead(lead_id, **campos)


@app.post("/api/lead/{lead_id}/consentimiento")
def api_consentimiento(lead_id: int, datos: dict,
                       x_api_key: str | None = Header(default=None)):
    """Registra (o revoca) el permiso de llamada de un lead y lo sincroniza
    con KD Sonar. El permiso queda con fecha y origen: registro maestro."""
    verificar(x_api_key)
    with conexion() as con:
        f = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not f:
        raise HTTPException(404, "Lead no encontrado")
    lead = dict(f)
    if not (lead.get("telefono") or "").strip():
        raise HTTPException(400, "Este lead no tiene teléfono: el permiso de llamada no aplica")
    valor = bool(datos.get("valor"))
    fuente = (datos.get("fuente") or "panel_manual").strip()
    _conceder_permiso(lead_id, valor, fuente)
    sonar = _sync_consent_sonar(lead["telefono"], valor, fuente)
    return {"ok": True, "lead_id": lead_id, "consentimiento": valor,
            "fuente": fuente, "sonar": sonar}
