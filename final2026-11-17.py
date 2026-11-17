"""
TeraBox share link -> (Playwright WebKit වලින් download) -> mp4 -> Telegram channels වලට upload

Flow:  SOURCE_CHANNEL post / "/li <terabox link>"  ->  Queue  ->  download  ->  BACKUP -> PUBLIC -> COPY_TARGET

(optional) commands පාවිච්චි කරන්න පුළුවන් අය සීමා කරන්න:
    export ADMIN_IDS=123456789,987654321
"""
import asyncio
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
# Python 3.14 හි pyrogram import කරද්දි event loop එකක් ඕන (නැත්නම් "no current event loop" error)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============ 1. CONFIG ============
API_ID = 31855342
API_HASH = "720b7e861db769d41bebd92b6b9b283c"
BOT_TOKEN = "8510260597:AAFos53NZVfpmfOsV0qLg8wMdVwds4MiitA"

SOURCE_CHANNEL = -1003741713366
PUBLIC_TARGET = -1003765729750
BACKUP_CHANNEL = -1004429992140
COPY_TARGET = -1004323650127   # channel ID එකක් හැම වෙලේම -100 වලින් පටන් ගන්නවා

TERABOX_BOT = "terabox_downloader_new_bot"   # web app එක තියෙන bot
DOWNLOAD_ROOT = os.path.abspath("downloads")
HEADLESS = True                              # සැමවිටම headless (noVNC / display එකක් ඕන නැහැ)
DB_FILE = "processed.db"
NOTIFY_FILE = "notify_chat.txt"

ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x]
# channel එකේ ගහන commands (sender = channel) ත් අනුමතයි
admin_only = (filters.user(ADMIN_IDS) | filters.chat(SOURCE_CHANNEL)) if ADMIN_IDS else filters.all
CMD_CHAT = filters.private | filters.chat(SOURCE_CHANNEL)

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

try:
    with open(NOTIFY_FILE) as _f:
        NOTIFY_CHAT_ID = int(_f.read().strip())
except Exception:
    NOTIFY_CHAT_ID = None

bot_app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_app = Client("user_session", api_id=API_ID, api_hash=API_HASH)

BOT_USERNAME = ""
PROCESSED_CODES_CACHE = set()
RUN_EVENT = asyncio.Event()
RUN_EVENT.set()
CURRENT_TASK = "Idle"

TERA_RE = re.compile(r"https?://(?:[\w-]+\.)*[\w-]*tera[\w-]*\.\w+/s/([\w-]+)", re.I)


# ============ 2. TERABOX LINK HELPERS ============
def extract_code(link):
    m = TERA_RE.search(link or "")
    return m.group(1) if m else None


def find_terabox_link(text):
    m = TERA_RE.search(text or "")
    return m.group(0) if m else None


def find_link_in_message(message):
    urls = []
    text = message.text or message.caption or ""
    urls.append(text)
    for ent in (message.entities or message.caption_entities or []):
        if ent.url:
            urls.append(ent.url)
    if message.reply_markup and getattr(message.reply_markup, "inline_keyboard", None):
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                if b.url:
                    urls.append(b.url)
    for u in urls:
        link = find_terabox_link(u)
        if link:
            return link
    return None


def is_command_message(message):
    return (message.text or message.caption or "").lstrip().startswith(("!", "/"))


# ============ 3. PLAYWRIGHT DOWNLOADER (blocking - thread එකක run වෙනවා) ============
TG_HOOK_JS = """
const _pm = window.postMessage.bind(window);
window.postMessage = function (msg, ...rest) {
    try {
        const d = typeof msg === 'string' ? JSON.parse(msg) : msg;
        if (d && d.eventType) console.log('TGEVENT ' + JSON.stringify(d));
    } catch (e) {}
    return _pm(msg, ...rest);
};
"""
SKIP_WORDS = (".jpg", ".jpeg", ".gif", ".png", ".webp", "thumb", "avatar", "favicon")
WEBAPP_CACHE = {"url": "", "t": 0.0}


def get_webapp(force=False):
    """Bot ගේ web app URL එක (get_webapp_url.py) - විනාඩි 25ක් cache කරනවා."""
    if force or not WEBAPP_CACHE["url"] or time.time() - WEBAPP_CACHE["t"] > 1500:
        from get_webapp_url import get_webapp_url
        print("[tb] web app URL එක ගන්නවා...")
        WEBAPP_CACHE["url"] = get_webapp_url(TERABOX_BOT)
        WEBAPP_CACHE["t"] = time.time()
    return WEBAPP_CACHE["url"]


def find_links(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += find_links(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_links(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj.startswith("http"):
        out.append((path, obj))
    return out


def direct_links(api_data):
    """download link විතරයි (thumbnail සහ .m3u8 stream අයින්)."""
    res = []
    for k, u in find_links(api_data):
        low = (k + u).lower()
        if any(w in low for w in SKIP_WORDS) or "stream" in low or ".m3u8" in low:
            continue
        if "download" in k.lower() or "dlink" in k.lower():
            res.append((k, u))
    return res


def clear_overlay(page, btn):
    handle = btn.element_handle()
    page.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        for (let i = 0; i < 10; i++) {
            const top = document.elementFromPoint(x, y);
            if (!top || top === el || el.contains(top) || top.contains(el)) break;
            top.remove();
        }
    }""", handle)


def press(page, btn):
    clear_overlay(page, btn)
    try:
        btn.click(timeout=5000)
    except Exception:
        btn.evaluate("el => el.click()")


def find_partial_file(since, ignore):
    pattern = os.path.join(tempfile.gettempdir(), "playwright-artifacts-*", "*")
    best, best_m = None, 0
    for f in glob.glob(pattern):
        try:
            m = os.path.getmtime(f)
            if os.path.isfile(f) and f not in ignore and m >= since - 3 and m > best_m:
                best, best_m = f, m
        except OSError:
            pass
    return best


def progress_monitor(stop, state, ignore, label):
    start = time.time()
    last_size, last_t, speed = 0, start, 0.0
    while not stop.is_set():
        f = find_partial_file(start, ignore)
        size = os.path.getsize(f) if f and os.path.exists(f) else 0
        if f:
            state["file"] = f
        now = time.time()
        if now - last_t >= 1:
            speed = (size - last_size) / (now - last_t) / 1e6
            last_size, last_t = size, now
        sys.stdout.write(f"\r    ⏬ {label}: {size / 1e6:8.1f} MB | {speed:5.2f} MB/s | {now - start:4.0f}s")
        sys.stdout.flush()
        stop.wait(1)
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


def _tb_download_once(share_link, out_dir, force_url):
    os.makedirs(out_dir, exist_ok=True)
    webapp_url = get_webapp(force=force_url)
    downloads, opened_links, saved_files, done_temp = [], [], [], []
    st = {"idx": 0, "expected": 1}

    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=HEADLESS)
        try:
            context = browser.new_context(accept_downloads=True, viewport={"width": 480, "height": 900})
            context.add_init_script(TG_HOOK_JS)
            page = context.new_page()

            def on_console(m):
                t = m.text
                if t.startswith("TGEVENT "):
                    try:
                        d = json.loads(t[8:])
                    except Exception:
                        return
                    if d.get("eventType") in ("web_app_open_link", "web_app_open_tg_link"):
                        u = (d.get("eventData") or {}).get("url")
                        if u:
                            opened_links.append(u)

            page.on("console", on_console)
            page.on("dialog", lambda dlg: dlg.dismiss())

            def hook(pg):
                pg.on("download", lambda d: downloads.append(d))

            hook(page)
            context.on("page", hook)
            helper = context.new_page()  # download links open කරන්න එකම page එක (resize error එන්නේ නැහැ)

            page.goto(webapp_url, wait_until="domcontentloaded", timeout=60000)
            inp = page.locator('input[placeholder="Enter TeraBox link"]')
            inp.wait_for(timeout=30000)
            inp.fill(share_link)
            with page.expect_response(lambda r: "/api/terabox/new" in r.url, timeout=180000) as ri:
                page.locator('form button[type="submit"]').click()

            api_data = None
            try:
                api_data = ri.value.json()
            except Exception as e:
                print("[tb] API response read කරන්න බැරි:", e)
            data = api_data.get("data") if isinstance(api_data, dict) else None
            if isinstance(data, list) and data:
                st["expected"] = len(data)  # API එකේ files කීයක්ද
            print(f"[tb] files {st['expected']}ක් expected")

            green_btn = page.locator("button.bg-green-600", has_text="Download")
            try:
                green_btn.first.wait_for(timeout=180000)
            except PWTimeout:
                page.screenshot(path=os.path.join(out_dir, "error.png"))
                raise RuntimeError("Green Download button එක ආවේ නැහැ")

            def wait_complete(d, path):
                stop, state = threading.Event(), {}
                th = threading.Thread(target=progress_monitor,
                                      args=(stop, state, set(done_temp), d.suggested_filename[:25]), daemon=True)
                th.start()
                try:
                    err = d.failure()  # සම්පූර්ණයෙන්ම ඉවර වෙනකම් block
                finally:
                    stop.set()
                    th.join()
                if state.get("file"):
                    done_temp.append(state["file"])
                if err:
                    print("[tb] download error:", err)
                    return False
                d.save_as(path)
                last, stable = -1, 0
                while stable < 3:  # size එක තත්පර 6ක් ස්ථාවරද බලනවා
                    size = os.path.getsize(path) if os.path.exists(path) else 0
                    stable = stable + 1 if (size == last and size > 0) else 0
                    last = size
                    page.wait_for_timeout(2000)
                return True

            def collect(wait_sec):
                deadline = time.time() + wait_sec
                while time.time() < deadline and len(downloads) <= st["idx"]:
                    page.wait_for_timeout(500)
                got = 0
                while len(downloads) > st["idx"]:
                    d = downloads[st["idx"]]
                    st["idx"] += 1
                    name = d.suggested_filename
                    if name in [os.path.basename(s) for s in saved_files]:
                        continue
                    path = os.path.join(out_dir, name)
                    print(f"[tb] Download ({len(saved_files) + 1}/{st['expected']}) -> {name}")
                    if wait_complete(d, path) and os.path.getsize(path) > 0:
                        print(f"[tb] ඉවරයි: {name} ({os.path.getsize(path) / 1e6:.1f} MB)")
                        saved_files.append(path)
                        got += 1
                return got

            def reset_helper():
                try:
                    helper.goto("about:blank", timeout=10000)
                except Exception:
                    pass

            # ---- Plan A: API download links ----
            if api_data is not None:
                for k, u in direct_links(api_data):
                    if len(saved_files) >= st["expected"]:
                        break
                    try:
                        helper.goto(u, timeout=30000)
                    except Exception:
                        pass
                    if not collect(25):
                        reset_helper()

            # ---- Plan B: green buttons ----
            if len(saved_files) < st["expected"]:
                for attempt in range(1, 4):
                    count = green_btn.count()
                    for i in range(max(count, 1)):
                        if len(saved_files) >= st["expected"]:
                            break
                        press(page, green_btn.nth(min(i, count - 1)))
                        got = collect(60)
                        if not got and opened_links:
                            try:
                                helper.goto(opened_links.pop(0), timeout=30000)
                            except Exception:
                                pass
                            if not collect(60):
                                reset_helper()
                        for pg in context.pages:
                            if pg not in (page, helper) and not pg.is_closed():
                                pg.close()
                    if len(saved_files) >= st["expected"]:
                        break
        finally:
            browser.close()

    return saved_files


def tb_download(share_link, out_dir):
    """Blocking. Failure එකක් ආවොත් web app URL එක refresh කරලා එක පාරක් retry කරනවා."""
    try:
        return _tb_download_once(share_link, out_dir, force_url=False)
    except Exception as e:
        print(f"[tb] පළවෙනි try fail ({e}). URL refresh කරලා ආයෙත්...")
        return _tb_download_once(share_link, out_dir, force_url=True)


# ============ 4. DATABASE ============
def db():
    return sqlite3.connect(DB_FILE, timeout=15)


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
        msg_id INTEGER PRIMARY KEY, link TEXT, code TEXT, status TEXT, target_file_id TEXT)""")
    for col in ("code TEXT", "target_file_id TEXT"):
        try:
            c.execute(f"ALTER TABLE processed_messages ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_code ON processed_messages(code);")
    conn.commit()
    c.execute("SELECT code, link FROM processed_messages WHERE status IN ('SUCCESS','SKIPPED_DUPLICATE','PENDING')")
    for code, link in c.fetchall():
        code = code or extract_code(link)
        if code:
            PROCESSED_CODES_CACHE.add(code)
    conn.close()
    print(f"🧠 RAM cache: {len(PROCESSED_CODES_CACHE)} codes")


def q1(sql, args=()):
    conn = db()
    r = conn.execute(sql, args).fetchone()
    conn.close()
    return r


def qall(sql, args=()):
    conn = db()
    r = conn.execute(sql, args).fetchall()
    conn.close()
    return r


def get_message_status(msg_id):
    r = q1("SELECT status FROM processed_messages WHERE msg_id=?", (msg_id,))
    return r[0] if r else None


def mark_processed(msg_id, link, status="SUCCESS", file_id=""):
    code = extract_code(link)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO processed_messages (msg_id,link,code,status,target_file_id) VALUES (?,?,?,?,?)",
                 (msg_id, link, code, status, file_id))
    conn.commit()
    conn.close()
    if code and status in ("SUCCESS", "SKIPPED_DUPLICATE", "PENDING"):
        PROCESSED_CODES_CACHE.add(code)


def set_status(msg_id, status):
    conn = db()
    cur = conn.execute("UPDATE processed_messages SET status=? WHERE msg_id=?", (status, msg_id))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO processed_messages (msg_id,link,code,status,target_file_id) VALUES (?, '', NULL, ?, '')",
                     (msg_id, status))
    conn.commit()
    conn.close()


def get_existing_file_ids(code):
    r = q1("""SELECT target_file_id FROM processed_messages WHERE code=? AND status IN ('SUCCESS','SKIPPED_DUPLICATE')
              AND target_file_id IS NOT NULL AND target_file_id!='' LIMIT 1""", (code,))
    return r[0] if r else None


def get_file_ids_by_msg(msg_id):
    r = q1("SELECT target_file_id FROM processed_messages WHERE msg_id=?", (msg_id,))
    return [x for x in (r[0] or "").split(",") if x] if r else []


FAILED_SQL = "status LIKE 'ERROR%' OR status IN ('FAILED_EXTRACT','INVALID_LINK','CANCELLED')"


def get_stats():
    ok = q1("SELECT COUNT(*) FROM processed_messages WHERE status IN ('SUCCESS','SKIPPED_DUPLICATE')")[0]
    pend = q1("SELECT COUNT(*) FROM processed_messages WHERE status='PENDING'")[0]
    fail = q1(f"SELECT COUNT(*) FROM processed_messages WHERE {FAILED_SQL}")[0]
    return ok, pend, fail


# ============ 5. QUEUE ============
class PriorityQueue:
    def __init__(self):
        self._q = asyncio.Queue()

    async def put(self, item):
        await self._q.put(item)

    async def put_front(self, item):
        items = [item]
        while not self._q.empty():
            items.append(self._q.get_nowait())
            self._q.task_done()
        for i in items:
            await self._q.put(i)

    async def get(self):
        return await self._q.get()

    def task_done(self):
        self._q.task_done()

    def qsize(self):
        return self._q.qsize()

    def clear(self):
        removed = []
        while not self._q.empty():
            removed.append(self._q.get_nowait())
            self._q.task_done()
        return removed


MESSAGE_QUEUE = PriorityQueue()


# ============ 6. HELPERS ============
async def notify_admins(text):
    if NOTIFY_CHAT_ID:
        try:
            await bot_app.send_message(NOTIFY_CHAT_ID, text)
        except Exception:
            pass


async def upload_throttler(current, total):
    await asyncio.sleep(0.08)


def size_str(path):
    return f"{os.path.getsize(path) / (1024 * 1024):.2f} MB"


def get_video_duration(path):
    try:
        from hachoir.metadata import extractMetadata
        from hachoir.parser import createParser
        parser = createParser(path)
        if parser:
            with parser:
                md = extractMetadata(parser)
                if md and md.has("duration"):
                    return md.get("duration").seconds
    except Exception:
        pass
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return int(float(json.loads(r.stdout)["format"]["duration"]))
    except Exception:
        return 0


def fmt_duration(sec):
    if not sec or sec <= 0:
        return "N/A"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


async def upload_file(chat_id, path, caption, duration):
    """Video එකක් විදියට, බැරි නම් document එකක් විදියට. (message, kind) return කරනවා."""
    try:
        m = await bot_app.send_video(chat_id, path, duration=duration, caption=caption,
                                     supports_streaming=path.lower().endswith(".mp4"), progress=upload_throttler)
        return m, "video"
    except Exception as e:
        print(f"⚠️ send_video fail ({e}) -> document")
    m = await bot_app.send_document(chat_id, path, caption=caption, progress=upload_throttler)
    return m, "document"


async def send_by_id(chat_id, file_id, kind, caption, duration=0):
    if kind == "video":
        return await bot_app.send_video(chat_id, file_id, duration=duration, caption=caption, supports_streaming=True)
    return await bot_app.send_document(chat_id, file_id, caption=caption)


def file_id_of(msg):
    for attr in ("video", "document", "animation"):
        obj = getattr(msg, attr, None)
        if obj:
            return obj.file_id
    return None


# ============ 7. CORE TASK ============
async def process_task(identifier, link, custom_chat_id=None, source_msg=None, from_cmd=False):
    is_custom = custom_chat_id is not None
    code = extract_code(link)
    if not code:
        if not is_custom:
            mark_processed(identifier, link, "INVALID_LINK")
        return

    if not is_custom and not from_cmd:
        existing = get_existing_file_ids(code)
        if existing:
            print(f"⏭️ Duplicate: Msg #{identifier}")
            mark_processed(identifier, link, "SKIPPED_DUPLICATE", file_id=existing)
            return

    print(f"\n[+] {'Custom' if is_custom else f'Msg #{identifier}'}: {link}")
    out_dir = os.path.join(DOWNLOAD_ROOT, str(identifier))

    try:
        # Playwright sync API එක වෙනම thread එකක (bot එක block වෙන්නේ නැහැ)
        files = await asyncio.to_thread(tb_download, link, out_dir)
        if not files:
            raise RuntimeError("download වුණ file එකක් නැහැ")

        infos = []
        for path in files:
            infos.append({"path": path, "name": os.path.basename(path),
                          "size": size_str(path), "dur": get_video_duration(path)})

        # ---- /li (private chat) -> ඔයාගේ chat එකටම ----
        if is_custom:
            for it in infos:
                cap = f"🎬 **Downloaded Video**\n\n📁 `{it['name']}`\n📊 `{it['size']}`"
                await upload_file(custom_chat_id, it["path"], cap, it["dur"])
            print("✅ Custom video(s) sent")
            return

        # ---- Channel post / !li (channel) -> Backup -> Public -> Copy target ----
        file_ids = []
        for n, it in enumerate(infos, 1):
            cap = f"🎬 **Downloaded from Msg #{identifier}**" + (f" ({n}/{len(infos)})" if len(infos) > 1 else "")
            cap += f"\n\n📁 **File Name:** `{it['name']}`\n📊 **Size:** `{it['size']}`"
            print(f"📤 Backup upload {n}/{len(infos)}...")
            sent, kind = await upload_file(BACKUP_CHANNEL, it["path"], cap, it["dur"])
            fid = file_id_of(sent)
            if not fid:
                raise RuntimeError("Telegram එකෙන් file_id එකක් ආවේ නැහැ")
            file_ids.append(fid)
            try:
                await send_by_id(PUBLIC_TARGET, fid, kind, cap, it["dur"])
            except Exception as e:
                print(f"⚠️ Public Target Error: {e}")

        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🎬 Watch / Get Video", url=f"https://t.me/{BOT_USERNAME}?start=get_{identifier}")]])

        copy_caption = "\n\n".join(
            f"📁 **File Name:** `{it['name']}`\n📊 **Size:** `{it['size']}`\n⏱️ **Duration:** `{fmt_duration(it['dur'])}`"
            for it in infos)[:1000]

        if not from_cmd:
            try:
                await bot_app.edit_message_reply_markup(SOURCE_CHANNEL, identifier, reply_markup=markup)
            except MessageNotModified:
                pass
            except Exception as e:
                print(f"⚠️ Edit Source Error: {e}")

        try:
            has_media = bool(source_msg and (source_msg.photo or source_msg.video or source_msg.animation or source_msg.document))
            if has_media or not from_cmd:
                await bot_app.copy_message(COPY_TARGET, SOURCE_CHANNEL, identifier,
                                           caption=copy_caption, reply_markup=markup)
            else:
                await bot_app.send_message(COPY_TARGET, copy_caption, reply_markup=markup)
        except Exception as e:
            print(f"❌ Copy Target Error: {e}")
            await notify_admins(f"❌ **COPY_TARGET** එකට යවන්න බැරි වුණා (Msg #{identifier}):\n`{e}`")

        mark_processed(identifier, link, "SUCCESS", file_id=",".join(file_ids))
        print(f"✅ SUCCESS Msg #{identifier} ({len(file_ids)} file)")

    except Exception as e:
        print(f"❌ ERROR: {e}")
        if not is_custom:
            mark_processed(identifier, link, f"ERROR: {str(e)[:30]}")
            await notify_admins(f"❌ **Msg #{identifier}** process කරද්දී error එකක්:\n`{str(e)[:200]}`")
        else:
            await bot_app.send_message(custom_chat_id, f"❌ Error: {e}")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ============ 8. COMMANDS ============
@bot_app.on_message(filters.private & filters.command(
    ["li", "q", "failed", "retry", "pause", "resume", "current", "check", "skip", "clear", "get", "disk", "menu", "help"],
    prefixes=["!", "/"]), group=-1)
async def remember_chat(client, message):
    global NOTIFY_CHAT_ID
    if NOTIFY_CHAT_ID != message.chat.id:
        NOTIFY_CHAT_ID = message.chat.id
        try:
            with open(NOTIFY_FILE, "w") as f:
                f.write(str(NOTIFY_CHAT_ID))
        except Exception:
            pass


@bot_app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith("get_"):
        try:
            msg_id = int(parts[1][4:])
        except ValueError:
            return
        ids = get_file_ids_by_msg(msg_id)
        if not ids:
            await message.reply_text("❌ **Video එක හම්බුනේ නැහැ, නැත්නම් තාම process වෙනවා!**")
            return
        await message.reply_text("⏳ **Video යවනවා...**")
        for fid in ids:
            try:
                await client.send_video(message.chat.id, fid, caption=f"🎬 Msg #{msg_id}")
            except Exception:
                await client.send_document(message.chat.id, fid, caption=f"🎬 Msg #{msg_id}")
        return
    await message.reply_text("👋 **Hello! TeraBox Downloader Active.**")


@bot_app.on_message(filters.command("li", prefixes=["!", "/"]) & CMD_CHAT & admin_only)
async def li_handler(client, message):
    parts = (message.text or "").split(maxsplit=1)
    link = find_terabox_link(parts[1]) if len(parts) > 1 else None
    if not link:
        await message.reply_text("⚠️ **TeraBox link එකක් දෙන්න!**\nඋදා: `/li https://1024terabox.com/s/...`")
        return
    await message.reply_text("⚡ **Link එක Queue එකේ ඉස්සරහට දැම්මා!**")
    if message.chat.id == SOURCE_CHANNEL:
        mark_processed(message.id, link, "PENDING")
        await MESSAGE_QUEUE.put_front({"type": "channel", "id": message.id})
    else:
        await MESSAGE_QUEUE.put_front({"type": "custom", "link": link, "chat_id": message.chat.id, "id": int(time.time())})


@bot_app.on_message(filters.command("q", prefixes=["!"]) & CMD_CHAT & admin_only)
async def q_handler(client, message):
    ok, pend, fail = get_stats()
    state = "▶️ Running" if RUN_EVENT.is_set() else "⏸️ Paused"
    await message.reply_text(
        f"📊 **Bot Status**\n\n🔘 Worker: `{state}`\n⚙️ දැන්: `{CURRENT_TASK}`\n"
        f"⏳ Queue: `{MESSAGE_QUEUE.qsize()}`\n✅ Completed: `{ok}`\n💾 Pending: `{pend}`\n❌ Failed: `{fail}`")


@bot_app.on_message(filters.command("current", prefixes=["!"]) & CMD_CHAT & admin_only)
async def current_handler(client, message):
    await message.reply_text(f"⚙️ **දැන් process වෙන්නේ:** `{CURRENT_TASK}`")


@bot_app.on_message(filters.command("failed", prefixes=["!"]) & CMD_CHAT & admin_only)
async def failed_handler(client, message):
    rows = qall(f"SELECT msg_id, status FROM processed_messages WHERE {FAILED_SQL} ORDER BY msg_id")
    if not rows:
        await message.reply_text("✅ **Fail වුන videos නැහැ!**")
        return
    text = f"❌ **Failed ({len(rows)}):**\n\n" + "\n".join(f"• `{m}` — {s}" for m, s in rows[:50])
    await message.reply_text(text)


@bot_app.on_message(filters.command("retry", prefixes=["!"]) & CMD_CHAT & admin_only)
async def retry_handler(client, message):
    rows = qall(f"SELECT msg_id FROM processed_messages WHERE {FAILED_SQL} ORDER BY msg_id")
    for (msg_id,) in rows:
        set_status(msg_id, "PENDING")
        await MESSAGE_QUEUE.put({"type": "channel", "id": msg_id})
    await message.reply_text(f"🔁 **Messages {len(rows)} ක් Queue එකට දැම්මා.**")


@bot_app.on_message(filters.command("pause", prefixes=["!"]) & CMD_CHAT & admin_only)
async def pause_handler(client, message):
    RUN_EVENT.clear()
    await message.reply_text("⏸️ **Worker නවත්තලා.** (දැන් යන task එක ඉවර වෙලා නවතිනවා)")


@bot_app.on_message(filters.command("resume", prefixes=["!"]) & CMD_CHAT & admin_only)
async def resume_handler(client, message):
    RUN_EVENT.set()
    await message.reply_text("▶️ **Worker ආයෙත් පටන් ගත්තා.**")


@bot_app.on_message(filters.command("clear", prefixes=["!"]) & CMD_CHAT & admin_only)
async def clear_handler(client, message):
    removed = MESSAGE_QUEUE.clear()
    for t in removed:
        if t.get("type") == "channel":
            set_status(t["id"], "CANCELLED")
    await message.reply_text(f"🗑️ **Tasks {len(removed)} ක් අයින් කළා.** (`!retry` වලින් ආයෙත් ගන්න පුළුවන්)")


@bot_app.on_message(filters.command("skip", prefixes=["!"]) & CMD_CHAT & admin_only)
async def skip_handler(client, message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("⚠️ භාවිතය: `!skip <msg_id>`")
        return
    set_status(int(parts[1]), "SKIPPED_MANUAL")
    await message.reply_text(f"⏭️ Msg `{parts[1]}` skip කළා.")


@bot_app.on_message(filters.command("check", prefixes=["!"]) & CMD_CHAT & admin_only)
async def check_handler(client, message):
    parts = message.text.split(maxsplit=1)
    code = extract_code(parts[1]) if len(parts) > 1 else None
    if not code:
        await message.reply_text("⚠️ භාවිතය: `!check <terabox link>`")
        return
    await message.reply_text("⏭️ **කලින් process වෙලා තියෙනවා.**" if code in PROCESSED_CODES_CACHE
                             else "🆕 **තාම process වෙලා නැහැ.**")


@bot_app.on_message(filters.command("get", prefixes=["!"]) & filters.private & admin_only)
async def get_handler(client, message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("⚠️ භාවිතය: `!get <msg_id>`")
        return
    ids = get_file_ids_by_msg(int(parts[1]))
    if not ids:
        await message.reply_text("❌ **Video එකක් නැහැ.**")
        return
    for fid in ids:
        try:
            await client.send_video(message.chat.id, fid)
        except Exception:
            await client.send_document(message.chat.id, fid)


@bot_app.on_message(filters.command("disk", prefixes=["!"]) & CMD_CHAT & admin_only)
async def disk_handler(client, message):
    total, used, free = shutil.disk_usage(".")
    gb = 1024 ** 3
    await message.reply_text(f"💽 **Disk**\n\nTotal `{total / gb:.1f} GB` | Used `{used / gb:.1f} GB` | Free `{free / gb:.1f} GB`")


@bot_app.on_message(filters.command(["menu", "help"], prefixes=["/", "!"]) & CMD_CHAT & admin_only)
async def menu_handler(client, message):
    await message.reply_text(
        "📖 **COMMANDS**\n\n"
        "`/li <terabox link>` — download කරලා upload කරනවා (private chat එකේ නම් ඔයාටම, channel එකේ නම් channels වලට)\n"
        "`!q` — status | `!current` — දැන් යන task\n"
        "`!pause` / `!resume` — worker control | `!clear` — queue හිස් කරනවා\n"
        "`!failed` / `!retry` / `!skip <id>` — fail වුන ඒවා\n"
        "`!check <link>` — කලින් process වෙලාද | `!get <id>` — video එක ගන්න\n"
        "`!disk` — disk space")


# ============ 9. CHANNEL HANDLERS & SYNC ============
@user_app.on_message(filters.chat(SOURCE_CHANNEL))
async def live_message_handler(client, message):
    status = get_message_status(message.id)
    if status in ("SUCCESS", "SKIPPED_DUPLICATE", "SKIPPED_MANUAL"):
        return
    if is_command_message(message):
        if status is None:
            mark_processed(message.id, "", "SKIPPED_NO_LINK")
        return
    link = find_link_in_message(message) or ""
    if not link:
        mark_processed(message.id, "", "SKIPPED_NO_LINK")
        return
    if extract_code(link) in PROCESSED_CODES_CACHE:
        mark_processed(message.id, link, "SKIPPED_DUPLICATE")
        print(f"⏭️ Duplicate in live Msg #{message.id}")
        return
    mark_processed(message.id, link, "PENDING")
    await MESSAGE_QUEUE.put({"type": "channel", "id": message.id})


async def sync_missed_channel_messages():
    max_id = (q1("SELECT MAX(msg_id) FROM processed_messages") or [0])[0] or 0
    print(f"🔍 Syncing after Msg #{max_id}...")
    try:
        if max_id == 0:
            async for m in user_app.get_chat_history(SOURCE_CHANNEL, limit=1):
                mark_processed(m.id, "", "SKIPPED_INITIAL_RUN")
            return
        count = 0
        async for m in user_app.get_chat_history(SOURCE_CHANNEL, limit=50):
            if m.id <= max_id:
                break
            if get_message_status(m.id) is None:
                link = find_link_in_message(m) or ""
                if is_command_message(m) or not link:
                    mark_processed(m.id, "", "SKIPPED_NO_LINK")
                elif extract_code(link) in PROCESSED_CODES_CACHE:
                    mark_processed(m.id, link, "SKIPPED_DUPLICATE")
                else:
                    mark_processed(m.id, link, "PENDING")
                    await MESSAGE_QUEUE.put({"type": "channel", "id": m.id})
                    count += 1
        print(f"🔄 Synced {count} missed messages.")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")


# ============ 10. WORKER ============
async def queue_worker():
    global CURRENT_TASK
    while True:
        await RUN_EVENT.wait()
        task = await MESSAGE_QUEUE.get()
        try:
            CURRENT_TASK = f"{task['type']} #{task['id']}"
            if task["type"] == "custom":
                await process_task(task["id"], task["link"], custom_chat_id=task["chat_id"])
            else:
                msg_id = task["id"]
                if get_message_status(msg_id) in ("SUCCESS", "SKIPPED_DUPLICATE", "SKIPPED_MANUAL"):
                    continue
                message = await user_app.get_messages(SOURCE_CHANNEL, msg_id)
                if message and not message.empty:
                    link = find_link_in_message(message) or ""
                    await process_task(msg_id, link, source_msg=message, from_cmd=is_command_message(message))
                else:
                    mark_processed(msg_id, "", "DELETED")
        except Exception as e:
            print(f"Worker Error: {e}")
        finally:
            CURRENT_TASK = "Idle"
            MESSAGE_QUEUE.task_done()
            await asyncio.sleep(8)


async def announce_online():
    """Channels හඳුනාගෙන (peer cache), bot connected කියලා target channels වලට message එකක් යවනවා."""
    for name, cid in (("SOURCE", SOURCE_CHANNEL), ("BACKUP", BACKUP_CHANNEL),
                      ("PUBLIC", PUBLIC_TARGET), ("COPY", COPY_TARGET)):
        try:
            chat = await bot_app.get_chat(cid)  # bot එකට channel එක resolve වෙනවා
        except Exception as e:
            print(f"❌ {name} ({cid}) access නැහැ: {e}")
            continue
        if name == "SOURCE":
            print(f"✅ {name}: {chat.title}")  # source channel එකට message යවන්නේ නැහැ
            continue
        try:
            await bot_app.send_message(cid, "✅ Hi! Bot connected & online 🚀")
            print(f"✅ {name}: {chat.title} - connected message යැවුවා")
        except Exception as e:
            print(f"❌ {name} ({cid}) message යවන්න බැරි: {e}")


# ============ 11. MAIN ============
async def main():
    global BOT_USERNAME
    init_db()
    print("🚀 Starting...")
    await bot_app.start()
    BOT_USERNAME = (await bot_app.get_me()).username
    await announce_online()
    await user_app.start()
    await sync_missed_channel_messages()
    for pid in [r[0] for r in qall("SELECT msg_id FROM processed_messages WHERE status='PENDING' ORDER BY msg_id")]:
        await MESSAGE_QUEUE.put({"type": "channel", "id": pid})
    asyncio.create_task(queue_worker())
    print("✅ Ready")
    await idle()
    await bot_app.stop()
    await user_app.stop()


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
