from datetime import date, datetime, timedelta
import re
from typing import Literal
import unicodedata
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# Third year, first semester 2026/27. Both feeds remain live: only the
# university schedule APIs supply dates, times, and rooms.
ACADEMIC_YEAR = "2026"
UNIMIB_URL = "https://gestioneorari.didattica.unimib.it/PortaleStudentiUnimib/grid_call.php"
UNIMI_URL = "https://orari-be.divsi.unimi.it/AgendaWeb/Orario/grid_call.php"
ROME = ZoneInfo("Europe/Rome")
DAY_NAMES = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì"]

SOURCES = {
    "Bicocca": {
        "weekdays": {1, 3},
        "courses": ("statistical modelling", "statistical modeling", "information retrieval and recommender systems"),
        "blue_courses": ("information retrieval and recommender systems",),
    },
    "Statale": {
        "weekdays": {0, 2, 4},
        "courses": ("brain modelling", "brain modeling", "data mining and knowledge extraction"),
        "blue_courses": ("data mining and knowledge extraction",),
    },
}


class Lesson(BaseModel):
    day: str
    date: str
    start: str
    end: str
    name: str
    room: str
    institution: Literal["Bicocca", "Statale"]
    track: Literal["all", "track1"]
    cancelled: bool


class OrarioResponse(BaseModel):
    course_code: str
    course_title: str
    week_label: str
    updated_at: str
    source_status: dict[str, str]
    lessons: list[Lesson]


class ScheduleSourceError(Exception):
    pass


app = FastAPI(title="Orario AI interateneo")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def parse_date(value: str) -> date | None:
    for date_format in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except (TypeError, ValueError):
            continue
    return None


def clean_time(value: str) -> str:
    match = re.match(r"\s*(\d{1,2}:\d{2})", value or "")
    return match.group(1).zfill(5) if match else ""


def is_selected_lesson(name: str, source: str) -> bool:
    normalized_name = normalize(name)
    return any(normalize(alias) in normalized_name for alias in SOURCES[source]["courses"])


def parse_source_lessons(data: dict, source: Literal["Bicocca", "Statale"]) -> list[Lesson]:
    lessons = []
    source_config = SOURCES[source]

    for cell in data.get("celle", []):
        name = (cell.get("nome_insegnamento") or cell.get("name_original") or "").strip()
        if not name or not is_selected_lesson(name, source):
            continue

        class_date = parse_date(cell.get("data", ""))
        if class_date is None or class_date.weekday() not in source_config["weekdays"]:
            continue

        lesson_type = str(cell.get("tipo") or "lezione").strip().casefold()
        if lesson_type and lesson_type != "lezione":
            continue

        start = clean_time(cell.get("ora_inizio", ""))
        end = clean_time(cell.get("ora_fine", ""))
        if not start or not end:
            time_range = (cell.get("orario") or "").split("-")
            if len(time_range) == 2:
                start, end = clean_time(time_range[0]), clean_time(time_range[1])
        if not start or not end:
            continue

        blue = any(normalize(alias) in normalize(name) for alias in source_config["blue_courses"])
        lessons.append(
            Lesson(
                day=DAY_NAMES[class_date.weekday()],
                date=class_date.strftime("%d/%m"),
                start=start,
                end=end,
                name=name,
                room=(cell.get("aula") or "").strip() or "Aula non indicata",
                institution=source,
                track="track1" if blue else "all",
                cancelled=str(cell.get("Annullato", "0")).strip() == "1",
            )
        )

    return lessons


def fetch_bicocca(monday: date) -> list[Lesson]:
    payload = {
        "view": "easycourse",
        "include": "corso",
        "txtcurr": "3 - PERCORSO COMUNE",
        "anno": ACADEMIC_YEAR,
        "corso": "E311PV",
        "anno2[]": "GGG|3",
        "_lang": "it",
        "highlighted_date": "0",
        "all_events": "0",
        "date": monday.strftime("%d-%m-%Y"),
        "ar_codes": "",
        "ar_select": "",
    }
    try:
        response = requests.post(UNIMIB_URL, data=payload, timeout=18)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ScheduleSourceError(f"Bicocca: {error}") from error
    return parse_source_lessons(data, "Bicocca")


def fetch_statale(monday: date) -> list[Lesson]:
    params = {
        "view": "easycourse",
        "include": "corso",
        "anno": ACADEMIC_YEAR,
        "corso": "F3A",
        "anno2[]": "F3A-0|3",
        "txtcurr": "3 - Unico",
        "_lang": "it",
        "all_events": "0",
        "date": monday.strftime("%d-%m-%Y"),
        "ar_codes": "",
        "ar_select": "",
    }
    try:
        response = requests.get(UNIMI_URL, params=params, timeout=18)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ScheduleSourceError(f"Statale: {error}") from error
    return parse_source_lessons(data, "Statale")


def format_week(monday: date) -> str:
    friday = monday + timedelta(days=4)
    if monday.month == friday.month:
        return f"{monday.day:02d}/{monday.month:02d} – {friday.day:02d}/{friday.month:02d}/{friday.year}"
    return f"{monday.day:02d}/{monday.month:02d} – {friday.day:02d}/{friday.month:02d}/{friday.year}"


@app.get("/api/orario", response_model=OrarioResponse)
def get_orario(
    response: Response,
    date_param: str | None = Query(None, alias="date", description="dd-mm-yyyy; default: oggi"),
):
    """Unisce le lezioni selezionate dai feed live delle due università."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if date_param is None:
        selected_date = datetime.now(ROME).date()
    else:
        selected_date = parse_date(date_param)
        if selected_date is None:
            raise HTTPException(status_code=400, detail="Formato data atteso: dd-mm-yyyy")

    monday = selected_date - timedelta(days=selected_date.weekday())
    source_status: dict[str, str] = {}
    lessons: list[Lesson] = []
    failures: list[str] = []

    for source, fetch in (("Bicocca", fetch_bicocca), ("Statale", fetch_statale)):
        try:
            lessons.extend(fetch(monday))
            source_status[source] = "aggiornato"
        except ScheduleSourceError as error:
            source_status[source] = "non disponibile"
            failures.append(str(error))

    if len(failures) == len(source_status):
        raise HTTPException(status_code=502, detail="; ".join(failures))

    lessons.sort(key=lambda item: (DAY_NAMES.index(item.day), item.start, item.institution))
    return OrarioResponse(
        course_code="E311PV · F3A",
        course_title="Artificial Intelligence · terzo anno",
        week_label=format_week(monday),
        updated_at=datetime.now(ROME).isoformat(timespec="minutes"),
        source_status=source_status,
        lessons=lessons,
    )



@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="public", html=True), name="public")
