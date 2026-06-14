import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime

import os
from dotenv import load_dotenv
load_dotenv()  # loads .env locally; on Render use Environment Variables in dashboard

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
TOKEN = os.getenv("TOKEN")
RENDER_WEB_SERVICE_NAME = os.getenv("YOUR_RENDER_WEB_SERVICE_NAME")

if not TOKEN or not RENDER_WEB_SERVICE_NAME:
    raise RuntimeError("Environment variables TOKEN or YOUR_RENDER_WEB_SERVICE_NAME are missing.")

WEBHOOK_PATH = f"/bot/{TOKEN}"
WEBHOOK_URL = f"https://{RENDER_WEB_SERVICE_NAME}.onrender.com{WEBHOOK_PATH}"

# ---------------------------------------------------------------------------
# TextService
# ---------------------------------------------------------------------------

def remove_last_line(text: str) -> str:
    idx = text.rfind("\n")
    return text[:idx] if idx != -1 else text


class TextService:
    DELIMITER = "📆 "

    def __init__(self, daily_book_path: str, bot_username: str = ""):
        self.daily_book_path = daily_book_path
        self.bot_username = bot_username
        self.texts: list[str] = []
        self._parse_book()

    def _parse_book(self) -> None:
        import zipfile
        import base64
        import io
        path = Path(self.daily_book_path)
        if not path.exists():
            log.error("Daily-book file not found: %s", self.daily_book_path)
            return
        try:
            if path.suffix == ".b64":
                # base64-encoded zip — used for Render Secret Files (binary not supported)
                b64 = path.read_text(encoding="utf-8")
                zip_bytes = base64.b64decode(b64)
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    name = zf.namelist()[0]
                    content = zf.read(name).decode("utf-8")
            elif path.suffix == ".zip":
                with zipfile.ZipFile(path, "r") as zf:
                    name = zf.namelist()[0]
                    content = zf.read(name).decode("utf-8")
            else:
                content = path.read_text(encoding="utf-8")

            self.texts = [
                f"{self.DELIMITER}{s}"
                for s in content.split(self.DELIMITER)
                if s
            ]
            log.info("Daily book loaded: %d entries", len(self.texts))
        except Exception as exc:
            log.error("Error reading daily-book: %s", exc)

    @staticmethod
    def _is_leap_year(year: int) -> bool:
        return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)

    def _day_index(self) -> int:
        today = datetime.now()
        day_of_year = today.timetuple().tm_yday
        if not self._is_leap_year(today.year) and today.month >= 3:
            day_of_year += 1
        return day_of_year - 1  # 0-based

    def get_daily_text(self) -> str:
        try:
            entry = self.texts[self._day_index()]
            blocks = entry.split("\n")
            return self._format(blocks)
        except IndexError:
            log.error("No daily-text entry for today.")
            return "No entry available for today's date."

    def get_daily_text_preview(self, lines: int = 3) -> str:
        """First `lines` non-empty lines joined with ' | ' — for logging."""
        try:
            entry = self.texts[self._day_index()]
            non_empty = [ln for ln in entry.split("\n") if ln.strip()]
            return " | ".join(non_empty[:lines])
        except IndexError:
            return "(no entry)"

    def _format(self, blocks: list[str]) -> str:
        if len(blocks) < 4:
            return "\n".join(blocks)
        date  = self._bold(blocks[0])
        topic = self._bold(blocks[1])
        quote = self._italic(blocks[2])
        icon  = f'<a href="https://t.me/{self.bot_username}">🍀</a>' if self.bot_username else "🍀"
        main_content = "\n\n".join(blocks[3:-2])
        footer = self._italic(blocks[-2])
        return "\n\n".join([date, topic, quote, main_content, footer, icon])

    @staticmethod
    def _bold(text: str) -> str:
        return f"<b>{text}</b>"

    @staticmethod
    def _italic(text: str) -> str:
        return f"<i>{text}</i>"


# ---------------------------------------------------------------------------
# SubscriberService
# format per line: HH:MM;chat_id;message_thread_id
# message_thread_id — int, 0 or empty means no topic
# ---------------------------------------------------------------------------
from dataclasses import dataclass
from datetime import time as dtime, timezone
from typing import Optional


@dataclass
class Subscriber:
    chat_id: int
    send_time: dtime
    message_thread_id: Optional[int]  # None means regular chat, no topic


class SubscriberService:
    TIME_FORMATTER = "%H:%M"

    def __init__(self, file_path: str):
        self.file_path = file_path

    def get_subscribers(self) -> list:
        result = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(";")
                    if len(parts) < 2:
                        continue
                    try:
                        send_time  = datetime.strptime(parts[0], self.TIME_FORMATTER).time()
                        chat_id    = int(parts[1])
                        thread_raw = parts[2].strip() if len(parts) > 2 else ""
                        thread_id  = int(thread_raw) if thread_raw and thread_raw != "0" else None
                        result.append(Subscriber(chat_id, send_time, thread_id))
                    except (ValueError, IndexError) as e:
                        log.warning("Skipping bad subscriber line %r: %s", line, e)
        except IOError:
            log.warning("Subscribers file not found: %s", self.file_path)
        return result

    def get_due_subscribers(self) -> list:
        """Returns subscribers whose sent hour matches the current hour in SERVER_TZ timezone.
        Hours in the subscriber file must be set in SERVER_TZ time."""
        import zoneinfo
        tz_name = os.getenv("SERVER_TZ", "UTC")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now_hour = datetime.now(tz).hour
        return [s for s in self.get_subscribers() if s.send_time.hour == now_hour]


# ---------------------------------------------------------------------------
# Service init
# ---------------------------------------------------------------------------
DAILY_BOOK_PATH  = os.getenv("DAILY_BOOK_PATH", "daily.zip.b64")
BOT_USERNAME     = os.getenv("BOT_USERNAME", "test-bot")
SUBSCRIBERS_PATH = os.getenv("SUBSCRIBERS_PATH", "subscribers")

text_service       = TextService(daily_book_path=DAILY_BOOK_PATH, bot_username=BOT_USERNAME)
subscriber_service = SubscriberService(file_path=SUBSCRIBERS_PATH)

# ---------------------------------------------------------------------------
# aiogram v3 setup
# ---------------------------------------------------------------------------
bot = Bot(token=TOKEN)
dp  = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------------
# Handlers  (aiogram v3 style)
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def start_handler(message: types.Message):
    full_name = message.from_user.full_name
    log.info("/start from %s (%s)", full_name, message.from_user.id)
    await message.reply(
        f"Hello, {full_name}!\n\nUse /now to get today's daily text."
    )


@router.message(Command("now"))
async def now_handler(message: types.Message):
    log.info("/now from %s (%s)", message.from_user.full_name, message.from_user.id)
    text = text_service.get_daily_text()
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message()
async def fallback_handler(message: types.Message):
    log.info(
        "Message from %s (%s) at %s: %s",
        message.from_user.full_name,
        message.from_user.id,
        time.asctime(),
        message.text,
    )
    await message.reply("Unknown command. Try /now.")


# ---------------------------------------------------------------------------
# FastAPI lifespan (replaces deprecated on_event)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    log.info("Starting application — checking webhook configuration.")
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL)
        log.info("Webhook set to: %s", WEBHOOK_URL)
    else:
        log.info("Webhook already set correctly.")

    tz_name = os.getenv("SERVER_TZ", "UTC")
    log.info("Timezone: %s — hours in subscribers file must be in %s", tz_name, tz_name)
    preview = text_service.get_daily_text_preview(lines=3)
    log.info("=== Daily text preview (startup) === %s", preview)

    yield  # app is running

    # --- shutdown ---
    log.info("Shutting down — closing bot session.")
    await bot.session.close()


# docs enabled only when TRIGGER_SECRET is not set (local dev)
_local = not os.getenv("TRIGGER_SECRET")
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _local else None,
    redoc_url="/redoc" if _local else None,
    openapi_url="/openapi.json" if _local else None,
)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_last_sent_hour: int = -1  # tracks the last hour a broadcast was sent

@app.get("/hourly-trigger")
async def hourly_trigger(secret: str = ""):
    global _last_sent_hour
    from fastapi import HTTPException
    import zoneinfo
    if not secret or secret != os.getenv("TRIGGER_SECRET", ""):
        raise HTTPException(status_code=403, detail="Forbidden")

    tz = zoneinfo.ZoneInfo(os.getenv("SERVER_TZ", "UTC"))
    current_hour = datetime.now(tz).hour

    if current_hour == _last_sent_hour:
        log.info("Already sent this hour (%d), skipping.", current_hour)
        return {"status": "skipped", "reason": "already sent this hour"}

    preview = text_service.get_daily_text_preview(lines=3)
    log.info("=== Hourly trigger fired === %s", preview)

    all_subscribers = subscriber_service.get_subscribers()
    due = subscriber_service.get_due_subscribers()
    log.info("Current hour: %02d — %d/%d subscriber(s) scheduled now:",
             current_hour, len(due), len(all_subscribers))
    for sub in all_subscribers:
        marker = ">>> SEND" if sub.send_time.hour == current_hour else "    wait"
        log.info("  %s  chat_id=%-20s thread=%-6s at %s",
                 marker, sub.chat_id, sub.message_thread_id or "-", sub.send_time.strftime("%H:%M"))

    text = text_service.get_daily_text()
    sent, failed = 0, 0
    for sub in due:
        try:
            await bot.send_message(
                chat_id=sub.chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                message_thread_id=sub.message_thread_id,  # None is ignored by aiogram
            )
            log.info("Sent to chat_id=%s thread=%s", sub.chat_id, sub.message_thread_id)
            sent += 1
        except Exception as e:
            log.error("Failed chat_id=%s thread=%s: %s", sub.chat_id, sub.message_thread_id, e)
            failed += 1

    _last_sent_hour = current_hour
    return {"status": "ok", "preview": preview, "sent": sent, "failed": failed}


@app.post(WEBHOOK_PATH, include_in_schema=False)
async def handle_webhook(request: Request):
    data = await request.json()
    log.debug("Received update: %s", data)
    update = types.Update(**data)
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}


@app.get("/")
async def health_check():
    return {"status": "ok"}


@app.get("/debug/now")
async def debug_now(secret: str = ""):
    """Local dev only — daily text preview in the browser."""
    from fastapi import HTTPException
    if not secret or secret != os.getenv("TRIGGER_SECRET", ""):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "preview": text_service.get_daily_text_preview(lines=5),
        "subscribers_due": len(subscriber_service.get_due_subscribers()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)