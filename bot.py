"""Local CVFetch web UI and Telegram userbot for an authorized channel."""

import asyncio
import hmac
import html
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from cryptography.fernet import Fernet
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from pymongo import MongoClient
from pyrogram import Client
from pyrogram.errors import FloodWait, SessionPasswordNeeded
from pyrogram.types import Message

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cvfetch")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting {name}; configure it in .env")
    return value


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "cvfetch_user").strip() or "cvfetch_user"
AUTH_CHANNEL = required_env("AUTH_CHANNEL")
CV_BOT = os.getenv("CV_BOT", "CVFilesearch_Bot").strip().lstrip("@")
REFRESH_TRIGGER = "#cvfetch_refresh"
HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = int(os.getenv("PORT", "5055"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
FILE_TIMEOUT = int(os.getenv("FILE_TIMEOUT", "90"))
WEB_PASSWORD = required_env("WEB_PASSWORD")
SESSION_STORE_URI = required_env("SESSION_STORE_URI")
SESSION_STORE_DB = os.getenv("SESSION_STORE_DB", "cvfetch").strip() or "cvfetch"
SESSION_ENCRYPTION_KEY = required_env("SESSION_ENCRYPTION_KEY").encode("ascii")
FLASK_SECRET_KEY = required_env("FLASK_SECRET_KEY")

sessions: dict[str, dict[str, Any]] = {}
state_lock = threading.RLock()
operation_lock: asyncio.Lock | None = None
auth_flows: dict[str, dict[str, Any]] = {}
auth_lock: asyncio.Lock | None = None
web = Flask(__name__, template_folder="templates")
web.secret_key = FLASK_SECRET_KEY
web.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
    PERMANENT_SESSION_LIFETIME=3600 * 12,
)
fernet = Fernet(SESSION_ENCRYPTION_KEY)
mongo = MongoClient(SESSION_STORE_URI, serverSelectionTimeoutMS=10000)
session_collection = mongo[SESSION_STORE_DB]["sessions"]
userbot: Client | None = None
loop: asyncio.AbstractEventLoop | None = None


def clean_filename(label: str) -> str:
    """Normalize a search-result label to a useful media filename."""
    text = re.sub(r"^\s*\[[\d.,]+\s*(?:GB|MB|TB)\]\s*", "", label, flags=re.I).strip()
    text = re.sub(r"\.(?:mkv|mp4|avi|mov|webm)$", "", text, flags=re.I).strip()
    year_match = re.search(r"\b(?:19|20)\d{2}\b", text)
    year = year_match.group(0) if year_match else ""

    quality = ""
    for pattern, value in ((r"\b2160\s*p\b|\b4k\b", "2160p"), (r"\b1080\s*p\b", "1080p"),
                           (r"\b720\s*p\b", "720p"), (r"\b480\s*p\b", "480p"), (r"\b360\s*p\b", "360p")):
        if re.search(pattern, text, re.I):
            quality = value
            break

    folded = re.sub(r"[\s._-]+", "", text).lower()
    source = ""
    source_patterns = (("NF.WEB-DL", r"nfweb(?:dl|rip)?"), ("AMZN.WEB-DL", r"amznweb(?:dl|rip)?"),
                       ("DSNP.WEB-DL", r"dsnpweb(?:dl|rip)?"), ("WEBRip", r"webrip"),
                       ("WEB-DL", r"webdl"), ("BluRay", r"bluray|brrip"), ("HDRip", r"hdrip"))
    for value, pattern in source_patterns:
        if re.search(pattern, folded):
            source = value
            break

    language_map = (("HIN", r"\bhin(?:di)?\b"), ("TEL", r"\btel(?:ugu)?\b"),
                    ("TAM", r"\btam(?:il)?\b"), ("KAN", r"\bkan(?:nada)?\b"),
                    ("MAL", r"\bmal(?:ayalam)?\b"), ("ENG", r"\beng(?:lish)?\b"),
                    ("BEN", r"\bben(?:gali)?\b"), ("MAR", r"\bmar(?:athi)?\b"))
    languages = [code for code, pattern in language_map if re.search(pattern, text, re.I)]

    if year_match:
        title = text[:year_match.start()].strip(" ._-–")
    else:
        title = text
        for pattern, _ in source_patterns:
            title = re.sub(pattern, " ", title, flags=re.I)
        title = re.sub(r"\b(?:2160p?|1080p?|720p?|480p?|360p?|4k|DS4K|HIN|TEL|TAM|KAN|MAL|ENG|BEN|MAR|NF|AMZN|DSNP|x26[45]|HEVC)\b", " ", title, flags=re.I)
        title = re.sub(r"\b(?:WEB\s*-?\s*(?:Rip|DL)|Blu\s*-?Ray|HD\s*Rip)\b", " ", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" ._-–")

    parts = [title, year, quality, source, " ".join(languages)]
    stem = re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip(" .")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:180]
    return (stem or "Telegram media") + ".mkv"


def caption_from_result_label(label: str) -> str:
    """Normalize a search button label into a title Stremio can recognize."""
    text = html.unescape(label or "")
    text = re.sub(r"^\s*\[[\d.,]+\s*(?:GB|MB|TB)\]\s*", "", text, flags=re.I)
    text = re.sub(r"\\([*_\[\]()~`])", r"\1", text)
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\\s+", " ", text).strip()
    # WMR is a release/channel prefix in some search results, not part of the title.
    text = re.sub(r"^WMR(?:[ ._:-]+|$)", "", text, flags=re.I)
    text = re.sub(r"\\s+", " ", text).strip()
    text = re.sub(r"\\.(?:mkv|mp4|avi|mov|webm|m4v)\\s*$", "", text, flags=re.I)
    text = re.sub(r"^[^\\w]+", "", text, flags=re.UNICODE)
    text = re.sub(r"\\s+", " ", text).strip(" ._-–—\\\\/")
    return text[:1024] or clean_filename(label).removesuffix(".mkv")


def set_session(session_id: str, **values: Any) -> None:
    with state_lock:
        if session_id in sessions:
            sessions[session_id].update(values)


def current_userbot() -> Client:
    if userbot is None or not userbot.is_connected:
        raise RuntimeError("Connect your Telegram account in the Telegram Login page first.")
    return userbot


def call_on_loop(coro, timeout: int = 45):
    if loop is None or not loop.is_running():
        raise RuntimeError("The service is still starting. Try again shortly.")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError("Telegram did not respond in time. Please retry.") from exc


def load_saved_session() -> str | None:
    record = session_collection.find_one({"_id": "telegram_user"})
    if not record:
        return None
    encrypted = record.get("session")
    if not encrypted:
        return None
    return fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")


def save_session(session_string: str) -> None:
    encrypted = fernet.encrypt(session_string.encode("utf-8")).decode("ascii")
    session_collection.update_one(
        {"_id": "telegram_user"},
        {"$set": {"session": encrypted, "updated_at": time.time()}},
        upsert=True,
    )


async def begin_telegram_login(phone: str) -> str:
    if userbot is not None and userbot.is_connected:
        raise RuntimeError("A Telegram account is already connected. Log out first to switch accounts.")
    client = Client(f"{SESSION_NAME}_web_login", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        sent = await client.send_code(phone)
    except Exception:
        if client.is_connected:
            await client.disconnect()
        raise
    flow_id = uuid.uuid4().hex
    async with auth_lock:
        auth_flows[flow_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "stage": "code",
            "created": time.monotonic(),
        }
    return flow_id


async def complete_telegram_login(flow_id: str, code: str | None = None, password: str | None = None) -> bool:
    global userbot
    async with auth_lock:
        flow = auth_flows.get(flow_id)
    if not flow:
        raise RuntimeError("Login expired. Start the Telegram login again.")
    if time.monotonic() - flow["created"] > 600:
        async with auth_lock:
            auth_flows.pop(flow_id, None)
        await flow["client"].disconnect()
        raise RuntimeError("Login expired. Start the Telegram login again.")

    client = flow["client"]
    if flow["stage"] == "password":
        if not password:
            raise RuntimeError("Enter your Telegram two-step verification password.")
        await client.check_password(password)
    else:
        if not code:
            raise RuntimeError("Enter the Telegram verification code.")
        try:
            await client.sign_in(flow["phone"], flow["phone_code_hash"], code)
        except SessionPasswordNeeded:
            flow["stage"] = "password"
            return False

    session_string = await client.export_session_string()
    await asyncio.to_thread(save_session, session_string)
    await client.disconnect()
    connected = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH,
                       session_string=session_string, in_memory=True)
    await connected.start()
    userbot = connected
    async with auth_lock:
        auth_flows.pop(flow_id, None)
    return True


async def logout_telegram() -> None:
    global userbot
    if userbot is not None and userbot.is_connected:
        await userbot.stop()
    userbot = None
    await asyncio.to_thread(session_collection.delete_one, {"_id": "telegram_user"})


async def maybe_join_required_chat(msg: Message) -> bool:
    """Join channel/group links offered in a bot reply, where public joining is allowed."""
    client = current_userbot()
    markup = msg.reply_markup
    if not markup or not getattr(markup, "inline_keyboard", None):
        return False
    joined = False
    for row in markup.inline_keyboard:
        for button in row:
            label = (button.text or "").lower()
            url = getattr(button, "url", None)
            if not url or not any(word in label for word in ("join", "channel", "subscribe")):
                continue
            try:
                parsed = urlparse(url)
                path = parsed.path.strip("/")
                if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"} or not path:
                    continue
                await client.join_chat(url if path.startswith("+") or path.startswith("joinchat/") else path.split("/")[0])
                joined = True
                log.info("Joined required chat from bot button")
            except Exception as exc:
                log.warning("Could not join required chat from button: %s", exc)
    return joined


async def wait_for_bot_messages(after_id: int, timeout: int, predicate, chat_id: str = CV_BOT):
    client = current_userbot()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async for msg in client.get_chat_history(chat_id, limit=12):
            if msg.id > after_id and msg.from_user and msg.from_user.is_bot and predicate(msg):
                return msg
        await asyncio.sleep(1.5)
    return None


def has_media(msg: Message) -> bool:
    return bool(msg.document or msg.video or msg.audio or msg.animation or msg.photo)


def has_join_prompt(msg: Message) -> bool:
    if not msg.reply_markup or not getattr(msg.reply_markup, "inline_keyboard", None):
        return False
    for row in msg.reply_markup.inline_keyboard:
        for button in row:
            label = (button.text or "").lower()
            if "join" in label or "subscribe" in label:
                return True
    return False


PAGE_COUNTER_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")


def keyboard_buttons(msg: Message) -> list[Any]:
    markup = msg.reply_markup
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    return [button for row in markup.inline_keyboard for button in row]


def page_counter(msg: Message) -> tuple[int, int] | None:
    for button in keyboard_buttons(msg):
        match = PAGE_COUNTER_RE.search((button.text or "").replace(" ", ""))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def keyboard_signature(msg: Message) -> tuple[Any, ...]:
    return (
        (msg.text or msg.caption or "").strip(),
        msg.edit_date,
        page_counter(msg),
        tuple(
            ((button.text or "").strip(), button.callback_data, button.url)
            for button in keyboard_buttons(msg)
        ),
    )


def telegram_bot_start_target(url: str) -> tuple[str, str] | None:
    """Return the bot and optional /start payload for a Telegram bot deep link."""
    try:
        parsed = urlparse(url)
        if parsed.scheme == "tg" and parsed.netloc == "resolve":
            query = parse_qs(parsed.query)
            username = query.get("domain", [""])[0]
            payload = query.get("start", [""])[0]
        elif parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
            "t.me", "www.t.me", "telegram.me", "www.telegram.me"
        }:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 1:
                return None
            username = parts[0]
            payload = parse_qs(parsed.query).get("start", [""])[0]
        else:
            return None
        username = username.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return None
        if payload and (len(payload) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", payload)):
            return None
        return username, payload
    except (TypeError, ValueError):
        return None


def is_navigation_label(label: str, direction: str | None = None) -> bool:
    """Recognize navigation controls without excluding titles such as “Next Goal Wins”."""
    raw = (label or "").strip().lower().replace("\ufe0f", "")
    normalized = re.sub(r"[^a-z0-9/]+", " ", raw).strip()
    next_labels = {"next", "next page", "next results", "more", "forward"}
    previous_labels = {"prev", "previous", "back", "prev page", "previous page"}
    other_labels = {"first", "first page", "last", "last page", "page"}
    arrow_labels = {"◀", "◀◀", "⬅", "⬅⬅", "▶", "▶▶", "⏭", "⏮", "⏩", "⏪", "➡", "⬅️", "➡️"}
    if raw in arrow_labels:
        return direction is None or (direction == "next" and raw in {"▶", "▶▶", "⏭", "⏩", "➡"}) or (direction == "prev" and raw in {"◀", "◀◀", "⬅", "⏮", "⏪"})
    if direction == "next":
        return normalized in next_labels
    if direction == "prev":
        return normalized in previous_labels
    return normalized in next_labels | previous_labels | other_labels


def result_keyboard_buttons(msg: Message) -> list[Any]:
    return [
        button for button in keyboard_buttons(msg)
        if (button.callback_data or button.url)
        and not is_navigation_label(button.text or "")
        and not PAGE_COUNTER_RE.search((button.text or "").replace(" ", ""))
    ]


def navigation_button(msg: Message, direction: str) -> Any | None:
    for button in keyboard_buttons(msg):
        if button.callback_data and is_navigation_label(button.text or "", direction):
            return button
    return None


async def press_callback(message: Message, button: Any) -> None:
    """Press the exact keyboard position using Pyrogram's documented click helper."""
    if not button.callback_data:
        raise RuntimeError("This Telegram button cannot be clicked automatically.")
    markup = message.reply_markup
    if not markup or not getattr(markup, "inline_keyboard", None):
        raise RuntimeError("The Telegram button is no longer available. Search again.")
    for row_index, row in enumerate(markup.inline_keyboard):
        for column_index, candidate in enumerate(row):
            if candidate is button:
                try:
                    # Some bots update the page without answering the callback query.
                    # Keep waiting for the message update even if this click times out.
                    await message.click(column_index, row_index, timeout=5)
                except TimeoutError:
                    log.debug("Callback button did not return an answer; waiting for the message update")
                return
    raise RuntimeError("The Telegram button is no longer available. Search again.")


async def wait_for_page_change(message: Message, old_signature: tuple[Any, ...], timeout: int = 30) -> Message | None:
    """Wait for a paginated bot keyboard to be edited or replaced by a new message."""
    client = current_userbot()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            updated = await client.get_messages(CV_BOT, message.id)
            if updated and keyboard_signature(updated) != old_signature:
                return updated
        except Exception:
            pass
        async for candidate in client.get_chat_history(CV_BOT, limit=8):
            if (candidate.id > message.id and candidate.from_user and candidate.from_user.is_bot
                    and keyboard_buttons(candidate) and keyboard_signature(candidate) != old_signature
                    and (page_counter(candidate) or navigation_button(candidate, "next")
                         or result_keyboard_buttons(candidate))):
                return candidate
        await asyncio.sleep(0.6)
    return None


async def move_to_result_page(message: Message, target_page: int) -> Message:
    """Navigate an edited inline keyboard to the page containing a selected item."""
    current = message
    for _ in range(20):
        counter = page_counter(current)
        if not counter or counter[0] == target_page:
            return current
        direction = "next" if counter[0] < target_page else "prev"
        button = navigation_button(current, direction)
        if not button:
            raise RuntimeError(f"Could not navigate to result page {target_page}.")
        old_signature = keyboard_signature(current)
        try:
            await press_callback(current, button)
        except Exception as exc:
            log.warning("Page navigation click reported an error: %s", exc)
        updated = await wait_for_page_change(current, old_signature)
        if not updated:
            raise TimeoutError("The search bot did not load the next results page.")
        current = updated
    raise RuntimeError("The search bot's page navigation did not reach the selected result.")


async def collect_result_pages(first_message: Message, max_pages: int = 20, on_page=None) -> tuple[list[dict[str, Any]], str | None]:
    """Collect selectable result buttons from successive bot pages."""
    current = first_message
    results: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, Any]] = set()
    warning = None
    fallback_page = 1

    for _ in range(max_pages):
        signature = keyboard_signature(current)
        counter = page_counter(current)
        page_number, page_total = counter if counter else (fallback_page, fallback_page)
        page_key = ("counter", page_number) if counter else ("signature", signature)
        if not signature or page_key in seen_pages:
            break
        seen_pages.add(page_key)
        for button in result_keyboard_buttons(current):
            label = (button.text or "").strip()
            results.append({
                "label": label,
                "callback_data": button.callback_data,
                "url": button.url,
                "button_type": "callback" if button.callback_data else "url",
                "can_fetch": bool(button.callback_data or telegram_bot_start_target(button.url or "")),
                "msg_id": current.id,
                "page": page_number,
                "page_total": page_total,
                "clean_name": clean_filename(label),
            })

        if on_page:
            on_page(list(results), page_number, page_total, current.id)

        next_button = navigation_button(current, "next")
        if not next_button:
            break
        if counter and counter[0] >= counter[1]:
            break
        old_signature = signature
        try:
            await press_callback(current, next_button)
        except Exception as exc:
            log.warning("Page navigation click reported an error: %s", exc)
        updated = await wait_for_page_change(current, old_signature)
        if not updated:
            warning = f"Could not read page {page_number + 1}. The bot did not return a detectable page update."
            break
        current = updated
        fallback_page += 1
    else:
        warning = f"Stopped after {max_pages} pages to keep the result list manageable."

    return results, warning


async def click_result(item: dict[str, Any]) -> tuple[Message | None, int]:
    """Select a callback or start-link result, then handle an optional join gate."""
    client = current_userbot()
    result_msg = await client.get_messages(CV_BOT, item["msg_id"])
    if not result_msg or not result_msg.reply_markup:
        raise RuntimeError("The search result message is no longer available. Search again.")

    if item.get("button_type") == "url" or not item.get("callback_data"):
        target = telegram_bot_start_target(item.get("url") or "")
        if not target:
            raise RuntimeError("This Telegram link does not start a bot, so CVFetch cannot request its file automatically.")
        target_bot, payload = target
        request = f"/start {payload}" if payload else "/start"
        sent = await client.send_message(target_bot, request)
        result_msg = None
        target_chat = target_bot
        latest_id = sent.id
    else:
        result_msg = await move_to_result_page(result_msg, int(item.get("page", 1)))
        target_chat = CV_BOT
        latest_id = result_msg.id
        button = next(
            (candidate for candidate in keyboard_buttons(result_msg)
             if candidate.callback_data == item.get("callback_data")),
            None,
        )
        if not button:
            raise RuntimeError("That result button changed. Search again and select it once more.")
        await press_callback(result_msg, button)
    response = await wait_for_bot_messages(
        latest_id, FILE_TIMEOUT, lambda m: has_media(m) or has_join_prompt(m), chat_id=target_chat
    )
    if not response:
        return None, latest_id
    latest_id = max(latest_id, response.id)
    if has_media(response):
        return response, latest_id

    joined = await maybe_join_required_chat(response)
    if not joined:
        return None, latest_id

    # Some bots require a separate “I joined” / “Check” callback after the channel join.
    confirm_buttons = []
    for row in response.reply_markup.inline_keyboard:
        for button in row:
            label = (button.text or "").strip()
            if button.callback_data and any(word in label.lower() for word in ("joined", "check", "continue", "verify")):
                confirm_buttons.append(label)
    for label in confirm_buttons:
        try:
            confirm_msg = await client.get_messages(target_chat, response.id)
            confirm_button = next(
                (button for row in confirm_msg.reply_markup.inline_keyboard for button in row
                 if button.callback_data and (button.text or "").strip() == label),
                None,
            )
            if confirm_button:
                await press_callback(confirm_msg, confirm_button)
        except Exception as exc:
            log.warning("Join confirmation button failed: %s", exc)
        followup = await wait_for_bot_messages(
            latest_id, FILE_TIMEOUT, lambda m: has_media(m) or has_join_prompt(m), chat_id=target_chat
        )
        if followup:
            latest_id = max(latest_id, followup.id)
            if has_media(followup):
                return followup, latest_id
            response = followup

    # Once membership/confirmation is handled, repeat the original file selection once.
    if item.get("button_type") == "url" or not item.get("callback_data"):
        sent = await client.send_message(target_chat, request)
        latest_id = sent.id
    else:
        result_msg = await client.get_messages(CV_BOT, item["msg_id"])
        button = next(
            (candidate for candidate in keyboard_buttons(result_msg)
             if candidate.callback_data == item.get("callback_data")),
            None,
        )
        if not button:
            raise RuntimeError("The original result button is no longer available. Search again.")
        await press_callback(result_msg, button)
    file_msg = await wait_for_bot_messages(latest_id, FILE_TIMEOUT, has_media, chat_id=target_chat)
    return file_msg, max(latest_id, file_msg.id if file_msg else latest_id)


async def copy_media_to_destination(client: Client, file_msg: Message, caption: str) -> int:
    """Resolve both Telegram peers before copying media with the cleaned caption."""
    target: int | str = AUTH_CHANNEL
    if re.fullmatch(r"-?\d+", AUTH_CHANNEL):
        target = int(AUTH_CHANNEL)
    destination = None
    try:
        destination = await client.get_chat(target)
    except Exception as exc:
        # A private channel ID can be absent from the local peer cache until its
        # dialogs are loaded after a fresh web login or service restart.
        if isinstance(target, int):
            try:
                async for dialog in client.get_dialogs():
                    if dialog.chat.id == target:
                        destination = dialog.chat
                        break
            except Exception:
                log.debug("Could not refresh Telegram dialog peers", exc_info=True)
        if destination is None:
            raise RuntimeError(
                "CVFetch could not resolve the destination channel. Check AUTH_CHANNEL and confirm the connected Telegram account can post there."
            ) from exc

    source_chat_id = file_msg.chat.id if file_msg.chat else CV_BOT
    try:
        await client.copy_message(
            chat_id=destination.id,
            from_chat_id=source_chat_id,
            message_id=file_msg.id,
            caption=caption,
        )
    except Exception as exc:
        if "PEER_ID_INVALID" in str(exc):
            raise RuntimeError(
                "Telegram could not resolve the source file or destination channel. Check AUTH_CHANNEL and make sure the connected account has opened the source bot and can post in the destination."
            ) from exc
        raise
    return destination.id


async def search_cv_bot(session_id: str, query: str) -> None:
    set_session(session_id, status="searching", results=[], error=None)
    try:
        async with operation_lock:
            client = current_userbot()
            sent = await client.send_message(CV_BOT, query)
            reply = await wait_for_bot_messages(
                sent.id, REQUEST_TIMEOUT,
                lambda m: bool(keyboard_buttons(m)) or bool(m.text and re.search(r"no results?|not found", m.text, re.I)),
            )
            if reply and await maybe_join_required_chat(reply):
                set_session(session_id, status="joining")
                await asyncio.sleep(2)
                retry = await client.send_message(CV_BOT, query)
                reply = await wait_for_bot_messages(
                    retry.id, REQUEST_TIMEOUT,
                    lambda m: bool(keyboard_buttons(m)) or bool(m.text and re.search(r"no results?|not found", m.text, re.I)),
                )
            if not reply:
                raise TimeoutError("No response from the search bot. Try again or check the bot in Telegram.")
            def publish_page(partial_results, pages_loaded, pages_total, message_id):
                set_session(session_id, status="searching", results=partial_results,
                            result_msg_id=message_id, pages_loaded=pages_loaded,
                            pages_total=pages_total)

            results, warning = await collect_result_pages(reply, on_page=publish_page)
            selectable = [item for item in results if item["can_fetch"]]
            link_count = len(results) - len(selectable)
            if not results and keyboard_buttons(reply):
                warning = "The bot replied with buttons CVFetch cannot read. Please check its button type."
            elif link_count:
                warning = f"{link_count} Telegram link{'s' if link_count != 1 else ''} cannot be fetched automatically."
            last_page = max((int(item.get("page", 1)) for item in results), default=0)
            known_pages = max((int(item.get("page_total", item.get("page", 1))) for item in results), default=last_page)
            set_session(session_id, results=results, result_msg_id=reply.id,
                        pages_loaded=last_page, pages_total=known_pages,
                        status="ready" if results else "no_results",
                        warning=warning,
                        error=None if results else (reply.text or "No selectable results found."))
    except Exception as exc:
        log.exception("Search failed for session %s", session_id)
        set_session(session_id, status="error", error=str(exc))


async def fetch_selected(session_id: str, indices: list[int]) -> None:
    with state_lock:
        session = sessions.get(session_id)
        if not session:
            return
        results = session["results"]
    set_session(session_id, status="fetching", fetched=0, total=len(indices), fetch_log=[])
    trigger_chat_id = None
    try:
        async with operation_lock:
            client = current_userbot()
            for idx in indices:
                if idx < 0 or idx >= len(results):
                    continue
                item = results[idx]
                try:
                    file_msg, _ = await click_result(item)
                    if not file_msg:
                        raise TimeoutError("No media arrived before timeout.")
                    trigger_chat_id = await copy_media_to_destination(client, file_msg, caption_from_result_label(item.get("label") or item["clean_name"]))
                    with state_lock:
                        session["fetched"] += 1
                        session["fetch_log"].append({"ok": True, "text": item["clean_name"]})
                    await asyncio.sleep(1)
                except FloodWait as exc:
                    await asyncio.sleep(exc.value + 1)
                    with state_lock:
                        session["fetch_log"].append({"ok": False, "text": f"{item['label']} — Telegram rate limit; retry this item."})
                except Exception as exc:
                    log.exception("Fetch failed for %s", item["label"])
                    with state_lock:
                        session["fetch_log"].append({"ok": False, "text": f"{item['label']} — {exc}"})
    finally:
        if trigger_chat_id is not None:
            try:
                await client.send_message(trigger_chat_id, REFRESH_TRIGGER)
            except Exception:
                log.warning("Media was copied, but Telegram-Stremio refresh trigger could not be sent", exc_info=True)
        set_session(session_id, status="done")


def schedule(coro) -> None:
    if loop is None or not loop.is_running():
        raise RuntimeError("Service is not ready")
    asyncio.run_coroutine_threadsafe(coro, loop)


@web.before_request
def protect_web_app():
    if request.endpoint in {"healthz", "web_login", "static"}:
        return None
    origin = request.headers.get("Origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
        if urlparse(origin).netloc != request.host:
            return jsonify(error="Cross-site request blocked."), 403
    if not session.get("web_authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Sign in to CVFetch first."), 401
        return redirect(url_for("web_login"))
    return None


@web.route("/login", methods=["GET", "POST"])
def web_login():
    error = None
    if request.method == "POST":
        password = str(request.form.get("password", ""))
        if hmac.compare_digest(password, WEB_PASSWORD):
            session.clear()
            session.permanent = True
            session["web_authenticated"] = True
            return redirect(url_for("index"))
        error = "That password did not match."
    return render_template("login.html", error=error)


@web.post("/logout")
def web_logout():
    session.clear()
    return redirect(url_for("web_login"))


@web.get("/")
def index():
    if userbot is None or not userbot.is_connected:
        return redirect(url_for("telegram_login"))
    return render_template("index.html")


@web.get("/telegram-login")
def telegram_login():
    return render_template("telegram_login.html", connected=bool(userbot and userbot.is_connected))


@web.post("/api/auth/send-code")
def api_auth_send_code():
    data = request.get_json(silent=True) or {}
    phone = re.sub(r"[\s()-]", "", str(data.get("phone", "")))
    if not re.fullmatch(r"\+?[1-9]\d{6,14}", phone):
        return jsonify(error="Enter your phone number with country code, such as +14155550123."), 400
    try:
        flow_id = call_on_loop(begin_telegram_login(phone), timeout=60)
        session["telegram_auth_flow"] = flow_id
        return jsonify(ok=True, message="Telegram sent a login code to one of your signed-in devices or by SMS.")
    except Exception as exc:
        log.warning("Could not start Telegram login: %s", type(exc).__name__)
        return jsonify(error=str(exc)), 400


@web.post("/api/auth/verify-code")
def api_auth_verify_code():
    flow_id = session.get("telegram_auth_flow")
    code = str((request.get_json(silent=True) or {}).get("code", "")).strip()
    if not flow_id or not code:
        return jsonify(error="Start login and enter the Telegram code."), 400
    try:
        complete = call_on_loop(complete_telegram_login(flow_id, code=code), timeout=60)
        if complete:
            session.pop("telegram_auth_flow", None)
        return jsonify(ok=True, password_required=not complete,
                       message="Enter your Telegram two-step password." if not complete else "Telegram connected.")
    except Exception as exc:
        log.warning("Telegram code verification failed: %s", type(exc).__name__)
        return jsonify(error=str(exc)), 400


@web.post("/api/auth/verify-password")
def api_auth_verify_password():
    flow_id = session.get("telegram_auth_flow")
    password = str((request.get_json(silent=True) or {}).get("password", ""))
    if not flow_id or not password:
        return jsonify(error="Enter your Telegram two-step password."), 400
    try:
        call_on_loop(complete_telegram_login(flow_id, password=password), timeout=60)
        session.pop("telegram_auth_flow", None)
        return jsonify(ok=True, message="Telegram connected.")
    except Exception as exc:
        log.warning("Telegram password verification failed: %s", type(exc).__name__)
        return jsonify(error=str(exc)), 400


@web.post("/api/auth/logout")
def api_auth_logout():
    try:
        call_on_loop(logout_telegram(), timeout=60)
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@web.get("/healthz")
def healthz():
    """Cheap endpoint for Render health checks and an external uptime monitor."""
    return jsonify(ok=True, telegram_connected=bool(userbot and userbot.is_connected)), 200


@web.post("/api/search")
def api_search():
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "")).strip()
    if not query or len(query) > 200:
        return jsonify(error="Enter a search query of 1 to 200 characters."), 400
    session_id = uuid.uuid4().hex
    with state_lock:
        sessions[session_id] = {"query": query, "status": "pending", "results": [], "created": time.time()}
    try:
        schedule(search_cv_bot(session_id, query))
    except Exception as exc:
        set_session(session_id, status="error", error=str(exc))
    return jsonify(session_id=session_id)


@web.get("/api/status/<session_id>")
def api_status(session_id: str):
    with state_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify(error="Session not found"), 404
        return jsonify(session)


@web.post("/api/fetch")
def api_fetch():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    indices = data.get("indices")
    with state_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify(error="Search session not found"), 404
        if session["status"] != "ready":
            return jsonify(error="Search results are not ready, or a fetch is already running."), 409
        if not isinstance(indices, list) or not indices or len(indices) > 100:
            return jsonify(error="Select between 1 and 100 results."), 400
        try:
            selected = sorted(set(int(i) for i in indices))
        except (ValueError, TypeError):
            return jsonify(error="Invalid selection."), 400
        if any(i < 0 or i >= len(session["results"]) for i in selected):
            return jsonify(error="Selection contains an invalid result."), 400
        session["status"] = "fetching"
    try:
        schedule(fetch_selected(session_id, selected))
    except Exception as exc:
        set_session(session_id, status="error", error=str(exc))
        return jsonify(error=str(exc)), 503
    return jsonify(ok=True)


async def main() -> None:
    global loop, operation_lock, auth_lock, userbot
    loop = asyncio.get_running_loop()
    operation_lock = asyncio.Lock()
    auth_lock = asyncio.Lock()
    await asyncio.to_thread(mongo.admin.command, "ping")
    saved_session = await asyncio.to_thread(load_saved_session)
    if saved_session:
        userbot = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH,
                         session_string=saved_session, in_memory=True)
        await userbot.start()
        log.info("Stored Telegram session restored")
    else:
        userbot = None
        log.info("No stored Telegram session; connect from the web login page")
    log.info("CVFetch web UI: http://%s:%s", HOST, PORT)
    server = threading.Thread(target=lambda: web.run(host=HOST, port=PORT, debug=False, use_reloader=False), daemon=True)
    server.start()
    try:
        await asyncio.Event().wait()
    finally:
        if userbot is not None and userbot.is_connected:
            await userbot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"CVFetch configuration/startup error: {exc}") from exc

