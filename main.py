import logging
import os
import time

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types

# Logging setup
logging.basicConfig(level=logging.INFO)

# Load environment variables
TOKEN = os.getenv("TOKEN")
RENDER_WEB_SERVICE_NAME = os.getenv("YOUR_RENDER_WEB_SERVICE_NAME")

if not TOKEN or not RENDER_WEB_SERVICE_NAME:
    raise RuntimeError("Environment variables TOKEN or YOUR_RENDER_WEB_SERVICE_NAME are missing.")

# Webhook configuration
WEBHOOK_PATH = f"/bot/{TOKEN}"
WEBHOOK_URL = f"https://{RENDER_WEB_SERVICE_NAME}.onrender.com{WEBHOOK_PATH}"

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher(bot=bot)
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    logging.info("Starting application... checking webhook configuration.")
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set to: {WEBHOOK_URL}")
    else:
        logging.info("Webhook already set correctly.")

@app.get("/hourly-trigger")
async def hourly_trigger():
    logging.info(f"hourly-trigger")
    #await bot.send_message(chat_id, "Hourly message")
    return {"status": "sent"}

@app.on_event("shutdown")
async def on_shutdown():
    logging.info("Shutting down bot session.")
    await bot.session.close()

@dp.message_handler(commands=["start2"])
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    logging.info(f"/start2 command from {full_name} ({user_id}) at {time.asctime()}")
    await message.reply(f"Hello, {full_name}!")

@dp.message_handler()
async def message_handler(message: types.Message):
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    logging.info(f"Message from {full_name} ({user_id}) at {time.asctime()}: {message.text}")
    await message.reply("Message received!")

@app.post(WEBHOOK_PATH)
async def handle_webhook(request: Request):
    update = await request.json()
    logging.info(f"Received update: {update}")
    telegram_update = types.Update(**update)
    Dispatcher.set_current(dp)
    Bot.set_current(bot)
    await dp.process_update(telegram_update)
    return {"ok": True}

@app.get("/")
async def health_check():
    return {"status": "ok"}
