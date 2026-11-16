# ছবির বট (আলাদা বট, আলাদা Render সার্ভিস)।
# ১) শুধু অ্যাডমিন বটকে ছবি পাঠালে বট সেটা প্রাইভেট স্টোরেজ চ্যানেলে রাখে এবং ওয়েবসাইটের জন্য ছবির লিংক ফেরত দেয়।
# ২) ওয়েবসাইট ওই লিংক (/img/<মেসেজ আইডি>) খুললে বট চ্যানেল থেকে ছবি এনে ক্যাশ করে দেয়।
# নিরাপত্তা: শুধু ছবি (photo) যায়, অন্য কিছু (ভিডিও/ফাইল) কখনো যায় না।
import os, time, asyncio, logging
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import FloodWait

logging.basicConfig(level=logging.INFO)

E = os.environ
ADMIN = int(E["ADMIN_ID"])            # শুধু এই আইডির মানুষ ছবি আপলোড করতে পারবে
IMG_CH = int(E["IMG_CHANNEL"])        # স্টোরেজ প্রাইভেট চ্যানেলের আইডি, যেমন -1001234567890
BASE = (E.get("BASE_URL") or E.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
app = None

# ===== IMG PROXY START =====
IMG_DIR = "/tmp/imgc"
IMG_MAX = 250 * 1024 * 1024  # ডিস্ক ক্যাশের সীমা; ছাড়ালে সবচেয়ে পুরনোগুলো মোছে
IMG_SEM = asyncio.Semaphore(3)  # একসাথে ৩টার বেশি ডাউনলোড নয় (Telegram FloodWait ঠেকাতে)
IMG_LOCK = {}
IMG_BAD = {}  # ভুল আইডি ৫ মিনিট মনে রাখি, বারবার Telegram-কে জিজ্ঞেস না করতে


def img_trim():
    try:
        fs = []
        for f in os.listdir(IMG_DIR):
            p = os.path.join(IMG_DIR, f)
            fs.append((os.path.getmtime(p), os.path.getsize(p), p))
        tot = sum(s for _, s, _ in fs)
        for _, s, p in sorted(fs):
            if tot <= IMG_MAX:
                break
            os.remove(p)
            tot -= s
    except Exception as e:
        print("IMG TRIM:", repr(e), flush=True)


async def img_fetch(mid, small):
    """ক্যাশ ফাইলের পাথ ফেরত দেয়; ছবি না থাকলে None।"""
    os.makedirs(IMG_DIR, exist_ok=True)
    path = os.path.join(IMG_DIR, f"{mid}{'s' if small else 'm'}.jpg")
    if os.path.exists(path):
        os.utime(path)
        return path
    t = IMG_BAD.get(mid)
    if t and time.monotonic() - t < 300:
        return None
    key = (mid, small)
    lock = IMG_LOCK.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            if os.path.exists(path):
                return path
            async with IMG_SEM:
                m = await app.get_messages(IMG_CH, mid)
                if not m or m.empty or not m.photo:
                    if len(IMG_BAD) > 5000:
                        IMG_BAD.clear()
                    IMG_BAD[mid] = time.monotonic()
                    return None
                fid = m.photo.file_id
                if small and getattr(m.photo, "thumbs", None):
                    c = [x for x in m.photo.thumbs if x.width >= 320] or list(m.photo.thumbs)
                    fid = min(c, key=lambda x: x.width).file_id
                bio = await app.download_media(fid, in_memory=True)
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(bytes(bio.getbuffer()))
                os.replace(tmp, path)
            img_trim()
            return path
    finally:
        IMG_LOCK.pop(key, None)


async def img_route(r):
    try:
        mid = int(r.match_info["mid"])
        if not 0 < mid < 10**9:
            raise ValueError
    except ValueError:
        return web.Response(status=404)
    try:
        path = await img_fetch(mid, r.query.get("s") == "1")
    except FloodWait as e:
        return web.Response(status=503, headers={"Retry-After": str(e.value)})
    except Exception as e:
        print("IMG ERROR:", repr(e), flush=True)
        return web.Response(status=502)
    if not path:
        return web.Response(status=404)
    return web.FileResponse(path, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Type": "image/jpeg",
    })
# ===== IMG PROXY END =====


# ===== আপলোড (শুধু অ্যাডমিনের প্রাইভেট চ্যাট) =====
HELP = (
    "👋 আমাকে যেকোনো ছবি (Photo হিসেবে) পাঠান। আমি সেটা স্টোরেজ চ্যানেলে রেখে একটা লিংক দেব।\n"
    "সেই লিংক অ্যাডমিন প্যানেলে পোস্টের Thumbnail ঘরে বসান।\n\n"
    "⚠️ ছবি অবশ্যই Photo হিসেবে পাঠাবেন, File/Document হিসেবে নয়।"
)


def is_admin(m):
    return bool(m.from_user and m.from_user.id == ADMIN)


async def on_start(c, m):
    if is_admin(m):
        await m.reply_text(HELP)


async def on_photo(c, m):
    if not is_admin(m):
        return
    try:
        sent = await app.send_photo(IMG_CH, m.photo.file_id)
    except Exception as e:
        print("UPLOAD ERROR:", repr(e), flush=True)
        await m.reply_text("❌ চ্যানেলে রাখা যায়নি। বট চ্যানেলের অ্যাডমিন (পোস্ট করার অনুমতিসহ) আছে কি না এবং IMG_CHANNEL আইডি ঠিক আছে কি না দেখুন।")
        return
    url = f"{BASE}/img/{sent.id}" if BASE else f"/img/{sent.id}"
    await m.reply_text("✅ ছবি রাখা হয়েছে। এই লিংক বসান:\n\n" + url, quote=True)


async def on_doc(c, m):
    if is_admin(m):
        await m.reply_text("ছবিটা File হিসেবে এসেছে। অনুগ্রহ করে Photo হিসেবে (ছবি চিহ্নে) পাঠান।")


async def main():
    global app
    app = Client(
        "imgbot",
        api_id=int(E["API_ID"]),
        api_hash=E["API_HASH"],
        bot_token=E["BOT_TOKEN"],
        in_memory=True,
    )
    app.add_handler(MessageHandler(on_start, filters.command("start") & filters.private), group=0)
    app.add_handler(MessageHandler(on_photo, filters.photo & filters.private), group=0)
    app.add_handler(MessageHandler(on_doc, filters.document & filters.private), group=0)
    await app.start()
    me = await app.get_me()
    print("IMG BOT CHALU:", me.username, flush=True)
    try:
        ch = await app.get_chat(IMG_CH)
        print("CHANNEL OK:", ch.title, flush=True)
    except Exception as e:
        print("CHANNEL ERROR:", repr(e), flush=True)
    w = web.Application()
    w.router.add_get("/", lambda r: web.Response(text="ok"))
    w.router.add_get("/img/{mid}", img_route)
    rn = web.AppRunner(w)
    await rn.setup()
    await web.TCPSite(rn, "0.0.0.0", int(E.get("PORT", 10000))).start()
    await asyncio.Event().wait()


asyncio.run(main())
