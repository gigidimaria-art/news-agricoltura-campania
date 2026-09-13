import os
import time
import hashlib
import requests
import psycopg2

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime


# ============================================================
# CONFIGURAZIONE GENERALE
# ============================================================

NOME_PROGETTO = "news-agricoltura-campania"

URL_HOME = "https://agricoltura.regione.campania.it/home.htm"

URL_ARCHIVIO = (
    "https://agricoltura.regione.campania.it/comunicati/comunicati.htm"
)

USER_AGENT = "Mozilla/5.0 News-Agricoltura-Campania"

INTERVALLO_CONTROLLO = 60 * 60


# ============================================================
# VARIABILI D'AMBIENTE
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")

EMAIL_DESTINATARIO = os.getenv("EMAIL_DESTINATARIO")
EMAIL_MITTENTE = os.getenv("EMAIL_MITTENTE")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")


# ============================================================
# CONTROLLO CONFIGURAZIONE
# ============================================================

if not DATABASE_URL:
    print("❌ DATABASE_URL non configurata")
else:
    print("✅ DATABASE_URL configurata")


print(f"✅ Progetto: {NOME_PROGETTO}")
print(f"✅ Fonte ufficiale: {URL_HOME}")
print(f"✅ Archivio: {URL_ARCHIVIO}")
print(f"✅ Intervallo controllo: {INTERVALLO_CONTROLLO} secondi")
