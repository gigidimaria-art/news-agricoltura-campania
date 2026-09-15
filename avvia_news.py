import os
import time
import hashlib
import requests
import psycopg2

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import json
import re

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
        print("✅ Tabella pubblicazioni_monitorate presente")
        print(
            f"✅ Pubblicazioni attualmente nel database: "
            f"{risultato}"
        )

        cur.close()
        conn.close()

        return True

    except Exception as e:

        print(
            f"❌ Errore collegamento database: {e}"
        )

        return False


# ============================================================
# SALVATAGGIO DI UNA PUBBLICAZIONE NEL DATABASE
# ============================================================

def salva_pubblicazione_database(pubblicazione, url):

    try:

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # ----------------------------------------------------
        # DATI PRINCIPALI
        # ----------------------------------------------------

        titolo = pubblicazione.get(
            "titolo",
            ""
        )

        data_pubblicazione = pubblicazione.get(
            "data_pubblicazione"
        ) or None

        # ----------------------------------------------------
        # CONVERSIONE DATA
        # Da DD/MM/YYYY a oggetto date Python
        # ----------------------------------------------------

        if data_pubblicazione:

            data_pubblicazione = datetime.strptime(
                data_pubblicazione,
                "%d/%m/%Y"
            ).date()

        testo_pagina = pubblicazione.get(
            "testo_pagina",
            ""
        )

        documenti = pubblicazione.get(
            "documenti",
            []
        )

        documenti = json.dumps(
            documenti,
            ensure_ascii=False
        )

        # ----------------------------------------------------
        # DATA ULTIMO AGGIORNAMENTO
        # ----------------------------------------------------

        ultimo_aggiornamento = pubblicazione.get(
            "ultimo_aggiornamento"
        ) or None

        if ultimo_aggiornamento:

            mesi = {
                "gennaio": 1,
                "febbraio": 2,
                "marzo": 3,
                "aprile": 4,
                "maggio": 5,
                "giugno": 6,
                "luglio": 7,
                "agosto": 8,
                "settembre": 9,
                "ottobre": 10,
                "novembre": 11,
                "dicembre": 12
            }

            match_aggiornamento = re.search(
                r"ultimo aggiornamento\s+(\d{1,2})\s+([a-zà]+)\s+(\d{4})",
                ultimo_aggiornamento,
                re.IGNORECASE
            )

            if match_aggiornamento:

                giorno = int(
                    match_aggiornamento.group(1)
                )

                mese = mesi[
                    match_aggiornamento.group(2).lower()
                ]

                anno = int(
                    match_aggiornamento.group(3)
                )

                ultimo_aggiornamento = datetime(
                    anno,
                    mese,
                    giorno
                ).date()

            print(
                "ULTIMO AGGIORNAMENTO PRIMA DEL DATABASE:",
                ultimo_aggiornamento
            )

            print(
                "TIPO ULTIMO AGGIORNAMENTO:",
                type(ultimo_aggiornamento)
            )

        # ----------------------------------------------------
        # CONTROLLO DATA
        # ----------------------------------------------------

        print(
            "DATA PRIMA DEL DATABASE:",
            data_pubblicazione
        )

        print(
            "TIPO DATA:",
            type(data_pubblicazione)
        )

        # ----------------------------------------------------
        # IMPRONTA DEL CONTENUTO
        # ----------------------------------------------------

        contenuto = (
            titolo
            + "|"
            + str(data_pubblicazione)
            + "|"
            + testo_pagina
            + "|"
            + str(ultimo_aggiornamento)
            + "|"
            + documenti
        )

        impronta = hashlib.sha256(
            contenuto.encode("utf-8")
        ).hexdigest()

        # ----------------------------------------------------
        # CONTROLLO PUBBLICAZIONE GIÀ PRESENTE
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT impronta
            FROM pubblicazioni_monitorate
            WHERE url = %s
            """,
            (url,)
        )

        risultato = cur.fetchone()

        # ----------------------------------------------------
        # PUBBLICAZIONE GIÀ PRESENTE
        # ----------------------------------------------------

        if risultato:

            impronta_esistente = risultato[0]
            print("IMPRONTA CALCOLATA:", impronta)
            print("IMPRONTA DATABASE:", impronta_esistente)
            
            # ------------------------------------------------
            # PUBBLICAZIONE INVARIATA
            # ------------------------------------------------
            
            if impronta_esistente == impronta:

                print(
                    "ℹ️ Pubblicazione già presente e invariata"
                )

                cur.execute(
                    """
                    UPDATE pubblicazioni_monitorate
                    SET ultima_verifica = CURRENT_TIMESTAMP
                    WHERE url = %s
                    """,
                    (url,)
                )

                conn.commit()

                cur.close()
                conn.close()

                return

            # ------------------------------------------------
            # PUBBLICAZIONE MODIFICATA
            # ------------------------------------------------

            else:

                print(
                    "🔄 Pubblicazione già presente ma modificata"
                )

                cur.execute(
                    """
                    UPDATE pubblicazioni_monitorate
                    SET
                        titolo = %s,
                        data_pubblicazione = %s,
                        ultimo_aggiornamento = %s,
                        testo_pagina = %s,
                        documenti = %s,
                        impronta = %s,
                        ultima_verifica = CURRENT_TIMESTAMP
                    WHERE url = %s
                    """,
                    (
                        titolo,
                        data_pubblicazione,
                        ultimo_aggiornamento,
                        testo_pagina,
                        documenti,
                        impronta,
                        url
                    )
                )

                conn.commit()

                cur.close()
                conn.close()

                print(
                    "✅ Pubblicazione aggiornata nel database"
                )

                return

        # ----------------------------------------------------
        # INSERIMENTO NEL DATABASE
        # ----------------------------------------------------
       
        cur.execute(
            """
            INSERT INTO pubblicazioni_monitorate
            (
                titolo,
                data_pubblicazione,
                ultimo_aggiornamento,
                url,
                testo_pagina,
                documenti,
                impronta
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
            """,
            (
                titolo,
                data_pubblicazione,
                ultimo_aggiornamento,
                url,
                testo_pagina,
                documenti,
                impronta
            )
        )

        conn.commit()

        cur.close()
        conn.close()

        print(
            "✅ Pubblicazione salvata nel database"
        )

    except Exception as e:

        print(
            f"❌ Errore nel salvataggio nel database: {e}"
        )


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

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        pubblicazioni = []

        for link in soup.find_all(
            "a",
            href=True
        ):

            titolo = link.get_text(
                " ",
                strip=True
            )

            if not titolo:
                continue

            url = urljoin(
                URL_ARCHIVIO,
                link["href"]
            )

            # ------------------------------------------------
            # SOLO COLLEGAMENTI NELL'AREA COMUNICATI
            # ------------------------------------------------

            if "/comunicati/" not in url:
                continue

            # ------------------------------------------------
            # ESCLUDIAMO LA PAGINA PRINCIPALE
            # ------------------------------------------------

            if url == URL_ARCHIVIO:
                continue

            # ------------------------------------------------
            # ESCLUDIAMO GLI ARCHIVI ANNUALI
            # ------------------------------------------------

            nome_file = (
                url.rstrip("/")
                .split("/")[-1]
                .lower()
            )

            if nome_file.startswith(
                "comunicati_"
            ):
                continue

            # ------------------------------------------------
            # EVITIAMO DUPLICATI
            # ------------------------------------------------

            if any(
                p["url"] == url
                for p in pubblicazioni
            ):
                continue

            pubblicazioni.append(
                {
                    "titolo": titolo,
                    "url": url
                }
            )

        print(
            "============================================"
        )

        print(
            "📋 ESTRAZIONE ARCHIVIO"
        )

        print(
            f"✅ Pubblicazioni individuate: "
            f"{len(pubblicazioni)}"
        )

        for i, pubblicazione in enumerate(
            pubblicazioni,
            start=1
        ):

            print(
                f"{i}. "
                f"{pubblicazione['titolo']}"
            )

            print(
                f"   {pubblicazione['url']}"
            )

        print(
            "============================================"
        )

        return pubblicazioni

    except Exception as e:

        print(
            f"❌ Errore estrazione archivio: {e}"
        )

        return []


# ============================================================
# TEST ESTRAZIONE ARCHIVIO
# ============================================================

estrai_pubblicazioni_archivio()


# ============================================================
# LETTURA PAGINA INDIVIDUALE DELLA PUBBLICAZIONE
# ============================================================

def estrai_dati_pubblicazione(url):

    try:

        headers = {
            "User-Agent": USER_AGENT
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # ----------------------------------------------------
        # TITOLO
        # ----------------------------------------------------

        h1 = soup.find("h1")

        titolo = (
            h1.get_text(
                " ",
                strip=True
            )
            if h1
            else ""
        )

        # ----------------------------------------------------
        # TESTO COMPLETO DELLA PAGINA
        # ----------------------------------------------------

        testo_pagina = soup.get_text(
            " ",
            strip=True
        )

        # ----------------------------------------------------
        # DATA DI PUBBLICAZIONE
        # ----------------------------------------------------

        data_pubblicazione = ""

        match_data = re.search(
            r"\b(\d{2}/\d{2}/\d{2,4})\s*-\s*Si comunica",
            testo_pagina,
            re.IGNORECASE
        )

        if match_data:

            data_pubblicazione = (
                match_data.group(1)
            )

        # ----------------------------------------------------
        # DOCUMENTI / LINK
        # ----------------------------------------------------

        documenti = []

        for link in soup.find_all(
            "a",
            href=True
        ):

            href = urljoin(
                url,
                link["href"]
            )

            testo_link = link.get_text(
                " ",
                strip=True
            )

            if href.lower().endswith(
                (
                    ".pdf",
                    ".doc",
                    ".docx",
                    ".xls",
                    ".xlsx"
                )
            ):

                documenti.append(
                    {
                        "titolo": testo_link,
                        "url": href
                    }
                )

        # ----------------------------------------------------
        # DATA ULTIMO AGGIORNAMENTO
        # ----------------------------------------------------

        ultimo_aggiornamento = ""

        for elemento in soup.find_all(
            string=True
        ):

            testo = elemento.strip().lower()

            if "ultimo aggiornamento" in testo:

                ultimo_aggiornamento = (
                    elemento.strip()
                )

                break

        # ----------------------------------------------------
        # RISULTATO
        # ----------------------------------------------------

        return {
            "titolo": titolo,
            "data_pubblicazione": data_pubblicazione,
            "testo_pagina": testo_pagina,
            "ultimo_aggiornamento": ultimo_aggiornamento,
            "documenti": documenti
        }

    except Exception as e:

        print(
            f"❌ Errore nella pagina {url}: {e}"
        )

        return None


# ============================================================
# TEST LETTURA PAGINA INDIVIDUALE
# ============================================================

def testa_pagina_individuale():

    pubblicazioni = (
        estrai_pubblicazioni_archivio()
    )

    if not pubblicazioni:

        print(
            "❌ Nessuna pubblicazione disponibile"
        )

        return

    # --------------------------------------------------------
    # PRIMO TEST
    # --------------------------------------------------------

    prima = pubblicazioni[0]

    print(
        "============================================"
    )

    print(
        "🔎 TEST PAGINA INDIVIDUALE"
    )

    print(
        f"Titolo archivio: "
        f"{prima['titolo']}"
    )

    print(
        f"URL: {prima['url']}"
    )

    dati = estrai_dati_pubblicazione(
        prima["url"]
    )

    if dati:

        print(
            "--------------------------------------------"
        )

        print(
            f"Titolo pagina: "
            f"{dati['titolo']}"
        )

        print(
            f"Data pubblicazione: "
            f"{dati['data_pubblicazione']}"
        )

        print(
            f"Testo pagina: "
            f"{len(dati['testo_pagina'])} caratteri"
        )

        print(
            f"Documenti trovati: "
            f"{len(dati['documenti'])}"
        )

        print(
            f"Ultimo aggiornamento: "
            f"{dati['ultimo_aggiornamento']}"
        )

        for documento in dati["documenti"]:

            print(
                f"Documento: "
                f"{documento['titolo']} "
                f"→ {documento['url']}"
            )

        salva_pubblicazione_database(
            dati,
            prima["url"]
        )

    print(
        "============================================"
    )


# ============================================================
# ESECUZIONE TEST
# ============================================================

testa_pagina_individuale()


# ============================================================
# AVVIO
# ============================================================

verifica_database()

print(
    f"✅ Progetto: {NOME_PROGETTO}"
)

print(
    f"✅ Fonte ufficiale: {URL_HOME}"
)

print(
    f"✅ Archivio: {URL_ARCHIVIO}"
)

print(
    f"✅ Intervallo controllo: "
    f"{INTERVALLO_CONTROLLO} secondi"
)
