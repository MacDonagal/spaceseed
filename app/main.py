from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from . import c411
from .autograb import save_grab
from .config import settings
from .database import SessionLocal, init_db
from .models import Grab, Opportunity, ScanRun
from .qbittorrent import QBittorrentError, get_qbittorrent, swarm_health
from .scanner import scan_once
from .security import basic_auth_valid, is_cross_origin
from .telegram import TelegramError, send_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not settings.spaceseed_password:
        logger.warning("SPACESEED_PASSWORD est vide : l'interface est accessible sans authentification")
    scheduler.add_job(
        scan_once,
        "interval",
        minutes=settings.scan_interval_minutes,
        id="spaceseed_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    logger.info(
        "SpaceSeed démarré | scan toutes les %d minutes",
        settings.scan_interval_minutes,
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def render(request: Request, template: str, **context) -> Response:
    return templates.TemplateResponse(request, template, {"app_name": settings.app_name, **context})


def grid_row(opportunity: Opportunity) -> dict:
    return {
        "id": opportunity.id,
        "title": opportunity.title,
        "score": opportunity.score,
        "seeders": opportunity.seeders,
        "leechers": opportunity.leechers,
        "demand_ratio": round(opportunity.leechers / max(opportunity.seeders, 1), 2),
        "size_gb": round(opportunity.size_bytes / 1024 ** 3, 2),
        "freeleech": opportunity.freeleech,
        "category": opportunity.category or "",
        "last_seen": opportunity.last_seen_at.strftime("%H:%M:%S UTC"),
        "download_available": bool(opportunity.download_url),
    }


@app.middleware("http")
async def protect_interface(request: Request, call_next):
    # /health reste ouvert pour le healthcheck Docker.
    if request.url.path == "/health":
        return await call_next(request)

    if settings.spaceseed_password and not basic_auth_valid(
        request.headers.get("authorization"),
        settings.spaceseed_username,
        settings.spaceseed_password,
    ):
        return Response(
            "Authentification requise",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="SpaceSeed", charset="UTF-8"'},
        )

    # Le mot de passe ne suffit pas : le navigateur le rejoue aussi pour
    # les formulaires envoyés depuis un autre site.
    if request.method == "POST" and is_cross_origin(request.headers.get("origin"), request.headers.get("host")):
        return Response("Origine refusée", status_code=403)

    return await call_next(request)


@app.exception_handler(QBittorrentError)
async def qbittorrent_error(_request: Request, exc: QBittorrentError):
    return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.get("/")
def index(request: Request):
    with SessionLocal() as db:
        return render(
            request,
            "index.html",
            total=db.scalar(select(func.count()).select_from(Opportunity)),
            freeleech=db.scalar(
                select(func.count()).select_from(Opportunity).where(Opportunity.freeleech.is_(True))
            ),
            last_scan=db.scalar(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(1)),
            top=db.scalars(select(Opportunity).order_by(desc(Opportunity.score)).limit(5)).all(),
        )


@app.get("/opportunities")
def opportunities(request: Request):
    with SessionLocal() as db:
        rows = db.scalars(
            select(Opportunity).order_by(desc(Opportunity.score), desc(Opportunity.leechers))
        ).all()
        return render(request, "opportunities.html", rows=[grid_row(row) for row in rows])


@app.get("/history")
def history(request: Request):
    with SessionLocal() as db:
        return render(
            request,
            "history.html",
            runs=db.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(100)).all(),
            grabs=db.scalars(select(Grab).order_by(desc(Grab.grabbed_at)).limit(50)).all(),
            auto_grab_enabled=settings.auto_grab_enabled,
            c411=c411.summary(),
        )


@app.post("/scan")
def manual_scan():
    result = scan_once()
    target = "/history" if result["status"] == "error" else "/opportunities"
    return RedirectResponse(url=target, status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}


@app.post("/download/{opportunity_id}")
def download_opportunity(opportunity_id: int):
    with SessionLocal() as db:
        opportunity = db.get(Opportunity, opportunity_id)

    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunité introuvable")
    if not opportunity.download_url:
        raise HTTPException(status_code=400, detail="URL de téléchargement indisponible")

    reason = c411.download_block_reason(opportunity.size_bytes, opportunity.freeleech)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    get_qbittorrent().add_from_url(opportunity.download_url)
    # Enregistré pour que l'envoi automatique ne le renvoie pas.
    save_grab(opportunity, source="manual", status="sent")
    # Le titre plutôt que l'URL, qui contient la clé API ou le passkey.
    logger.info("Torrent envoyé à qBittorrent | id=%d | titre=%s", opportunity_id, opportunity.title)
    return RedirectResponse(url="/opportunities?sent=1", status_code=303)


@app.get("/qbittorrent/test")
def test_qbittorrent():
    return {"status": "ok", "version": get_qbittorrent().version()}


@app.post("/telegram/test")
def test_telegram():
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
    try:
        send_message("✅ SpaceSeed est bien connecté à Telegram")
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"status": "ok"}


@app.get("/api/qbittorrent/torrents")
def qbittorrent_torrents():
    now = int(time.time())
    torrents = get_qbittorrent().list_torrents()
    for torrent in torrents:
        age = max(now - torrent["added_on"], 0) if torrent["added_on"] else 0
        health_code, label = swarm_health(torrent, age)
        torrent.update(age_seconds=age, health=health_code, health_label=label)
    return {"status": "ok", "generated_at": now, "torrents": torrents}
