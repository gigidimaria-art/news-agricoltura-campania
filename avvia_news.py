import os
import re
import json
import hashlib
import smtplib
import threading
import time
import requests
import psycopg2

from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup
from flask import Flask, jsonify


# ============================================================
# CONFIGURAZIONE
# ============================================================

URL_ARCHIVIO = "https://agricoltura.regione.campania.it/comunicati/comunicati.htm"
DATABASE_URL = os.environ.get("DATABASE_URL")
USER_AGENT = "Mozilla/5.0 News-Agricoltura-Campania"

# Gmail SMTP.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
INTERVALLO_MINUTI = max(1, int(os.environ.get("INTERVALLO_MINUTI", "60")))
CONTROLLO_COMPLETO_ORE = max(1, int(os.environ.get("CONTROLLO_COMPLETO_ORE", "24")))
EMAIL_MITTENTE = os.environ.get("EMAIL_MITTENTE")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO")

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

    return psycopg2.connect(DATABASE_URL, connect_timeout=20)


def prepara_database():
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

        # Contiene l'impronta dell'ultima versione per la quale l'email
        # di notifica è stata inviata con successo.
        cur.execute(
            """
            ALTER TABLE pubblicazioni_monitorate
            ADD COLUMN IF NOT EXISTS impronta_notificata TEXT
            """
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def database_ha_gia_dati():
    conn = connetti_database()
    cur = conn.cursor()
    try:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pubblicazioni_monitorate)")
        return bool(cur.fetchone()[0])
    finally:
        cur.close()
        conn.close()


# ============================================================
# EMAIL
# ============================================================

def configurazione_email_completa():
    mancanti = []
    if not EMAIL_MITTENTE:
        mancanti.append("EMAIL_MITTENTE")
    if not EMAIL_PASSWORD:
        mancanti.append("EMAIL_PASSWORD")
    if not EMAIL_DESTINATARIO:
        mancanti.append("EMAIL_DESTINATARIO")
    return mancanti


def invia_email_notifica(pubblicazione, tipo):
    mancanti = configurazione_email_completa()
    if mancanti:
        raise RuntimeError(
            "Configurazione email incompleta. Variabili mancanti: "
            + ", ".join(mancanti)
        )

    if tipo == "nuova":
        oggetto = f"[News Agricoltura Campania] Nuova pubblicazione: {pubblicazione['titolo']}"
        intestazione = "È stata rilevata una nuova pubblicazione."
    else:
        oggetto = f"[News Agricoltura Campania] Pubblicazione aggiornata: {pubblicazione['titolo']}"
        intestazione = "È stata rilevata una modifica a una pubblicazione già monitorata."

    corpo = (
        f"{intestazione}\n\n"
        f"Titolo: {pubblicazione['titolo']}\n"
        f"Categoria: {pubblicazione['categoria']}\n"
        f"Data pubblicazione: {pubblicazione['data_pubblicazione'] or 'non rilevata'}\n"
        f"Ultimo aggiornamento: {pubblicazione['ultimo_aggiornamento'] or 'non rilevato'}\n\n"
        f"Testo nell'archivio:\n{pubblicazione['testo_archivio']}\n\n"
        f"Pagina ufficiale:\n{pubblicazione['url']}\n\n"
        f"Fonte:\n{URL_ARCHIVIO}\n"
    )

    messaggio = EmailMessage()
    messaggio["Subject"] = oggetto
    messaggio["From"] = EMAIL_MITTENTE
    messaggio["To"] = EMAIL_DESTINATARIO
    messaggio.set_content(corpo)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(EMAIL_MITTENTE, EMAIL_PASSWORD)
        server.send_message(messaggio)


# ============================================================
# ESTRAZIONE ARCHIVIO
# ============================================================

def analizza_link_archivio(link):
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

    if not mese:
        return None

    try:
        datetime(2000, mese, giorno)
    except ValueError:
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
        timeout=20,
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

        # Il frammento (#...) non identifica una pagina diversa.
        url_pubblicazione = urlunparse(parsed._replace(fragment=""))

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
            key=lambda item: (
                item["data"][0],
                item["data"][1],
                item["categoria"],
                item["testo"],
            ),
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
    match = re.search(r"\b(\d{2}/\d{2}/\d{4})\s*-\s*", testo)
    if match:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()

    match = re.search(r"\b(\d{2}/\d{2}/\d{2})\s*-\s*", testo)
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
    estensioni = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")

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
        timeout=20,
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
# SALVATAGGIO, CONFRONTO E NOTIFICA
# ============================================================

def salva_pubblicazione(pubblicazione, invia_notifica=True):
    impronta = calcola_impronta(pubblicazione)
    pubblicazione["impronta"] = impronta

    conn = connetti_database()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, impronta, impronta_notificata
            FROM pubblicazioni_monitorate
            WHERE url = %s
            """,
            (pubblicazione["url"],),
        )
        riga = cur.fetchone()

        documenti_json = json.dumps(
            pubblicazione["documenti"],
            ensure_ascii=False,
            sort_keys=True,
        )

        if riga:
            id_record, impronta_database, impronta_notificata = riga

            if impronta_database == impronta:
                cur.execute(
                    """
                    UPDATE pubblicazioni_monitorate
                    SET ultima_verifica = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (id_record,),
                )
                conn.commit()
                stato = "invariata"
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
                conn.commit()
                stato = "modificata"

            if invia_notifica and EMAIL_MITTENTE and EMAIL_PASSWORD and EMAIL_DESTINATARIO:
                if impronta_notificata != impronta:
                    tipo_notifica = "nuova" if impronta_notificata is None else "modificata"
                    invia_email_notifica(pubblicazione, tipo_notifica)
                    cur.execute(
                        """
                        UPDATE pubblicazioni_monitorate
                        SET impronta_notificata = %s
                        WHERE id = %s
                        """,
                        (impronta, id_record),
                    )
                    conn.commit()
                    print("✉️ Email di notifica inviata")

            if stato == "invariata":
                print("ℹ️ Pubblicazione già presente e invariata")
            else:
                print("🔄 Pubblicazione modificata e aggiornata")

            return stato

        # Nuova URL.
        # Se il database era già popolato, la nuova pubblicazione è una vera
        # novità e viene notificata. Se il database è vuoto, la prima esecuzione
        # costruisce la base iniziale senza inviare centinaia di email.
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
                impronta,
                impronta_notificata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                None,
            ),
        )
        conn.commit()

        stato = "nuova"

        if invia_notifica:
            invia_email_notifica(pubblicazione, "nuova")
            cur.execute(
                """
                UPDATE pubblicazioni_monitorate
                SET impronta_notificata = %s
                WHERE url = %s
                """,
                (impronta, pubblicazione["url"]),
            )
            conn.commit()
            print("✉️ Email di notifica inviata")
        else:
            # Prima costruzione della base: niente email.
            cur.execute(
                """
                UPDATE pubblicazioni_monitorate
                SET impronta_notificata = %s
                WHERE url = %s
                """,
                (impronta, pubblicazione["url"]),
            )
            conn.commit()

        print("🆕 Nuova pubblicazione inserita")
        return stato

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


# ============================================================
# CICLO DI ELABORAZIONE
# ============================================================

def testa_pagine_archivio():
    print("▶️ Avvio estrazione archivio", flush=True)
    pubblicazioni_archivio = estrai_pubblicazioni_archivio()
    print(
        f"Pubblicazioni individuate nell'archivio: {len(pubblicazioni_archivio)}",
        flush=True,
    )

    risultati = []
    errori = []

    # Se il DB è vuoto, questa esecuzione costruisce la base iniziale.
    # Non inviamo email per le pubblicazioni già presenti nell'archivio.
    prima_esecuzione = not database_ha_gia_dati()

    # Leggiamo una sola volta dal database ciò che serve per decidere se una
    # pagina individuale deve essere scaricata. In questo modo una pubblicazione
    # già presente e invariata nell'archivio NON viene riscaricata ogni 60 minuti.
    conn = connetti_database()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT url, categoria, testo_archivio, impronta, impronta_notificata, ultima_verifica
            FROM pubblicazioni_monitorate
            """
        )
        record_database = {
            r[0]: {
                "categoria": r[1] or "",
                "testo_archivio": r[2] or "",
                "impronta": r[3],
                "impronta_notificata": r[4],
                "ultima_verifica": r[5],
            }
            for r in cur.fetchall()
        }
    finally:
        cur.close()
        conn.close()

    for dato_archivio in pubblicazioni_archivio:
        url = dato_archivio["url"]
        record = record_database.get(url)

        # Se URL, categoria e testo dell'archivio sono identici e l'ultima
        # versione è già stata notificata, evitiamo di scaricare continuamente
        # la pagina individuale. Tuttavia eseguiamo periodicamente un controllo
        # completo della pagina, così da intercettare eventuali modifiche che
        # non vengano riflesse nell'archivio.
        ultima_verifica = record["ultima_verifica"] if record is not None else None
        controllo_completo_necessario = (
            ultima_verifica is None
            or (datetime.now() - ultima_verifica).total_seconds() >= CONTROLLO_COMPLETO_ORE * 3600
        )

risultati.append(
    {
        "url": url,
        "titolo": "",
        "stato": "invariata_archivio",
    }
)
print(
    f"ℹ️ Invariata nell'archivio, pagina non scaricata: {url}",
    flush=True,
)
continue

        # Nuova pubblicazione oppure modifica rilevata nell'archivio: in questi
        # casi è necessario scaricare la pagina individuale per ricostruire la
        # versione completa e calcolare l'impronta.
        print(f"🔎 Elaborazione pubblicazione: {url}", flush=True)
        try:
            pubblicazione = estrai_pubblicazione(dato_archivio)
            stato = salva_pubblicazione(
                pubblicazione,
                invia_notifica=not prima_esecuzione,
            )
            risultati.append(
                {
                    "url": pubblicazione["url"],
                    "titolo": pubblicazione["titolo"],
                    "stato": stato,
                }
            )
        except Exception as e:
            errore = {"url": url, "errore": str(e)}
            errori.append(errore)
            print(f"❌ Errore nella pubblicazione {url}: {e}", flush=True)

    return risultati, errori, prima_esecuzione


# ============================================================
# CICLO AUTOMATICO
# ============================================================

_monitoraggio_lock = threading.Lock()
_stato_monitoraggio = {
    "ultimo_avvio": None,
    "ultima_fine": None,
    "ultima_esecuzione": None,
    "ultimo_errore": None,
}

def esegui_monitoraggio():
    if not _monitoraggio_lock.acquire(blocking=False):
        print("ℹ️ Monitoraggio già in esecuzione")
        return

    _stato_monitoraggio["ultimo_avvio"] = datetime.now().isoformat()
    try:
        risultati, errori, prima_esecuzione = testa_pagine_archivio()
        _stato_monitoraggio["ultima_esecuzione"] = {
            "prima_esecuzione": prima_esecuzione,
            "pubblicazioni_elaborate": len(risultati),
            "errori": errori,
        }
        _stato_monitoraggio["ultimo_errore"] = None if not errori else "Una o più pubblicazioni non sono state elaborate"
        print(
            f"✅ Monitoraggio completato: {len(risultati)} elaborate, "
            f"{len(errori)} errori"
        )
    except Exception as e:
        _stato_monitoraggio["ultima_esecuzione"] = None
        _stato_monitoraggio["ultimo_errore"] = str(e)
        print(f"❌ Errore del ciclo di monitoraggio: {e}")
    finally:
        _stato_monitoraggio["ultima_fine"] = datetime.now().isoformat()
        _monitoraggio_lock.release()

def ciclo_automatico():
    while True:
        esegui_monitoraggio()
        time.sleep(max(1, INTERVALLO_MINUTI) * 60)


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
            "notifiche": "email Gmail",
            "intervallo_minuti": INTERVALLO_MINUTI,
            "monitoraggio": _stato_monitoraggio,
        }
    )


@app.route("/test")
def test():
    prepara_database()
    risultati, errori, prima_esecuzione = testa_pagine_archivio()

    return jsonify(
        {
            "stato": "ok" if not errori else "completato_con_errori",
            "prima_esecuzione": prima_esecuzione,
            "pubblicazioni_elaborate": len(risultati),
            "errori": errori,
        }
    )


@app.route("/test-email")
def test_email():
    mancanti = configurazione_email_completa()
    if mancanti:
        return jsonify(
            {
                "stato": "errore",
                "messaggio": "Configurazione email incompleta",
                "variabili_mancanti": mancanti,
            }
        ), 500

    pubblicazione_test = {
        "titolo": "Test notifiche - News Agricoltura Campania",
        "categoria": "Test",
        "data_pubblicazione": datetime.now().date(),
        "ultimo_aggiornamento": datetime.now().date(),
        "testo_archivio": "Questo è un messaggio di prova del sistema di notifica.",
        "url": URL_ARCHIVIO,
    }

    try:
        invia_email_notifica(pubblicazione_test, "nuova")
        return jsonify(
            {
                "stato": "ok",
                "messaggio": "Email di prova inviata correttamente",
                "destinatario": EMAIL_DESTINATARIO,
            }
        )
    except Exception as e:
        return jsonify(
            {
                "stato": "errore",
                "messaggio": str(e),
            }
        ), 500


if __name__ == "__main__":
    prepara_database()

    thread = threading.Thread(target=ciclo_automatico, daemon=True)
    thread.start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
