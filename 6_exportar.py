from __future__ import annotations
"""KD Radar v2 — Paso 6: Exportar leads cualificados y listas de acción.

Genera ./exports con tres CSV (todos con columna Nicho):

  1. leads_email_cualificados.csv  -> campaña de email (validados, auditados)
  2. whatsapp_uno_a_uno.csv        -> móviles con enlace wa.me y mensaje
     personalizado por sector, para envío MANUAL uno a uno (David/Toni).
  3. visitas_toni.csv              -> sin web o sin email: ruta presencial.

Uso:
    python3 6_exportar.py              # todos los nichos
    python3 6_exportar.py barberias    # solo un nicho
"""
import csv
import pathlib
import re
import sys
from urllib.parse import quote

from config import NICHOS, nicho_config
from db import init_db, conexion, cargar_json, stats, stats_por_nicho

CARPETA = pathlib.Path("exports")

PLANTILLA_WA = (
    "Hola, soy David, de Ktys & Davids (ktysdavids.com). "
    "He visto {gancho} de {nombre} y os escribo porque trabajamos con "
    "negocios de vuestro sector en la zona: {oferta_corta}. "
    "¿Os viene bien una llamada de 10 minutos esta semana? Un saludo."
)

# Oferta corta por nicho para el mensaje de WhatsApp (una frase, natural)
OFERTA_WA = {
    "restaurantes": ("nuestro recepcionista con IA, Nora, atiende el teléfono "
                     "24/7 y toma pedidos y reservas sin que se pierda ninguna "
                     "llamada"),
    "barberias": ("automatizamos las citas por teléfono y WhatsApp para que "
                  "no se pierda ninguna mientras atendéis clientes"),
    "estetica": ("automatizamos citas y recordatorios por teléfono y WhatsApp "
                 "para llenar huecos y reducir no-shows"),
    "talleres": ("nuestro recepcionista con IA recoge las citas de taller por "
                 "teléfono para que nadie suelte la herramienta"),
    "fisioterapia": ("automatizamos las citas por teléfono y WhatsApp para "
                     "que ninguna llamada se pierda durante las sesiones"),
    "veterinarias": ("automatizamos citas y recordatorios de vacunas por "
                     "teléfono y WhatsApp"),
    "autoescuelas": ("atendemos las llamadas de información 24/7 con IA para "
                     "que ninguna matrícula se enfríe"),
    "opticas": ("automatizamos citas y recordatorios de revisión anual por "
                "teléfono y WhatsApp"),
    "gimnasios": ("atendemos llamadas de información 24/7 con IA y "
                  "automatizamos la reserva de clases por WhatsApp"),
}
OFERTA_DEFECTO = ("automatizamos la atención telefónica y las citas con IA "
                  "para que ninguna llamada se pierda")


def normalizar_movil_es(telefono: str | None) -> str | None:
    """Devuelve 34XXXXXXXXX solo para móviles españoles (6xx/7xx)."""
    if not telefono:
        return None
    digitos = re.sub(r"\D", "", telefono)
    if digitos.startswith("0034"):
        digitos = digitos[4:]
    elif digitos.startswith("34") and len(digitos) == 11:
        digitos = digitos[2:]
    if len(digitos) == 9 and digitos[0] in ("6", "7"):
        return "34" + digitos
    return None


def gancho(lead: dict) -> str:
    if lead.get("rating") and lead.get("num_resenas"):
        return (f"las reseñas ({lead['rating']}★ con "
                f"{lead['num_resenas']} opiniones en Google)")
    if lead.get("web"):
        return "la web"
    return "el perfil en Google"


def _filtro_nicho(nicho: str | None) -> tuple[str, list]:
    return (" AND nicho = ?", [nicho]) if nicho else ("", [])


def exportar_email(con, nicho: str | None) -> int:
    extra, params = _filtro_nicho(nicho)
    filas = con.execute(
        f"""SELECT nombre, nicho, municipio, provincia, email, telefono, web,
                   rating, num_resenas, delivery, pain_points, estado
            FROM leads
            WHERE email IS NOT NULL AND email != ''
              AND estado IN ('auditado', 'redactado', 'enviado')
              AND lower(email) NOT IN (SELECT email FROM exclusiones)
              {extra}
            ORDER BY nicho, num_resenas DESC""", params).fetchall()
    ruta = CARPETA / "leads_email_cualificados.csv"
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Negocio", "Nicho", "Municipio", "Provincia", "Email",
                    "Teléfono", "Web", "Rating", "Reseñas", "Delivery",
                    "Pain points", "Estado"])
        for r in filas:
            dolores = cargar_json(r["pain_points"]) or []
            w.writerow([r["nombre"], r["nicho"], r["municipio"], r["provincia"],
                        r["email"], r["telefono"], r["web"], r["rating"],
                        r["num_resenas"], "Sí" if r["delivery"] else "No",
                        " | ".join(dolores), r["estado"]])
    return len(filas)


def exportar_whatsapp(con, nicho: str | None) -> int:
    extra, params = _filtro_nicho(nicho)
    filas = con.execute(
        f"""SELECT nombre, nicho, municipio, telefono, rating, num_resenas,
                   delivery, estado
            FROM leads
            WHERE telefono IS NOT NULL AND telefono != ''
              AND estado != 'excluido'
              {extra}
            ORDER BY nicho, num_resenas DESC""", params).fetchall()
    ruta = CARPETA / "whatsapp_uno_a_uno.csv"
    n = 0
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Negocio", "Nicho", "Municipio", "Móvil", "Rating",
                    "Enlace WhatsApp (clic y enviar)"])
        for r in filas:
            movil = normalizar_movil_es(r["telefono"])
            if not movil:
                continue
            oferta = OFERTA_WA.get(r["nicho"], OFERTA_DEFECTO)
            mensaje = PLANTILLA_WA.format(nombre=r["nombre"],
                                          gancho=gancho(dict(r)),
                                          oferta_corta=oferta)
            enlace = f"https://wa.me/{movil}?text={quote(mensaje)}"
            w.writerow([r["nombre"], r["nicho"], r["municipio"], movil,
                        r["rating"], enlace])
            n += 1
    return n


def exportar_visitas(con, nicho: str | None) -> int:
    extra, params = _filtro_nicho(nicho)
    filas = con.execute(
        f"""SELECT nombre, nicho, municipio, direccion, telefono, rating,
                   num_resenas, delivery, estado
            FROM leads
            WHERE estado IN ('sin_web', 'sin_email')
              {extra}
            ORDER BY municipio, num_resenas DESC""", params).fetchall()
    ruta = CARPETA / "visitas_toni.csv"
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Negocio", "Nicho", "Municipio", "Dirección", "Teléfono",
                    "Rating", "Reseñas", "Motivo"])
        for r in filas:
            motivo = ("Sin web (máxima necesidad digital)"
                      if r["estado"] == "sin_web" else "Web sin email público")
            w.writerow([r["nombre"], r["nicho"], r["municipio"], r["direccion"],
                        r["telefono"], r["rating"], r["num_resenas"], motivo])
    return len(filas)


def main():
    init_db()
    nicho = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if nicho:
        nicho_config(nicho)
    CARPETA.mkdir(exist_ok=True)
    with conexion() as con:
        n_email = exportar_email(con, nicho)
        n_wa = exportar_whatsapp(con, nicho)
        n_visitas = exportar_visitas(con, nicho)

    ambito = NICHOS[nicho]["nombre"] if nicho else "todos los nichos"
    print(f"Exportado ({ambito}) a ./{CARPETA}/")
    print(f"  leads_email_cualificados.csv : {n_email} leads")
    print(f"  whatsapp_uno_a_uno.csv       : {n_wa} móviles con enlace wa.me")
    print(f"  visitas_toni.csv             : {n_visitas} negocios para visita/llamada")
    print("\nLeads por nicho:", stats_por_nicho())
    print("Resumen BD:", stats())


if __name__ == "__main__":
    main()
