from __future__ import annotations
"""KD Radar v2 — Capa de datos (SQLite) con soporte multinicho.

Migra automáticamente bases de datos v1: añade la columna 'nicho' y asigna
'restaurantes' a los leads existentes.

Estados del lead:
  nuevo -> con_email -> auditado -> redactado -> enviado
  Ramas: sin_email, sin_web, excluido (baja/opt-out)
"""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT UNIQUE,
    nombre TEXT NOT NULL,
    direccion TEXT,
    municipio TEXT,
    provincia TEXT,
    telefono TEXT,
    web TEXT,
    delivery INTEGER DEFAULT 0,
    rating REAL,
    num_resenas INTEGER,
    email TEXT,
    email_fuente TEXT,
    auditoria TEXT,
    pain_points TEXT,
    email_asunto TEXT,
    email_cuerpo TEXT,
    email_html TEXT,
    visito_informe TEXT,
    email_abierto TEXT,
    llamado TEXT,
    estado TEXT DEFAULT 'nuevo',
    nicho TEXT DEFAULT 'restaurantes',
    token_baja TEXT UNIQUE,
    creado_en TEXT,
    actualizado_en TEXT
);
CREATE INDEX IF NOT EXISTS idx_estado ON leads(estado);
CREATE INDEX IF NOT EXISTS idx_email ON leads(email);

CREATE TABLE IF NOT EXISTS exclusiones (
    email TEXT PRIMARY KEY,
    motivo TEXT,
    fecha TEXT
);

-- Registro de combos (nicho, municipio) ya prospectados (rotación automática)
CREATE TABLE IF NOT EXISTS prospeccion_log (
    nicho TEXT,
    municipio TEXT,
    fecha TEXT,
    PRIMARY KEY (nicho, municipio)
);

-- Historial de envíos (base para campañas posteriores y re-segmentación)
CREATE TABLE IF NOT EXISTS envios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    asunto TEXT,
    campana TEXT DEFAULT 'kd-radar-1',
    fecha TEXT
);
"""


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conexion():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with conexion() as con:
        con.executescript(SCHEMA)
        # Migración v1 -> v2: añadir columna nicho si la BD ya existía sin ella
        columnas = {f["name"] for f in con.execute("PRAGMA table_info(leads)")}
        if "nicho" not in columnas:
            con.execute("ALTER TABLE leads ADD COLUMN nicho TEXT DEFAULT 'restaurantes'")
        if "email_html" not in columnas:
            con.execute("ALTER TABLE leads ADD COLUMN email_html TEXT")
        if "visito_informe" not in columnas:
            con.execute("ALTER TABLE leads ADD COLUMN visito_informe TEXT")
        if "email_abierto" not in columnas:
            con.execute("ALTER TABLE leads ADD COLUMN email_abierto TEXT")
        if "llamado" not in columnas:
            con.execute("ALTER TABLE leads ADD COLUMN llamado TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_nicho ON leads(nicho)")


def upsert_lead(datos: dict):
    """Inserta un lead nuevo; si el place_id ya existe, no lo duplica."""
    with conexion() as con:
        con.execute(
            """INSERT INTO leads (place_id, nombre, direccion, municipio, provincia,
                                  telefono, web, delivery, rating, num_resenas,
                                  nicho, estado, token_baja, creado_en, actualizado_en)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'nuevo', ?, ?, ?)
               ON CONFLICT(place_id) DO NOTHING""",
            (
                datos["place_id"], datos["nombre"], datos.get("direccion"),
                datos.get("municipio"), datos.get("provincia"),
                datos.get("telefono"), datos.get("web"),
                1 if datos.get("delivery") else 0,
                datos.get("rating"), datos.get("num_resenas"),
                datos.get("nicho", "restaurantes"),
                secrets.token_urlsafe(16), ahora(), ahora(),
            ),
        )


def leads_por_estado(estado: str, con_web: bool = False,
                     nicho: str | None = None) -> list[dict]:
    q, params = "SELECT * FROM leads WHERE estado = ?", [estado]
    if con_web:
        q += " AND web IS NOT NULL AND web != ''"
    if nicho:
        q += " AND nicho = ?"
        params.append(nicho)
    with conexion() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def actualizar_lead(lead_id: int, **campos):
    campos["actualizado_en"] = ahora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    with conexion() as con:
        con.execute(f"UPDATE leads SET {sets} WHERE id = ?",
                    (*campos.values(), lead_id))


def email_excluido(email: str) -> bool:
    if not email:
        return True
    with conexion() as con:
        r = con.execute("SELECT 1 FROM exclusiones WHERE email = ?",
                        (email.lower(),)).fetchone()
        return r is not None


def excluir_email(email: str, motivo: str = "baja_voluntaria"):
    with conexion() as con:
        con.execute(
            "INSERT OR IGNORE INTO exclusiones (email, motivo, fecha) VALUES (?,?,?)",
            (email.lower(), motivo, ahora()),
        )
        con.execute(
            "UPDATE leads SET estado='excluido', actualizado_en=? WHERE lower(email)=?",
            (ahora(), email.lower()),
        )


def lote_para_envio(limite: int) -> list[dict]:
    """Leads redactados, con email, no excluidos. Deduplicados por email."""
    with conexion() as con:
        filas = con.execute(
            """SELECT l.* FROM leads l
               WHERE l.estado = 'redactado'
                 AND l.email IS NOT NULL AND l.email != ''
                 AND lower(l.email) NOT IN (SELECT email FROM exclusiones)
               GROUP BY lower(l.email)
               ORDER BY l.delivery DESC, l.num_resenas DESC
               LIMIT ?""",
            (limite,),
        ).fetchall()
        return [dict(f) for f in filas]


def stats(nicho: str | None = None) -> dict:
    with conexion() as con:
        if nicho:
            filas = con.execute(
                "SELECT estado, COUNT(*) n FROM leads WHERE nicho=? GROUP BY estado",
                (nicho,)).fetchall()
            total = con.execute("SELECT COUNT(*) n FROM leads WHERE nicho=?",
                                (nicho,)).fetchone()["n"]
        else:
            filas = con.execute(
                "SELECT estado, COUNT(*) n FROM leads GROUP BY estado").fetchall()
            total = con.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        return {"total": total, **{f["estado"]: f["n"] for f in filas}}


def stats_por_nicho() -> dict:
    with conexion() as con:
        filas = con.execute(
            "SELECT nicho, COUNT(*) n FROM leads GROUP BY nicho").fetchall()
        return {f["nicho"]: f["n"] for f in filas}


def registrar_prospeccion(nicho: str, municipio: str):
    with conexion() as con:
        con.execute(
            "INSERT OR IGNORE INTO prospeccion_log (nicho, municipio, fecha) VALUES (?,?,?)",
            (nicho, municipio, ahora()),
        )


def prospecciones_hechas() -> set[tuple[str, str]]:
    with conexion() as con:
        return {(r["nicho"], r["municipio"]) for r in
                con.execute("SELECT nicho, municipio FROM prospeccion_log")}


def cargar_json(texto: str | None):
    try:
        return json.loads(texto) if texto else None
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    init_db()
    print("Base de datos lista:", DB_PATH)
    print("Global:", stats())
    print("Por nicho:", stats_por_nicho())
