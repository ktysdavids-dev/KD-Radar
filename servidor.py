from __future__ import annotations
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

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from config import LOTE_DIARIO
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
    return {"ok": True, "id": lead_id, "fecha": ahora()}


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
    """Informe de análisis digital personalizado (público, enlazado desde el
    email). Marca el lead como interesado (lead caliente) al abrirse."""
    from db import cargar_json
    with conexion() as con:
        fila = con.execute(
            "SELECT * FROM leads WHERE token_baja = ?", (token,)).fetchone()
    if not fila:
        raise HTTPException(404, "Informe no encontrado")
    lead = dict(fila)
    if not lead.get("visito_informe"):
        actualizar_lead(lead["id"], visito_informe=ahora())

    auditoria = cargar_json(lead.get("auditoria")) or {}
    dolores = cargar_json(lead.get("pain_points")) or []
    nicho = lead.get("nicho") or "restaurantes"
    oferta = NICHO_OFERTA.get(nicho, OFERTA_DEFECTO)

    filas_check = ""
    for clave, etiqueta in CHECKS_INFORME:
        if clave not in auditoria:
            continue
        ok = bool(auditoria.get(clave))
        icono = "&#10003;" if ok else "&#10007;"
        color = "#3fae6a" if ok else "#c0392b"
        filas_check += (f'<tr><td style="padding:9px 12px;color:{color};'
                        f'font-weight:bold;width:26px;">{icono}</td>'
                        f'<td style="padding:9px 0;color:#1a2332;">{etiqueta}</td></tr>')

    lista_dolores = "".join(
        f'<li style="margin:0 0 10px 0;line-height:1.55;">{d}</li>' for d in dolores)
    lista_oferta = "".join(
        f'<li style="margin:0 0 10px 0;line-height:1.55;">{o}</li>' for o in oferta)
    velocidad = auditoria.get("tiempo_carga_s")
    dato_velocidad = (f'<p style="color:#4a5261;">Velocidad de carga medida: '
                      f'<strong>{velocidad}s</strong></p>') if velocidad else ""

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Análisis digital · {lead['nombre']}</title></head>
<body style="margin:0;background:#f4eede;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:640px;margin:0 auto;padding:26px 14px;">
  <div style="background:#ffffff;border-radius:14px;overflow:hidden;">
    <div style="background:#0A1628;padding:26px 32px;">
      <img src="https://cdn.prod.website-files.com/68b944d4a42f90c19d14a5da/6928305ea0e60a4050067585_Logo-normal.webp" alt="Ktys &amp; Davids" width="150" style="display:block;">
    </div>
    <div style="height:4px;background:#D4AF37;"></div>
    <div style="padding:28px 32px;">
      <div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:2px;color:#b8912e;font-weight:bold;">ANÁLISIS DIGITAL GRATUITO</div>
      <h1 style="color:#0A1628;font-size:26px;margin:6px 0 4px 0;">{lead['nombre']}</h1>
      <p style="color:#8a8577;font-family:Arial,sans-serif;font-size:13px;margin:0 0 18px 0;">{lead.get('municipio') or ''} · Informe preliminar elaborado por Ktys &amp; Davids</p>

      <h2 style="color:#0A1628;font-size:18px;border-bottom:1px solid #e8e2d4;padding-bottom:6px;">Estado de tu presencia digital</h2>
      <table style="width:100%;border-collapse:collapse;font-size:15px;">{filas_check}</table>
      {dato_velocidad}

      <h2 style="color:#0A1628;font-size:18px;border-bottom:1px solid #e8e2d4;padding-bottom:6px;margin-top:26px;">Dónde estás perdiendo tiempo y dinero</h2>
      <ul style="color:#1a2332;font-size:15px;padding-left:20px;">{lista_dolores}</ul>

      <div style="background:#0A1628;border-radius:12px;padding:22px 24px;margin-top:24px;">
        <div style="color:#D4AF37;font-family:Arial,sans-serif;font-size:11px;letter-spacing:2px;font-weight:bold;">CÓMO LO RESOLVEMOS</div>
        <ul style="color:#ffffff;font-size:15px;padding-left:20px;margin:10px 0 0 0;">{lista_oferta}</ul>
        <p style="color:#c9cdd6;font-size:13.5px;margin:14px 0 0 0;">Instalado y funcionando en 72 horas. Trato directo con David, el fundador — sin comerciales.</p>
      </div>

      <div style="text-align:center;margin-top:26px;">
        <a href="https://wa.me/34624577459?text=Hola%20David%2C%20he%20visto%20el%20an%C3%A1lisis%20de%20{lead['nombre'].replace(' ', '%20')}%20y%20quiero%20hablar" style="display:inline-block;background:#D4AF37;color:#0A1628;font-family:Arial,sans-serif;font-weight:bold;font-size:16px;padding:14px 28px;border-radius:10px;text-decoration:none;">Hablar con David por WhatsApp</a>
        <p style="font-family:Arial,sans-serif;font-size:13px;color:#8a8577;margin-top:12px;">O agenda una llamada de 10 minutos: <a href="https://calendly.com/ktysdavids-info-bjqc/30min" style="color:#b8912e;">calendly.com/ktysdavids</a></p>
      </div>
    </div>
  </div>
  <p style="text-align:center;font-family:Arial,sans-serif;font-size:11px;color:#8a8577;margin-top:16px;">Ktys &amp; Davids Productions S.L. · ktysdavids.com</p>
</div>
</body></html>"""


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
