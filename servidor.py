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

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response

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
    with conexion() as con:
        asunto = con.execute("SELECT email_asunto FROM leads WHERE id=?",
                             (lead_id,)).fetchone()["email_asunto"]
        con.execute("INSERT INTO envios (lead_id, asunto, fecha) VALUES (?,?,?)",
                    (lead_id, asunto, ahora()))
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


ESTADOS_MANUALES = {"respondido", "cliente", "descartado", "redactado", "enviado"}


@app.get("/api/leads")
def api_leads(x_api_key: str | None = Header(default=None)):
    """CRM: todos los leads con sus señales (enviado, abierto, informe, baja)."""
    verificar(x_api_key)
    with conexion() as con:
        filas = con.execute(
            """SELECT id, nombre, nicho, municipio, provincia, telefono, email,
                      web, rating, num_resenas, estado, email_abierto,
                      visito_informe, actualizado_en
               FROM leads ORDER BY
                 CASE WHEN visito_informe IS NOT NULL THEN 0
                      WHEN email_abierto IS NOT NULL THEN 1 ELSE 2 END,
                 actualizado_en DESC""").fetchall()
        return [dict(f) for f in filas]


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
    email o teléfono. Si trae email, entra directo al circuito de envío."""
    verificar(x_api_key)
    import secrets as _s
    nombre = (datos.get("nombre") or "").strip()
    email = (datos.get("email") or "").strip().lower() or None
    telefono = (datos.get("telefono") or "").strip() or None
    if not nombre:
        raise HTTPException(400, "Falta el nombre del negocio")
    if not email and not telefono:
        raise HTTPException(400, "Pon al menos email o teléfono")
    estado = "auditado" if email else "sin_email"
    with conexion() as con:
        con.execute(
            """INSERT INTO leads (place_id, nombre, municipio, provincia, nicho,
                                  telefono, email, web, estado, token_baja,
                                  creado_en, actualizado_en)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"manual-{_s.token_urlsafe(8)}", nombre,
             (datos.get("municipio") or "").strip() or None, "Valencia",
             (datos.get("nicho") or "restaurantes").strip(),
             telefono, email, (datos.get("web") or "").strip() or None,
             estado, _s.token_urlsafe(16), ahora(), ahora()))
    return {"ok": True, "mensaje": f"'{nombre}' añadido"
            + (" (entrará en la próxima redacción y envío)" if email
               else " (lista de WhatsApp)")}


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
    tareas.add_task(mod.main, nicho)
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
  <div class="bar">
    <input type="search" id="buscar" placeholder="Buscar negocio, municipio, email o teléfono..." oninput="pintar()">
    <select id="fnicho" onchange="pintar()"><option value="">Todos los nichos</option></select>
    <select id="fmunicipio" onchange="pintar()"><option value="">Todos los municipios</option></select>
    <button class="acc" style="border-color:#3a5a3a;color:#7bd99a" onclick="abrirNuevo()">➕ Añadir contacto</button>
    <button class="acc" style="border-color:var(--gold2);color:var(--gold)" onclick="redactarAhora()">✍️ Redactar ahora</button>
    <button class="acc" style="border-color:#5a3a3a;color:#e08585" onclick="limpiar()">🗑 Limpiar sin contacto</button>
  </div>

  <div id="modal" class="hide" onclick="if(event.target===this)cerrarNuevo()" style="position:fixed;inset:0;background:rgba(4,8,16,.75);display:flex;align-items:center;justify-content:center;z-index:50">
    <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;max-width:420px;width:92%">
      <h3 style="font-family:Fraunces,serif;color:#fff;margin-bottom:12px">Añadir contacto</h3>
      <input id="n_nombre" placeholder="Nombre del negocio *" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_email" placeholder="Email" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_tel" placeholder="Teléfono" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
      <input id="n_municipio" placeholder="Municipio" style="width:100%;margin-bottom:8px;background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px">
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
  ['listos','✍️ Listos p/ enviar'],
  ['enviados','📤 Enviados'],
  ['calientes','🔥 Calientes'],
  ['abiertos','👀 Abrieron'],
  ['respondidos','💬 Respondidos'],
  ['clientes','⭐ Clientes'],
  ['bajas','🚫 Bajas'],
];
const clave=()=>localStorage.getItem('kd_clave')||'';
async function api(ruta,opts={}){
  const r=await fetch(ruta,{...opts,headers:{'X-API-Key':clave(),...(opts.headers||{})}});
  if(r.status===401) throw new Error('clave');
  return r.json();
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
  pintar();
}
function pasaFiltroTab(l,tab){
  if(l.estado==='excluido') return tab==='bajas';
  switch(tab){
    case 'todos': return true;
    case 'completos': return tieneEmail(l);
    case 'solotel': return !tieneEmail(l) && tieneTel(l);
    case 'sincontacto': return !tieneEmail(l) && !tieneTel(l);
    case 'listos': return l.estado==='redactado';
    case 'enviados': return ['enviado','respondido','cliente'].includes(l.estado);
    case 'calientes': return !!l.visito_informe;
    case 'abiertos': return !!l.email_abierto;
    case 'respondidos': return l.estado==='respondido';
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
    calientes:LEADS.filter(l=>l.visito_informe).length,
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
        ${m?`<a class="acc wa" href="https://wa.me/${m}" target="_blank">WhatsApp</a>`:''}
        <button class="acc" title="Respondió" onclick="marcar(${l.id},'respondido')">💬</button>
        <button class="acc" title="Cliente" onclick="marcar(${l.id},'cliente')">⭐</button>
        <button class="acc" title="Borrar" onclick="borrarLead(${l.id})">✕</button>
      </td></tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">Sin leads en esta vista.</td></tr>';
}
async function marcar(id,estado){ try{ await api(`/api/lead/${id}/estado/${estado}`,{method:'POST'}); await cargar(); }catch(e){} }
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
  document.getElementById('modal').classList.remove('hide');
}
function cerrarNuevo(){ document.getElementById('modal').classList.add('hide'); }
async function guardarNuevo(){
  const datos={
    nombre:document.getElementById('n_nombre').value,
    email:document.getElementById('n_email').value,
    telefono:document.getElementById('n_tel').value,
    municipio:document.getElementById('n_municipio').value,
    nicho:document.getElementById('n_nicho').value,
  };
  try{
    const r=await fetch('/api/lead/nuevo',{method:'POST',headers:{'X-API-Key':clave(),'Content-Type':'application/json'},body:JSON.stringify(datos)});
    const j=await r.json();
    if(!r.ok){ document.getElementById('n_err').textContent=j.detail||'Error'; return; }
    cerrarNuevo();
    ['n_nombre','n_email','n_tel','n_municipio'].forEach(i=>document.getElementById(i).value='');
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
