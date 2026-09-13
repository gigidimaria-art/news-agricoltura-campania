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

INTERVALLO_CONTROLLO = 60

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

# ============================================================
# VERIFICA COLLEGAMENTO DATABASE
# ============================================================

def verifica_database():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*)
            FROM pubblicazioni_monitorate
        """)

        risultato = cur.fetchone()[0]

        print("✅ Collegamento a Neon riuscito")
        print(f"✅ Tabella pubblicazioni_monitorate presente")
        print(f"✅ Pubblicazioni attualmente nel database: {risultato}")

        cur.close()
        conn.close()

        return True

    except Exception as e:
        print(f"❌ Errore collegamento database: {e}")
        return False
# ============================================================
# ESTRAZIONE ELENCO PUBBLICAZIONI DALL'ARCHIVIO
# ============================================================

def estrai_pubblicazioni_archivio():
    try:
        headers = {
            "User-Agent": USER_AGENT
        }

        response = requests.get(
            URL_ARCHIVIO,
            headers=headers,
            timeout=15
        )

        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        pubblicazioni = []

        # Cerchiamo tutti i collegamenti presenti nell'archivio
        for link in soup.find_all("a", href=True):

            titolo = link.get_text(" ", strip=True)

            if not titolo:
                continue

            url = urljoin(URL_ARCHIVIO, link["href"])

            # Consideriamo solo i collegamenti alle singole pubblicazioni
            if "/comunicati/" not in url:
                continue

            # Evitiamo collegamenti tecnici o duplicati
            if url == URL_ARCHIVIO:
                continue

            if any(p["url"] == url for p in pubblicazioni):
                continue

            pubblicazioni.append({
                "titolo": titolo,
                "url": url
            })

        print("============================================")
        print("📋 ESTRAZIONE ARCHIVIO")
        print(f"✅ Pubblicazioni individuate: {len(pubblicazioni)}")

        for i, pubblicazione in enumerate(pubblicazioni, start=1):
            print(f"{i}. {pubblicazione['titolo']}")
            print(f"   {pubblicazione['url']}")

        print("============================================")

        return pubblicazioni

    except Exception as e:
        print(f"❌ Errore estrazione archivio: {e}")
        return []


# ============================================================
# TEST ESTRAZIONE ARCHIVIO
# ============================================================

estrai_pubblicazioni_archivio()

# ============================================================
# AVVIO
# ============================================================

verifica_database()
print(f"✅ Progetto: {NOME_PROGETTO}")
print(f"✅ Fonte ufficiale: {URL_HOME}")
print(f"✅ Archivio: {URL_ARCHIVIO}")
print(f"✅ Intervallo controllo: {INTERVALLO_CONTROLLO} secondi")
