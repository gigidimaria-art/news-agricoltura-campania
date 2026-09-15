import os
import re
import json
import hashlib
import requests
import psycopg2

from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup
from flask import Flask, jsonify


# ============================================================
# CONFIGURAZIONE
# ============================================================

URL_ARCHIVIO = "https://agricoltura.regione.campania.it/comunicati/comunicati.htm"
DATABASE_URL = os.environ.get("DATABASE_URL")
USER_AGENT = "Mozilla/5.0 News-Agricoltura-Campania"

CATEGORIE_ARCHIVIO = (
    "Comunicati Stampa",
    "Comunicati",
    "Bandi",
    "Eventi",
    "News",
)

MESI_ITALIANI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

MESI_ARCHIVIO = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
    **MESI_ITALIANI,
}


# ============================================================
# DATABASE
# ============================================================

def connetti_database():
    if not DATABASE_URL:
        raise RuntimeError("Variabile DATABASE_URL non configurata")

    return psycopg2.connect(DATABASE_URL)


def prepara_database():
    """Crea la tabella se necessario e aggiunge testo_archivio senza perdere dati."""
    conn = connetti_database()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pubblicazioni_monitorate (
                id SERIAL PRIMARY KEY,
                titolo TEXT NOT NULL,
                data_pubblicazione DATE,
                categoria TEXT,
                url TEXT NOT NULL UNIQUE,
                descrizione TEXT,
                testo_pagina TEXT,
                ultimo_aggiornamento DATE,
                documenti TEXT,
                impronta TEXT NOT NULL,
                prima_rilevazione TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ultima_verifica TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE pubblicazioni_monitorate
            ADD COLUMN IF NOT EXISTS testo_archivio TEXT
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# ESTRAZIONE ARCHIVIO
# ============================================================

def analizza_link_archivio(link):
    """
    Riconosce esclusivamente i link che nell'archivio hanno il formato:
    Giorno + mese + categoria + testo della pubblicazione.
    """
    testo = " ".join(link.get_text(" ", strip=True).split())
    if not testo:
        return None

    pattern = re.compile(
        r"^\s*(?P<giorno>\d{1,2})\s+"
        r"(?P<mese>gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        r"settembre|ottobre|novembre|dicembre|gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\s+"
        r"(?P<categoria>Comunicati Stampa|Comunicati|Bandi|Eventi|News)\b",
        re.IGNORECASE,
    )

    match = pattern.match(testo)
    if not match:
        return None

    giorno = int(match.group("giorno"))
    mese_nome = match.group("mese").lower()
    mese = MESI_ARCHIVIO.get(mese_nome)
    if not mese or not 1 <= giorno <= 31:
        return None

    categoria = match.group("categoria")
    categoria = next(
        (c for c in CATEGORIE_ARCHIVIO if c.lower() == categoria.lower()),
        categoria,
    )

    return {
        "categoria": categoria,
        "testo_archivio": testo,
        "data_archivio": (mese, giorno),
    }


def estrai_pubblicazioni_archivio():
    response = requests.get(
        URL_ARCHIVIO,
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    risultati_per_url = {}

    for link in soup.find_all("a", href=True):
        dati_archivio = analizza_link_archivio(link)
        if not dati_archivio:
            continue

        url_pubblicazione = urljoin(URL_ARCHIVIO, link["href"].strip())
        parsed = urlparse(url_pubblicazione)

        if parsed.scheme not in ("http", "https"):
            continue

        if parsed.netloc.lower() != "agricoltura.regione.campania.it":
            continue

        # Il frammento (#...) identifica una posizione interna della pagina,
        # non una pubblicazione diversa. Lo eliminiamo per avere un URL
        # canonico e non creare duplicati nel database.
        url_pubblicazione = urlunparse(parsed._replace(fragment=""))

        # Una stessa pagina può comparire più volte nell'archivio, anche con
        # la stessa data, ma con testi archivio diversi (per esempio una
        # pagina contenitore che raccoglie più avvisi). Non eliminiamo quindi
        # le occorrenze: le raccogliamo tutte e le ordiniamo in modo stabile.
        voce = risultati_per_url.setdefault(
            url_pubblicazione,
            {"url": url_pubblicazione, "voci_archivio": []},
        )
        voce_archivio = {
            "categoria": dati_archivio["categoria"],
            "testo": dati_archivio["testo_archivio"],
            "data": dati_archivio["data_archivio"],
        }

        if voce_archivio not in voce["voci_archivio"]:
            voce["voci_archivio"].append(voce_archivio)

    risultati = []
    for voce in risultati_per_url.values():
        voci = sorted(
            voce["voci_archivio"],
            key=lambda item: (item["data"][0], item["data"][1], item["categoria"], item["testo"]),
            reverse=True,
        )
        categorie = []
        testi = []
        for item in voci:
            if item["categoria"] not in categorie:
                categorie.append(item["categoria"])
            testi.append(item["testo"])

        risultati.append(
            {
                "url": voce["url"],
                "categoria": ", ".join(categorie),
                "testo_archivio": "\n".join(testi),
            }
        )

    if not risultati:
        raise RuntimeError("Nessuna pubblicazione individuata nell'archivio")

    return risultati


# ============================================================
# ESTRAZIONE PAGINA INDIVIDUALE
# ============================================================

def estrai_data_pubblicazione(testo):
    """Cerca prima la data associata all'inizio della pubblicazione."""
    match = re.search(
        r"\b(\d{2}/\d{2}/\d{4})\s*-\s*",
        testo,
    )
    if match:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()

    match = re.search(
        r"\b(\d{2}/\d{2}/\d{2})\s*-\s*",
        testo,
    )
    if match:
        return datetime.strptime(match.group(1), "%d/%m/%y").date()

    return None


def estrai_ultimo_aggiornamento(testo):
    pattern = re.compile(
        r"ultimo aggiornamento\s+(\d{1,2})\s+([A-Za-zàèéìòù]+)\s+(\d{4})",
        re.IGNORECASE,
    )

    match = pattern.search(testo)
    if not match:
        return None

    giorno = int(match.group(1))
    mese_nome = match.group(2).lower()
    anno = int(match.group(3))
    mese = MESI_ITALIANI.get(mese_nome)

    if not mese:
        return None

    try:
        return datetime(anno, mese, giorno).date()
    except ValueError:
        return None


def estrai_documenti(soup, url_base):
    estensioni = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".zip",
    )

    documenti = []
    visti = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        url_documento = urljoin(url_base, href)
        percorso = urlparse(url_documento).path.lower()

        if not percorso.endswith(estensioni):
            continue

        if url_documento in visti:
            continue

        visti.add(url_documento)
        documenti.append(
            {
                "titolo": " ".join(link.get_text(" ", strip=True).split()),
                "url": url_documento,
            }
        )

    return documenti


def estrai_titolo_pagina(soup):
    for tag_name in ("h1", "h2", "h3"):
        tag = soup.find(tag_name)
        if tag:
            titolo = " ".join(tag.get_text(" ", strip=True).split())
            if titolo:
                return titolo

    if soup.title:
        titolo = " ".join(soup.title.get_text(" ", strip=True).split())
        if titolo:
            return titolo

    return ""


def estrai_pubblicazione(dato_archivio):
    url = dato_archivio["url"]

    response = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    testo_pagina = " ".join(soup.get_text(" ", strip=True).split())

    titolo = estrai_titolo_pagina(soup)
    data_pubblicazione = estrai_data_pubblicazione(testo_pagina)
    ultimo_aggiornamento = estrai_ultimo_aggiornamento(testo_pagina)
    documenti = estrai_documenti(soup, url)

    if not titolo:
        raise ValueError(f"Titolo non trovato nella pagina: {url}")

    return {
        "titolo": titolo,
        "data_pubblicazione": data_pubblicazione,
        "categoria": dato_archivio["categoria"],
        "url": url,
        "descrizione": dato_archivio["testo_archivio"],
        "testo_archivio": dato_archivio["testo_archivio"],
        "testo_pagina": testo_pagina,
        "ultimo_aggiornamento": ultimo_aggiornamento,
        "documenti": documenti,
    }


# ============================================================
# IMPRONTA
# ============================================================

def calcola_impronta(pubblicazione):
    documenti = json.dumps(
        pubblicazione["documenti"],
        ensure_ascii=False,
        sort_keys=True,
    )

    contenuto = "|".join(
        [
            pubblicazione["titolo"],
            str(pubblicazione["data_pubblicazione"]),
            pubblicazione["categoria"],
            pubblicazione["testo_archivio"],
            pubblicazione["testo_pagina"],
            str(pubblicazione["ultimo_aggiornamento"]),
            documenti,
        ]
    )

    return hashlib.sha256(contenuto.encode("utf-8")).hexdigest()


# ============================================================
# SALVATAGGIO E CONFRONTO
# ============================================================

def salva_pubblicazione(pubblicazione):
    impronta = calcola_impronta(pubblicazione)
    pubblicazione["impronta"] = impronta

    conn = connetti_database()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, impronta
            FROM pubblicazioni_monitorate
            WHERE url = %s
            """,
            (pubblicazione["url"],),
        )

        riga = cur.fetchone()

        documenti_json = json.dumps(
            pubblicazione["documenti"],
            ensure_ascii=False,
        )

        if riga:
            id_record, impronta_database = riga

            if impronta_database == impronta:
                cur.execute(
                    """
                    UPDATE pubblicazioni_monitorate
                    SET ultima_verifica = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (id_record,),
                )
                print("ℹ️ Pubblicazione già presente e invariata")
            else:
                cur.execute(
                    """
                    UPDATE pubblicazioni_monitorate
                    SET titolo = %s,
                        data_pubblicazione = %s,
                        categoria = %s,
                        descrizione = %s,
                        testo_archivio = %s,
                        testo_pagina = %s,
                        ultimo_aggiornamento = %s,
                        documenti = %s,
                        impronta = %s,
                        ultima_verifica = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        pubblicazione["titolo"],
                        pubblicazione["data_pubblicazione"],
                        pubblicazione["categoria"],
                        pubblicazione["descrizione"],
                        pubblicazione["testo_archivio"],
                        pubblicazione["testo_pagina"],
                        pubblicazione["ultimo_aggiornamento"],
                        documenti_json,
                        impronta,
                        id_record,
                    ),
                )
                print("🔄 Pubblicazione modificata e aggiornata")
        else:
            cur.execute(
                """
                INSERT INTO pubblicazioni_monitorate (
                    titolo,
                    data_pubblicazione,
                    categoria,
                    url,
                    descrizione,
                    testo_archivio,
                    testo_pagina,
                    ultimo_aggiornamento,
                    documenti,
                    impronta
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pubblicazione["titolo"],
                    pubblicazione["data_pubblicazione"],
                    pubblicazione["categoria"],
                    pubblicazione["url"],
                    pubblicazione["descrizione"],
                    pubblicazione["testo_archivio"],
                    pubblicazione["testo_pagina"],
                    pubblicazione["ultimo_aggiornamento"],
                    documenti_json,
                    impronta,
                ),
            )
            print("🆕 Nuova pubblicazione inserita")

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


# ============================================================
# CICLO DI TEST
# ============================================================

def testa_pagine_archivio():
    pubblicazioni_archivio = estrai_pubblicazioni_archivio()

    print(
        f"Pubblicazioni individuate nell'archivio: {len(pubblicazioni_archivio)}"
    )

    risultati = []
    errori = []

    for dato_archivio in pubblicazioni_archivio:
        try:
            pubblicazione = estrai_pubblicazione(dato_archivio)
            salva_pubblicazione(pubblicazione)
            risultati.append(pubblicazione)
        except Exception as e:
            errore = {
                "url": dato_archivio["url"],
                "errore": str(e),
            }
            errori.append(errore)
            print(
                f"❌ Errore nella pubblicazione {dato_archivio['url']}: {e}"
            )

    return risultati, errori


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify(
        {
            "progetto": "News Agricoltura Campania",
            "stato": "attivo",
            "fonte": URL_ARCHIVIO,
        }
    )


@app.route("/test")
def test():
    prepara_database()
    risultati, errori = testa_pagine_archivio()

    return jsonify(
        {
            "stato": "ok" if not errori else "completato_con_errori",
            "pubblicazioni_individuate": len(risultati) + len(errori),
            "pubblicazioni_elaborate": len(risultati),
            "errori": errori,
        }
    )


if __name__ == "__main__":
    prepara_database()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
