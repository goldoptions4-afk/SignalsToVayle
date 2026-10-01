import os
import re
import json
import logging
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHART_IMG_API_KEY = os.environ.get("CHART_IMG_API_KEY", "GhKjWUCZA61Lx0OwoNZvp8AhcLtTkWee702zMySE")
HOLDING_CHANNEL = int(os.environ.get("HOLDING_CHANNEL", "-1002083673417"))
KEVINGOLD_CHANNEL = int(os.environ.get("KEVINGOLD_CHANNEL", "-1001673250065"))
VIP_CHANNEL = int(os.environ.get("VIP_CHANNEL", "-1004347840465"))
STORIES_CHANNEL = int(os.environ.get("STORIES_CHANNEL", "0"))
HOLDING_ENABLED = os.environ.get("HOLDING_ENABLED", "true").strip().lower() != "false"
RAY_GOLD_URL = os.environ.get("RAY_GOLD_URL", "https://web-production-f54d0.up.railway.app")
WHATSAPP_URL = os.environ.get("WHATSAPP_URL", "https://web-production-6cec8d.up.railway.app")
# Second WhatsApp sender (new number, new group). Leave empty to disable.
WHATSAPP_URL_2 = os.environ.get("WHATSAPP_URL_2", "").rstrip("/")

# ─────────────────────────────────────────────
# RAYGOLDSIGNALS — send signal to MT5
# ─────────────────────────────────────────────

async def send_to_mt5(text):
    try:
        text = text.replace('`', '')
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{RAY_GOLD_URL}/new_signal",
                json={"text": text}
            )
            if r.status_code == 200:
                logger.info(f"✅ Signal sent to MT5: {r.json()}")
            else:
                logger.warning(f"⚠️ MT5 signal failed: {r.status_code}")
    except Exception as e:
        logger.error(f"❌ MT5 send error: {e}")

async def send_to_whatsapp(message, group=None, image_url=None, video_url=None,
                           voice_url=None, exclude=None, base_url=None):
    """Send message to one WhatsApp service (base_url, default = service 1)."""
    url = (base_url or WHATSAPP_URL).rstrip("/")
    try:
        payload = {"message": message}
        if group:
            payload["group"] = group
        if exclude:
            payload["exclude"] = exclude
        if image_url:
            payload["image_url"] = image_url
        if video_url:
            payload["video_url"] = video_url
        if voice_url:
            payload["voice_url"] = voice_url
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{url}/send", json=payload)
            if r.status_code == 200:
                if voice_url: media_note = " (with voice)"
                elif video_url: media_note = " (with video)"
                elif image_url: media_note = " (with image)"
                else: media_note = ""
                logger.info(f"✅ Sent to WhatsApp [{url}]{' → ' + group if group else ''}{media_note}")
            else:
                logger.warning(f"⚠️ WhatsApp send failed [{url}]: {r.status_code} {r.text}")
    except Exception as e:
        logger.error(f"❌ WhatsApp send error [{url}]: {e}")

async def send_to_all_whatsapp(message, image_url=None, video_url=None, voice_url=None, exclude=None):
    """Broadcast to BOTH WhatsApp services (number 1 and, if set, number 2)."""
    await send_to_whatsapp(message, image_url=image_url, video_url=video_url,
                           voice_url=voice_url, exclude=exclude, base_url=WHATSAPP_URL)
    if WHATSAPP_URL_2:
        await send_to_whatsapp(message, image_url=image_url, video_url=video_url,
                               voice_url=voice_url, exclude=exclude, base_url=WHATSAPP_URL_2)

# ─────────────────────────────────────────────
# IMAGE HOSTING — uploads chart bytes to RayGoldSignals so WhatsApp's
# bot (index.js) can fetch them by URL.
# ─────────────────────────────────────────────

async def host_image_for_whatsapp(image_bytes, content_type="image/jpeg"):
    if not image_bytes:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{RAY_GOLD_URL}/host_image",
                content=image_bytes,
                headers={"Content-Type": content_type}
            )
            if r.status_code == 200:
                url = r.json().get("url")
                logger.info(f"✅ Chart image hosted: {url}")
                return url
            else:
                logger.warning(f"⚠️ Image hosting failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Image hosting error: {e}")
    return None

# ─────────────────────────────────────────────
# CHART IMAGE
# ─────────────────────────────────────────────

async def fetch_chart_image():
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.chart-img.com/v2/tradingview/advanced-chart",
                headers={
                    "x-api-key": CHART_IMG_API_KEY,
                    "content-type": "application/json"
                },
                json={
                    "symbol": "OANDA:XAUUSD",
                    "interval": "5m",
                    "theme": "dark",
                    "width": 800,
                    "height": 500
                }
            )
            if r.status_code == 200:
                logger.info("✅ Chart image fetched from chart-img.com")
                return r.content
            else:
                logger.warning(f"⚠️ Chart-img failed: {r.status_code} {r.text[:100]}")
                return None
    except Exception as e:
        logger.error(f"❌ Chart fetch error: {e}")
        return None

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

STATE_FILE = "/tmp/gold_state.json"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"last_signal_direction": None, "last_signal_hash": None, "last_signal_time": 0}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

def is_duplicate_signal(text, state):
    import hashlib, time
    h = hashlib.md5(text.encode()).hexdigest()[:8]
    now = time.time()
    if h == state.get("last_signal_hash") and (now - state.get("last_signal_time", 0)) < 60:
        logger.info(f"Duplicate signal blocked: {h}")
        return True
    state["last_signal_hash"] = h
    state["last_signal_time"] = now
    return False

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def format_price(p):
    return f"{float(p):.2f}"

def extract_entry(text):
    range_match = re.search(
        r'([3-9][0-9]{2,3}(?:\.[0-9]+)?)\s*[-–]\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text
    )
    if range_match:
        p1 = float(range_match.group(1))
        p2 = float(range_match.group(2))
        return max(p1, p2), min(p1, p2)

    entry_match = re.search(
        r'(?:buy|sell|now|@|entry|limit)\s*[:\s]?\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)',
        text, re.IGNORECASE
    )
    if entry_match:
        p = float(entry_match.group(1))
        return p, p

    prices = re.findall(r'\b[3-9][0-9]{2,3}(?:\.[0-9]+)?\b', text)
    if prices:
        p = float(prices[0])
        return p, p

    return None, None

def extract_all_tps(text):
    tps = []
    for m in re.finditer(
        r'\btp\s*(?:\d{1,2}\s*)?[:\s]?\s*([3-9][0-9]{3}(?:\.[0-9]+)?)',
        text, re.IGNORECASE
    ):
        val = float(m.group(1))
        if val not in tps:
            tps.append(val)
    return tps

def extract_sl(text):
    sl_match = re.search(
        r'(?:sl|stop\s*loss|stoploss)[:\s🚫☹️]*\s*([3-9][0-9]{2,3}(?:\.[0-9]+)?)',
        text, re.IGNORECASE
    )
    if sl_match:
        return float(sl_match.group(1))
    return None

def get_direction(text):
    if re.search(r'\bsell\b', text, re.IGNORECASE):
        return "SELL"
    if re.search(r'\bbuy\b', text, re.IGNORECASE):
        return "BUY"
    if re.search(r'\bse\b', text, re.IGNORECASE):
        return "SELL"
    if re.search(r'\bbu\b', text, re.IGNORECASE):
        return "BUY"
    sl_match = re.search(r'\bsl\s*[:\s]*([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE)
    tp_match = re.search(r'\btp\s*[\d:\s]*([3-9][0-9]{2,3}(?:\.[0-9]+)?)', text, re.IGNORECASE)
    prices = re.findall(r'\b([3-9][0-9]{2,3}(?:\.[0-9]+)?)\b', text)
    if sl_match and tp_match and prices:
        sl = float(sl_match.group(1))
        tp = float(tp_match.group(1))
        first_price = float(prices[0])
        if tp < first_price and sl > first_price: return "SELL"
        if tp > first_price and sl < first_price: return "BUY"
    return None

def get_tp_number(text):
    m = re.search(r'tp\s*(\d)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None

# ─────────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────────

def is_new_signal(text):
    has_direction = bool(re.search(r'\b(buy|sell|bu|se)\b', text, re.IGNORECASE))
    has_tp = bool(re.search(r'\btp\s*\d{0,2}[^0-9]*([3-9][0-9]{3}(?:\.[0-9]+)?)', text, re.IGNORECASE))
    has_sl = bool(re.search(r'\bsl[^0-9]*([3-9][0-9]{3}(?:\.[0-9]+)?)', text, re.IGNORECASE))
    return has_direction and has_tp and has_sl

def is_tp_hit(text):
    has_tp_number = bool(re.search(r'\btp\s*\d', text, re.IGNORECASE))
    if not has_tp_number:
        return False
    if is_new_signal(text):
        return False
    return True

def is_secure_profits(text):
    if is_new_signal(text):
        return False
    has_tp4_plus = bool(re.search(r'\btp\s*[4-9]\b', text, re.IGNORECASE))
    has_close_msg = bool(re.search(
        r'\b(close\s*(our|the|all|now|trade)|secure\s*(your\s*)?profits?|take\s*profits?|let.s\s*close|touch\s*and|pips\s*✅)\b',
        text, re.IGNORECASE
    ))
    return has_tp4_plus or has_close_msg

def is_sl_hit(text):
    return bool(re.search(
        r'\b(sl\s*hit|stop\s*loss\s*hit|stopped\s*out|setup\s*invalid|invalid\s*setup|closing\s*the\s*trade|cut\s*(the\s*)?trade|missed)\b',
        text, re.IGNORECASE
    ))

# ─────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────

def apply_tp_override(tps, entry, direction):
    if len(tps) <= 1:
        return tps

    if direction == "BUY":
        tp1 = round(entry + 2, 2)
        tp2 = round(entry + 3, 2)
    else:
        tp1 = round(entry - 2, 2)
        tp2 = round(entry - 3, 2)

    result = [tp1, tp2]
    if len(tps) > 2:
        result += tps[2:]
    return result

def format_signal(text):
    direction = get_direction(text)
    if not direction:
        return None, None

    top_entry, bottom_entry = extract_entry(text)
    tps = extract_all_tps(text)
    sl = extract_sl(text)

    if top_entry is None:
        return None, None

    if direction == "BUY":
        entry_top = top_entry
        entry_bottom = round(top_entry - 5, 2)
        ref_entry = top_entry
    else:
        entry_top = round(bottom_entry + 5, 2)
        entry_bottom = bottom_entry
        ref_entry = bottom_entry

    tps = apply_tp_override(tps, ref_entry, direction)

    emoji = "🟢" if direction == "BUY" else "🔴"

    lines = [
        f"{direction} {emoji}",
        f"XAU/USD | GOLD",
        f"",
        f"ENTRY: {format_price(entry_top)} - {format_price(entry_bottom)}",
        f"",
    ]

    if tps:
        for i, tp in enumerate(tps, 1):
            lines.append(f"✅ TP{i} {format_price(tp)}")
        lines.append("")

    if sl:
        lines.append(f"🛑 SL {format_price(sl)}")

    lines.append("")
    lines.append("Use Appropriate Lot Sizes")

    return "\n".join(lines), direction

def format_tp_hit(text):
    tp_num = get_tp_number(text)

    if tp_num == 1:
        return (
            f"GOLD SMASHED TP1 ✅✅✅\n\n"
            f"Close your positions or move SL to Break Even 🔒\n\n"
            f"Want trades like this in your account automatically? Join our Gold Account Management 💰"
        )
    elif tp_num == 2:
        return (
            f"GOLD SMASHED TP2 ✅✅✅✅\n\n"
            f"Close remaining positions or move SL to Break Even 🔒\n\n"
            f"Stop watching trades, let us execute them for you. Join Gold Account Management 💰"
        )
    elif tp_num == 3:
        return (
            f"GOLD SMASHED TP3 ✅✅✅✅✅\n\n"
            f"Close all positions and lock in your profits 🔒\n\n"
            f"This is what Gold Account Management does for our clients, every single trade, automatically in your account. Join now 💰"
        )
    else:
        return format_secure_profits()

def format_secure_profits():
    return (
        f"💰 SECURE YOUR PROFITS!\n"
        f"XAU/USD | GOLD\n\n"
        f"Close your positions and bank those gains 🏆\n"
        f"This is the power of Kevin's Gold VIP 💎"
    )

def format_sl_hit():
    return (
        f"❌ Apologies, We Hit SL On This Trade\n"
        f"XAU/USD | GOLD\n\n"
        f"We will be looking for more setups 🔍"
    )

# ─────────────────────────────────────────────
# HANDLER
# ─────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if not message:
        return

    chat_id = message.chat.id

    # Freshness guard: never blast out stale messages after downtime.
    try:
        from datetime import datetime, timezone
        age_seconds = (datetime.now(timezone.utc) - message.date).total_seconds()
        if age_seconds > 300:
            logger.info(f"Skipping stale message ({int(age_seconds)}s old) from chat {chat_id}")
            return
    except Exception as e:
        logger.warning(f"Freshness check failed (continuing anyway): {e}")

    # ── STORIES CHANNEL: paused ───────────────────────────────────
    if STORIES_CHANNEL and chat_id == STORIES_CHANNEL:
        logger.info("Stories flow paused, ignoring stories channel post")
        return

    # Skip non-text media (stickers, docs, animations) everywhere, and
    # skip video/voice everywhere EXCEPT kevingoldsignals which forwards them.
    if message.document or message.sticker or message.animation:
        logger.info("Skipping non-text media message")
        return
    if message.video and chat_id != KEVINGOLD_CHANNEL:
        logger.info("Skipping video, only kevingoldsignals channel supports video")
        return
    if message.voice and chat_id != KEVINGOLD_CHANNEL:
        logger.info("Skipping voice, only kevingoldsignals channel supports voice")
        return

    text = None
    if message.text:
        text = message.text.strip()
    elif message.photo and message.caption:
        text = message.caption.strip()
    elif message.video and message.caption:
        text = message.caption.strip()
    elif message.voice and message.caption:
        text = message.caption.strip()
    elif message.voice and not message.caption:
        text = ""
    elif message.photo and not message.caption:
        logger.info("Skipping photo with no caption")
        return
    elif message.video and not message.caption:
        logger.info("Skipping video with no caption")
        return
    else:
        return

    logger.info(f"Message from chat {chat_id}")

    # ── CHANNEL 1: kevingoldsignals → BOTH WhatsApp services ─────
    if chat_id == KEVINGOLD_CHANNEL:
        # [TG] tag anywhere in the message → Telegram only, skip WhatsApp entirely.
        if re.search(r'\[TG\]', text, re.IGNORECASE):
            cleaned = re.sub(r'\s*\[TG\]\s*', ' ', text, flags=re.IGNORECASE).strip()
            logger.info(f"🔒 [TG] tag detected, Telegram-only: {cleaned[:80]}")
            return

        # [NP] tag or the word "premium" → exclude PREMIUM GOLD GROUP
        # (harmless no-op while that group is not in any sender's target list).
        exclude_group = None
        if re.search(r'\[NP\]', text, re.IGNORECASE):
            exclude_group = "PREMIUM GOLD GROUP"
            text = re.sub(r'\s*\[NP\]\s*', ' ', text, flags=re.IGNORECASE).strip()
            logger.info("🚫 [NP] tag, excluding PREMIUM GOLD GROUP")
        elif re.search(r'\bpremium\b', text, re.IGNORECASE):
            exclude_group = "PREMIUM GOLD GROUP"
            logger.info("🚫 Word 'premium' detected, excluding PREMIUM GOLD GROUP")

        wa_text = text

        logger.info(f"📤 kevingoldsignals → WhatsApp services: {text[:80]}")
        image_url = None
        video_url = None
        voice_url = None
        photo = None
        if message.photo:
            photo = message.photo[-1]
        elif message.effective_attachment and hasattr(message.effective_attachment, '__iter__'):
            try:
                photo = list(message.effective_attachment)[-1]
            except:
                pass
        elif hasattr(message, 'forward_origin') and message.forward_origin:
            if message.photo:
                photo = message.photo[-1]

        if photo:
            try:
                photo_file = await context.bot.get_file(photo.file_id)
                image_url = photo_file.file_path
                logger.info(f"📷 Image detected: {image_url}")
            except Exception as e:
                logger.warning(f"⚠️ Could not get image file: {e}")
        elif message.video:
            try:
                video_file = await context.bot.get_file(message.video.file_id)
                video_url = video_file.file_path
                logger.info(f"🎥 Video detected: {video_url} ({message.video.file_size} bytes)")
            except Exception as e:
                logger.warning(f"⚠️ Could not get video file (possibly over Telegram's 20MB bot API limit): {e}")
        elif message.voice:
            try:
                voice_file = await context.bot.get_file(message.voice.file_id)
                voice_url = voice_file.file_path
                logger.info(f"🎙️ Voice detected: {voice_url} ({message.voice.file_size} bytes)")
            except Exception as e:
                logger.warning(f"⚠️ Could not get voice file: {e}")
        else:
            logger.info("📝 No image, video, or voice found in message")

        # Send to BOTH WhatsApp services (number 1 group + number 2 group)
        await send_to_all_whatsapp(wa_text, image_url=image_url, video_url=video_url,
                                   voice_url=voice_url, exclude=exclude_group)
        return

    # ── CHANNEL: testingtradesfiltered → MT5 only ─────────────────
    if chat_id == VIP_CHANNEL:
        logger.info(f"📤 testingtradesfiltered → MT5 only: {text[:80]}")
        await send_to_mt5(text)
        return

    # ── HOLDING CHANNEL: filter & reformat ────────────────────────
    if chat_id != HOLDING_CHANNEL:
        return

    if not HOLDING_ENABLED:
        logger.info("Holding flow paused (HOLDING_ENABLED=false)")
        return

    logger.info(f"📥 RECEIVED: {text[:150]}")

    state = load_state()
    output = None
    is_tp_hit_msg = False
    is_sl_hit_msg = False

    if is_new_signal(text):
        if is_duplicate_signal(text, state):
            save_state(state)
            return
        output, direction = format_signal(text)
        if output:
            state["last_signal_direction"] = direction
            save_state(state)
            logger.info(f"Detected: NEW SIGNAL ({direction})")

    elif is_tp_hit(text):
        output = format_tp_hit(text)
        is_tp_hit_msg = True
        logger.info("Detected: TP HIT")

    elif is_secure_profits(text):
        output = format_secure_profits()
        logger.info("Detected: SECURE PROFITS")

    elif is_sl_hit(text):
        output = format_sl_hit()
        is_sl_hit_msg = True
        logger.info("Detected: SL HIT")

    else:
        logger.info(f"⏭️ SKIPPED, no pattern matched: {text[:80]}")
        return

    if output:
        chart_bytes = None
        if is_new_signal(text):
            chart_bytes = await fetch_chart_image()

        whatsapp_image_url = None
        if chart_bytes:
            whatsapp_image_url = await host_image_for_whatsapp(chart_bytes, "image/jpeg")

        if chart_bytes:
            from io import BytesIO
            await context.bot.send_photo(
                chat_id=VIP_CHANNEL,
                photo=BytesIO(chart_bytes),
                caption=output
            )
            logger.info("Message + chart sent to VIP channel ✅")
        else:
            await context.bot.send_message(
                chat_id=VIP_CHANNEL,
                text=output
            )
            logger.info("Message sent to VIP channel (no chart) ✅")

        # TP/SL hits go to WhatsApp via app.py (profit card flow), skip here.
        if is_tp_hit_msg or is_sl_hit_msg:
            logger.info("TP/SL hit, skipping WhatsApp send (app.py sends MT5-driven TP/SL messages)")
        else:
            await send_to_whatsapp(output, group="Dummy group testing", image_url=whatsapp_image_url)
            await send_to_whatsapp(output, group="PREMIUM GOLD GROUP", image_url=whatsapp_image_url)
            logger.info("Message sent to WhatsApp holding targets ✅")

        if is_new_signal(text):
            await send_to_mt5(output)
            logger.info("Formatted signal sent to MT5 ✅")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    logger.info("Bot started, listening for signals...")
    app.run_polling(allowed_updates=["channel_post", "message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
