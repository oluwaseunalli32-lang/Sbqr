"""QR Code Bot: turns text, website links and contact details into QR codes.

Runs with long polling. The bot token is read from the BOT_TOKEN environment
variable. User content is processed in memory and is never stored or logged.
"""

import asyncio
import io
import ipaddress
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Logging (httpx logs full request URLs, which contain the bot token, so it
# is silenced to WARNING on purpose)
# --------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("qr-bot")

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
MAX_TEXT_LENGTH = 1000
MAX_LINK_LENGTH = 1000
RATE_LIMIT_COUNT = 8  # QR codes per user...
RATE_LIMIT_WINDOW = 60  # ...per this many seconds

SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "buff.ly",
    "rebrand.ly", "cutt.ly", "shorturl.at", "tiny.cc", "rb.gy", "t.ly",
    "lnkd.in", "s.id", "v.gd", "shorte.st", "bl.ink",
}

HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z][a-z0-9-]{1,62}$")
PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{6,25}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# --------------------------------------------------------------------------
# Messages and keyboards
# --------------------------------------------------------------------------
WELCOME = (
    "Welcome! I turn text, website links and contact details into QR codes.\n\n"
    "Choose what you want to encode, or just send me some text."
)

HELP = (
    "How to use this bot:\n\n"
    "Text: send any text (up to 1000 characters) and get a QR code.\n"
    "Website link: send an https:// link. Short links and plain http links "
    "are not supported.\n"
    "Contact card: I ask for a name, phone number and optional email, then "
    "make a QR code that adds the contact to a phone.\n\n"
    "Commands:\n"
    "/start - open the menu\n"
    "/cancel - stop the current step\n"
    "/help - show this message\n\n"
    "Privacy: what you send is used only to make your QR code. Nothing is "
    "saved.\n"
    "Safety: only scan QR codes from sources you trust."
)

MENU_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("Text", callback_data="mode_text"),
            InlineKeyboardButton("Website link", callback_data="mode_link"),
        ],
        [InlineKeyboardButton("Contact card", callback_data="mode_contact")],
    ]
)
AFTER_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Make another", callback_data="menu")]]
)
SKIP_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Skip email", callback_data="skip_email")]]
)

# --------------------------------------------------------------------------
# Rate limiting (in memory)
# --------------------------------------------------------------------------
_recent: dict[int, deque] = defaultdict(deque)


def allowed(user_id: int) -> bool:
    now = time.monotonic()
    if len(_recent) > 10000:
        _recent.clear()
    q = _recent[user_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT_COUNT:
        return False
    q.append(now)
    return True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def normalize_link(raw: str):
    """Return (url, error). Exactly one of them is None."""
    raw = raw.strip()
    if not raw:
        return None, "Please send a website link."
    if len(raw) > MAX_LINK_LENGTH:
        return None, "That link is too long."
    if re.search(r"\s", raw):
        return None, "A link cannot contain spaces. Please send just the link."
    if raw.lower().startswith("http://"):
        return None, "Plain http:// links are not supported. Please send an https:// link."
    if "://" not in raw:
        raw = "https://" + raw
    if not raw.lower().startswith("https://"):
        return None, "Only https:// links are supported."
    try:
        parsed = urlparse(raw)
        _ = parsed.port  # raises ValueError if the port is invalid
    except ValueError:
        return None, "That doesn't look like a valid link."
    host = parsed.hostname
    if not host:
        return None, "That doesn't look like a valid link."
    if parsed.username or parsed.password:
        return None, "Links with a username or password are not supported."
    try:
        ipaddress.ip_address(host)
        return None, "Links that use an IP address are not supported."
    except ValueError:
        pass
    try:
        host_ascii = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None, "That doesn't look like a valid link."
    if not HOST_RE.match(host_ascii):
        return None, "That doesn't look like a valid link."
    if host_ascii in SHORTENERS or host_ascii.removeprefix("www.") in SHORTENERS:
        return None, "Link shorteners are not supported. Please send the full link."
    return raw, None


def vcard_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def build_vcard(name: str, phone: str, email: str | None) -> str:
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:;{vcard_escape(name)};;;",
        f"FN:{vcard_escape(name)}",
        f"TEL;TYPE=CELL:{vcard_escape(phone)}",
    ]
    if email:
        lines.append(f"EMAIL:{vcard_escape(email)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines)


def make_qr(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "qr_code.png"
    return buf


async def send_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str, label: str):
    msg = update.effective_message
    user = update.effective_user
    if user is None or not allowed(user.id):
        await msg.reply_text(
            "You're going a bit fast. Please wait a minute and try again."
        )
        return
    await msg.chat.send_action(ChatAction.UPLOAD_PHOTO)
    try:
        buf = await asyncio.to_thread(make_qr, data)
    except Exception as exc:  # noqa: BLE001
        log.warning("QR generation failed: %s", type(exc).__name__)
        await msg.reply_text(
            "Sorry, I couldn't make a QR code from that. Try something shorter."
        )
        return
    context.user_data.clear()
    await msg.reply_photo(
        photo=buf,
        caption=f"Your QR code is ready.\n{label}",
        reply_markup=AFTER_KB,
    )


def preview(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(WELCOME, reply_markup=MENU_KB)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, reply_markup=MENU_KB)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Cancelled. What would you like to encode?", reply_markup=MENU_KB
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "I don't know that command. Use /start for the menu or /help for help."
    )


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = query.message
    data = query.data

    if data == "menu":
        context.user_data.clear()
        await msg.reply_text("What would you like to encode?", reply_markup=MENU_KB)
    elif data == "mode_text":
        context.user_data.clear()
        context.user_data["mode"] = "text"
        await msg.reply_text("Send me the text you want in the QR code.")
    elif data == "mode_link":
        context.user_data.clear()
        context.user_data["mode"] = "link"
        await msg.reply_text("Send me the website link (it must be https://).")
    elif data == "mode_contact":
        context.user_data.clear()
        context.user_data["mode"] = "contact_name"
        await msg.reply_text("Contact card: what is the full name?")
    elif data == "skip_email":
        if context.user_data.get("mode") == "contact_email":
            c = context.user_data["contact"]
            await send_qr(
                update,
                context,
                build_vcard(c["name"], c["phone"], None),
                f"Contact card: {preview(c['name'], 40)}",
            )
        else:
            await msg.reply_text("What would you like to encode?", reply_markup=MENU_KB)


# --------------------------------------------------------------------------
# Text messages
# --------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    mode = context.user_data.get("mode")

    if not text:
        await msg.reply_text("Please send some text.")
        return

    if mode == "link":
        url, error = normalize_link(text)
        if error:
            await msg.reply_text(error + "\nSend another link, or /cancel.")
            return
        await send_qr(update, context, url, f"Link: {preview(url)}")

    elif mode == "contact_name":
        if len(text) > 60:
            await msg.reply_text("That name is too long. Please send a shorter one.")
            return
        context.user_data["contact"] = {"name": text}
        context.user_data["mode"] = "contact_phone"
        await msg.reply_text("What is the phone number? (for example +2348012345678)")

    elif mode == "contact_phone":
        digits = re.sub(r"\D", "", text)
        if not PHONE_RE.match(text) or not 6 <= len(digits) <= 15:
            await msg.reply_text(
                "That doesn't look like a phone number. Please try again, or /cancel."
            )
            return
        context.user_data["contact"]["phone"] = text
        context.user_data["mode"] = "contact_email"
        await msg.reply_text(
            "What is the email address? You can skip this step.",
            reply_markup=SKIP_KB,
        )

    elif mode == "contact_email":
        c = context.user_data["contact"]
        if text.lower() == "skip":
            email = None
        elif EMAIL_RE.match(text) and len(text) <= 100:
            email = text
        else:
            await msg.reply_text(
                "That doesn't look like an email. Try again, or tap Skip email.",
                reply_markup=SKIP_KB,
            )
            return
        await send_qr(
            update,
            context,
            build_vcard(c["name"], c["phone"], email),
            f"Contact card: {preview(c['name'], 40)}",
        )

    else:
        # "text" mode, or no mode chosen: treat the message as plain text.
        if len(text) > MAX_TEXT_LENGTH:
            await msg.reply_text(
                f"That's too long. Please keep it under {MAX_TEXT_LENGTH} characters."
            )
            return
        await send_qr(update, context, text, f"Text: {preview(text)}")


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "I can only read text messages. Send text or a link, or use /start."
    )


# --------------------------------------------------------------------------
# Errors and startup
# --------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", type(context.error).__name__, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong. Please try again, or use /start."
            )
        except Exception:  # noqa: BLE001
            pass


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Open the menu"),
            BotCommand("help", "How to use this bot"),
            BotCommand("cancel", "Stop the current step"),
        ]
    )
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)

    app = Application.builder().token(token).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("help", help_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(
        MessageHandler(
            private & ~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            on_other,
        )
    )
    app.add_handler(MessageHandler(private & filters.COMMAND, unknown_command))
    app.add_error_handler(on_error)

    log.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

