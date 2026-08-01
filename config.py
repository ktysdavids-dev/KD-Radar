from __future__ import annotations
"""KD Radar v2 — Configuración central (multinicho)."""
import os
from dotenv import load_dotenv

load_dotenv()

def _limpiar(valor: str) -> str:
    """Quita espacios, tabuladores y saltos de línea invisibles que a veces
    se cuelan al pegar claves en las variables de entorno. Una cabecera HTTP
    no admite \\n, así que esto evita el error 'Illegal header value'."""
    return (valor or "").strip().strip("\r\n").strip()


GOOGLE_PLACES_API_KEY = _limpiar(os.getenv("GOOGLE_PLACES_API_KEY", ""))
ANTHROPIC_API_KEY = _limpiar(os.getenv("ANTHROPIC_API_KEY", ""))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
REMITENTE_NOMBRE = os.getenv("REMITENTE_NOMBRE", "Ktys & Davids")
REMITENTE_EMAIL = os.getenv("REMITENTE_EMAIL", "hola@ktysdavids.com")
# SMTP IONOS para enviar emails directamente desde el panel (acción inmediata)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.ionos.es")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "hola@ktysdavids.com")
SMTP_PASS = _limpiar(os.getenv("SMTP_PASS", ""))
EMPRESA_LEGAL = os.getenv("EMPRESA_LEGAL", "Ktys & Davids Productions S.L.")
LOTE_DIARIO = int(os.getenv("LOTE_DIARIO", "30"))
DB_PATH = os.getenv("DB_PATH", "kd_radar.db")

# ---------------------------------------------------------------------------
# MUNICIPIOS OBJETIVO
# ---------------------------------------------------------------------------
MUNICIPIOS_VALENCIA = [
    "Valencia", "Torrent", "Gandia", "Paterna", "Sagunto", "Mislata",
    "Burjassot", "Ontinyent", "Xàtiva", "Alzira", "Cullera", "Oliva",
    "Tavernes de la Valldigna", "Sueca", "Requena", "Llíria", "Catarroja",
    "Manises", "Aldaia", "Alaquàs", "Quart de Poblet", "Xirivella",
    "Picassent", "Alboraya", "Paiporta", "Bétera", "Moncada",
    "Algemesí", "Carcaixent",
]
MUNICIPIOS_ALICANTE = [
    "Alicante", "Elche", "Torrevieja", "Orihuela", "Benidorm", "Alcoy",
    "Elda", "San Vicente del Raspeig", "Dénia", "Petrer", "Villena",
    "Santa Pola", "Calpe", "Xàbia", "Villajoyosa", "Crevillente", "Ibi",
    "Altea", "El Campello", "Mutxamel", "Pilar de la Horadada",
    "Guardamar del Segura", "Novelda", "Aspe", "L'Alfàs del Pi",
]
MUNICIPIOS = [(m, "Valencia") for m in MUNICIPIOS_VALENCIA] + \
             [(m, "Alicante") for m in MUNICIPIOS_ALICANTE]

# ---------------------------------------------------------------------------
# CATÁLOGO DE NICHOS
# Patrón común: negocios que viven del teléfono/WhatsApp para citas o
# pedidos, con sistemas anticuados -> Nora, Qenda, Qena, Xamox, Faktor.
# Campos:
#   nombre        etiqueta legible
#   queries       plantillas de búsqueda Places ({m}=municipio, {p}=provincia)
#   productos     qué les vendemos (se inyecta en el prompt de Claude)
#   dolor         dolor principal del nicho (ancla del email/WhatsApp)
#   usa_delivery  si el flag delivery de Google es relevante
# ---------------------------------------------------------------------------
NICHOS: dict[str, dict] = {
    "restaurantes": {
        "nombre": "Restaurantes con delivery",
        "queries": [
            "restaurantes con servicio a domicilio en {m}, {p}",
            "pizzería comida a domicilio {m}, {p}",
        ],
        "productos": ("Nora (recepcionista telefónica IA: toma pedidos y "
                      "reservas 24/7, integrada con el TPV), Qena (carta QR y "
                      "pedidos online propios sin comisiones de plataformas), "
                      "Qenda (reservas por WhatsApp automatizadas)"),
        "dolor": ("cada llamada perdida en hora punta es un pedido que se va "
                  "a la competencia, y las plataformas de delivery se quedan "
                  "hasta un 30% de comisión"),
        "usa_delivery": True,
    },
    "barberias": {
        "nombre": "Barberías y peluquerías",
        "queries": [
            "barbería en {m}, {p}",
            "peluquería en {m}, {p}",
        ],
        "productos": ("Nora (recepcionista telefónica IA que da citas 24/7 "
                      "mientras el equipo atiende clientes), Qenda (citas por "
                      "WhatsApp automáticas con recordatorios que reducen "
                      "no-shows), Xamox (CRM con ficha e historial de cliente)"),
        "dolor": ("el teléfono suena mientras se atiende a un cliente: o se "
                  "corta el servicio o se pierde la cita, y cada no-show es "
                  "un hueco sin facturar"),
        "usa_delivery": False,
    },
    "estetica": {
        "nombre": "Centros de estética y uñas",
        "queries": [
            "centro de estética en {m}, {p}",
            "salón de uñas manicura {m}, {p}",
        ],
        "productos": ("Nora (citas telefónicas 24/7), Qenda (citas y "
                      "recordatorios por WhatsApp que reducen no-shows), "
                      "Xamox (CRM con historial de tratamientos y bonos)"),
        "dolor": ("las cabinas ocupadas no pueden coger el teléfono: citas "
                  "perdidas cada día, y los no-shows dejan huecos sin facturar"),
        "usa_delivery": False,
    },
    "talleres": {
        "nombre": "Talleres mecánicos",
        "queries": [
            "taller mecánico en {m}, {p}",
            "taller de coches chapa y pintura {m}, {p}",
        ],
        "productos": ("Nora (recepcionista telefónica IA: recoge citas de "
                      "taller y avisos de recogida sin interrumpir al mecánico), "
                      "Xamox (ERP de taller: órdenes de reparación, historial "
                      "por vehículo), Faktor (facturación adaptada a la "
                      "normativa Verifactu, obligatoria a partir de 2027)"),
        "dolor": ("el mecánico suelta la herramienta para coger el teléfono "
                  "o la llamada se pierde, y la gestión en papel o Excel roba "
                  "horas de taller facturables"),
        "usa_delivery": False,
    },
    "fisioterapia": {
        "nombre": "Clínicas de fisioterapia y osteopatía",
        "queries": [
            "clínica de fisioterapia en {m}, {p}",
            "fisioterapeuta osteopatía {m}, {p}",
        ],
        "productos": ("Nora (citas telefónicas 24/7 sin interrumpir las "
                      "sesiones), Qenda (citas y recordatorios por WhatsApp), "
                      "Xamox (CRM con ficha e historial de paciente y bonos "
                      "de sesiones)"),
        "dolor": ("en mitad de una sesión no se puede coger el teléfono: "
                  "pacientes nuevos que llaman y no vuelven a intentarlo"),
        "usa_delivery": False,
    },
    "veterinarias": {
        "nombre": "Clínicas veterinarias",
        "queries": [
            "clínica veterinaria en {m}, {p}",
            "veterinario en {m}, {p}",
        ],
        "productos": ("Nora (recepcionista telefónica IA: citas y urgencias "
                      "bien derivadas, 24/7), Qenda (recordatorios de vacunas "
                      "y citas por WhatsApp), Xamox (CRM con historial por "
                      "mascota)"),
        "dolor": ("consulta llena y teléfono sonando: citas perdidas, y los "
                  "recordatorios de vacunas a mano se olvidan"),
        "usa_delivery": False,
    },
    "autoescuelas": {
        "nombre": "Autoescuelas",
        "queries": [
            "autoescuela en {m}, {p}",
        ],
        "productos": ("Nora (atención telefónica 24/7 para información de "
                      "cursos y matrículas), Qenda (gestión de clases "
                      "prácticas por WhatsApp), Xamox (CRM de alumnos y "
                      "seguimiento de expedientes)"),
        "dolor": ("las llamadas de información llegan cuando la oficina está "
                  "cerrada o el profesor en clase: matrículas que se enfrían"),
        "usa_delivery": False,
    },
    "opticas": {
        "nombre": "Ópticas y centros auditivos",
        "queries": [
            "óptica en {m}, {p}",
            "centro auditivo en {m}, {p}",
        ],
        "productos": ("Nora (citas telefónicas 24/7), Qenda (recordatorios "
                      "de revisiones por WhatsApp), Xamox (CRM con historial "
                      "de graduaciones y avisos de revisión anual)"),
        "dolor": ("las revisiones anuales no se reclaman de forma sistemática: "
                  "clientes que se pierden por no tener recordatorios "
                  "automáticos"),
        "usa_delivery": False,
    },
    "gimnasios": {
        "nombre": "Gimnasios y centros deportivos",
        "queries": [
            "gimnasio en {m}, {p}",
            "centro de yoga pilates {m}, {p}",
        ],
        "productos": ("Nora (atención telefónica 24/7 para información y "
                      "altas), Qenda (reserva de clases por WhatsApp), Xamox "
                      "(CRM de socios, cuotas y asistencia)"),
        "dolor": ("los interesados llaman fuera de horario de recepción y no "
                  "insisten: altas que se pierden todos los meses"),
        "usa_delivery": False,
    },
}


def nicho_config(clave: str) -> dict:
    if clave not in NICHOS:
        disponibles = ", ".join(NICHOS)
        raise SystemExit(f"Nicho '{clave}' no existe. Disponibles: {disponibles}")
    return NICHOS[clave]


# ---------------------------------------------------------------------------
# Extracción de emails
# ---------------------------------------------------------------------------
PREFIJOS_GENERICOS = [
    "info", "hola", "contacto", "reservas", "pedidos", "citas", "clinica",
    "taller", "admin", "administracion", "gerencia", "comercial",
    "hello", "contact",
]
DOMINIOS_BASURA = [
    "example.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
    "godaddy.com", "domain.com", "email.com", "yourdomain",
    "mysite.com", "squarespace.com", "wordpress.com", "polyfill",
]
EXTENSIONES_FALSAS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
