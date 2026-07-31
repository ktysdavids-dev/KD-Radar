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
