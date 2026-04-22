from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ========= CONFIG =========

DAY_NAMES = [
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
    "domenica",
]

GRID_CALL_URL = (
    "https://gestioneorari.didattica.unimib.it/PortaleStudentiUnimib/grid_call.php"
)

# ========= MODELLI =========


class Lesson(BaseModel):
    day: str        # "lunedì"
    date: str       # "27/04"
    start: str      # "08:30"
    end: str        # "11:30"
    name: str
    room: str
    cancelled: bool


class OrarioResponse(BaseModel):
    course_code: str
    course_title: str
    week_label: str        # "27-04-2026"
    updated_at: str | None
    lessons: List[Lesson]


# ========= APP FASTAPI =========

app = FastAPI(title="Orario E311PV – proxy EasyCourse")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # in produzione restringi al tuo dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========= FUNZIONI DI SUPPORTO =========


def parse_lessons_from_json(data: dict) -> list[Lesson]:
    """
    Converte il JSON di grid_call.php in una lista di Lesson.
    Usa il campo 'celle', che contiene una voce per ogni lezione.
    """
    celle = data.get("celle", [])
    lessons: list[Lesson] = []

    for cell in celle:
        tipo = (cell.get("tipo") or "").strip()
        # Consideriamo solo le lezioni vere e proprie
        if tipo and tipo.lower() != "lezione":
            continue

        nome = (cell.get("nome_insegnamento") or "").strip()
        aula = (cell.get("aula") or "").strip() or "Aula non indicata"
        nome_giorno = (cell.get("nome_giorno") or "").strip().lower()
        data_str = cell.get("data") or ""
        ora_inizio = (cell.get("ora_inizio") or "").strip()
        ora_fine = (cell.get("ora_fine") or "").strip()
        annullato_flag = (cell.get("Annullato") or "0").strip()

        cancelled = annullato_flag == "1"

        if not nome or not nome_giorno or not data_str or not ora_inizio or not ora_fine:
            continue

        # data nel formato "dd-mm-yyyy" -> "dd/mm"
        try:
            d = datetime.strptime(data_str, "%d-%m-%Y").date()
            date_label = d.strftime("%d/%m")
        except ValueError:
            date_label = data_str

        lessons.append(
            Lesson(
                day=nome_giorno,
                date=date_label,
                start=ora_inizio,
                end=ora_fine,
                name=nome,
                room=aula,
                cancelled=cancelled,
            )
        )

    lessons.sort(key=lambda x: (DAY_NAMES.index(x.day), x.start))
    return lessons


# ========= ENDPOINT API =========


@app.get("/api/orario", response_model=OrarioResponse)
def get_orario(
    date: str | None = Query(None, description="dd-mm-yyyy; se vuoto usa oggi")
):
    """
    Restituisce l'orario del corso E311PV per la settimana che contiene 'date'.
    Se 'date' è omessa, usa la data di oggi.
    """
    if date is None:
        today = datetime.today().date()
    else:
        try:
            today = datetime.strptime(date, "%d-%m-%Y").date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Formato data atteso: dd-mm-yyyy"
            )

    # lunedì della settimana della data richiesta
    weekday = today.weekday()  # lun = 0
    monday = today - timedelta(days=weekday)
    date_str = monday.strftime("%d-%m-%Y")

    # payload copiato da Network → Form Data per grid_call.php
    payload = {
        "view": "easycourse",
        "include": "corso",
        "txtcurr": "2 - PERCORSO COMUNE",
        "anno": "2025",
        "corso": "E311PV",
        "anno2[]": "GGG|2",
        "_lang": "it",
        "highlighted_date": "0",
        "all_events": "0",
        "date": date_str,
        "ar_codes": "",
        "ar_select": "",
    }

    try:
        r = requests.post(GRID_CALL_URL, data=payload, timeout=15)
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502, detail=f"Errore nel contattare EasyCourse: {e}"
        )

    if r.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"EasyCourse ha risposto {r.status_code}"
        )

    try:
        data = r.json()
    except ValueError:
        raise HTTPException(
            status_code=502, detail="Risposta EasyCourse non valida (JSON)"
        )

    # meta base dalla testata JSON
    week_label = data.get("first_day", date_str)
    course_code = data.get("cds", "E311PV")
    course_title = f"{course_code} - Artificial Intelligence"
    updated_at = None  # JSON non sembra includere un campo aggiornamento

    lessons = parse_lessons_from_json(data)

    return OrarioResponse(
        course_code=course_code,
        course_title=course_title,
        week_label=week_label,
        updated_at=updated_at,
        lessons=lessons,
    )


# ========= FRONTEND =========


@app.get("/", response_class=HTMLResponse)
def read_root():
    """
    Serviamo static/index.html alla root.
    """
    index_path = Path("static/index.html")
    if not index_path.exists():
        return HTMLResponse(
            "<h1>Manca static/index.html</h1>", status_code=500
        )
    return index_path.read_text(encoding="utf-8")


# Serviamo gli asset statici (CSS/JS/icone) sotto /static
app.mount("/static", StaticFiles(directory="static"), name="static")