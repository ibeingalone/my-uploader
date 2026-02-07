from fastapi import FastAPI, Request
import os, requests, time, threading
import boto3
from urllib.parse import quote

app = FastAPI()

# ================= ENV =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_BUCKET = os.environ.get("R2_BUCKET")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE")

REQUIRED = {
    "BOT_TOKEN": BOT_TOKEN,
    "ADMIN_ID": ADMIN_ID,
    "R2_ACCESS_KEY": R2_ACCESS_KEY,
    "R2_SECRET_KEY": R2_SECRET_KEY,
    "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
    "R2_BUCKET": R2_BUCKET,
    "R2_PUBLIC_BASE": R2_PUBLIC_BASE,
}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

ADMIN_ID = int(ADMIN_ID)

# ================= R2 CLIENT =================
s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

# ================= GLOBAL =================
UPLOADS = {}          # msg_id -> cancel flag
MESSAGE_CACHE = {}   # msg_id -> last rendered message
PAGE_SIZE = 10

HINT_MSG = "⬆️ Please upload or forward a file (max 20 MB)"

# ================= HELPERS =================
def tg(method, payload):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=10
    )

def human(size):
    return f"{round(size / 1024 / 1024, 2)} MB"

# ================= DASHBOARD =================
def send_dashboard(chat_id, page=0):
    objects = s3.list_objects_v2(Bucket=R2_BUCKET).get("Contents", [])
    if not objects:
        tg("sendMessage", {"chat_id": chat_id, "text": "📂 No files found"})
        return

    total = len(objects)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    chunk = objects[start:end]

    keyboard = []
    for o in chunk:
        name = o["Key"]
        keyboard.append([
            {"text": f"📁 {name}", "callback_data": f"show_file:{name}"},
            {"text": "🗑", "callback_data": f"ask_delete:{name}"}
        ])

    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Prev", "callback_data": f"dash_page:{page-1}"})
    if end < total:
        nav.append({"text": "➡️ Next", "callback_data": f"dash_page:{page+1}"})

    if nav:
        keyboard.append(nav)

    tg("sendMessage", {
        "chat_id": chat_id,
        "text": f"📊 Dashboard (Page {page+1})",
        "reply_markup": {"inline_keyboard": keyboard}
    })

# ================= ROUTES =================
@app.get("/")
def root():
    return {"status": "OK", "message": "Bot running"}

@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    # ================= CALLBACKS =================
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq["data"]
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]

        # Pagination
        if data.startswith("dash_page:"):
            page = int(data.split(":")[1])
            send_dashboard(chat_id, page)
            return {"ok": True}

        # Show file
        if data.startswith("show_file:"):
            name = data.split(":", 1)[1]
            safe = quote(name)
            public = f"{R2_PUBLIC_BASE}/{safe}"

            obj = s3.head_object(Bucket=R2_BUCKET, Key=name)
            size = obj["ContentLength"]

            text = (
                "📁 Uploaded file\n\n"
                f"{name}\n"
                f"📦 Size: {human(size)}\n\n"
                f"`{public}`"
            )

            markup = {
                "inline_keyboard": [[
                    {"text": "🔗 Open", "url": public},
                    {"text": "🗑 Delete", "callback_data": f"ask_delete:{name}"}
                ]]
            }

            MESSAGE_CACHE[msg_id] = {"text": text, "reply_markup": markup}

            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": markup
            })
            return {"ok": True}

        # Ask delete
        if data.startswith("ask_delete:"):
            name = data.split(":", 1)[1]
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": f"⚠️ Confirm delete?\n\n📁 {name}",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Yes, Delete", "callback_data": f"confirm_delete:{name}"},
                        {"text": "❌ Cancel", "callback_data": f"cancel_delete:{msg_id}"}
                    ]]
                }
            })
            return {"ok": True}

        # Cancel delete → restore
        if data.startswith("cancel_delete:"):
            original = MESSAGE_CACHE.get(msg_id)
            if original:
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": original["text"],
                    "parse_mode": "Markdown",
                    "reply_markup": original["reply_markup"]
                })
            return {"ok": True}

        # Confirm delete
        if data.startswith("confirm_delete:"):
            name = data.split(":", 1)[1]
            s3.delete_object(Bucket=R2_BUCKET, Key=name)
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "🗑 File deleted successfully"
            })
            return {"ok": True}

        # Cancel upload
        if data.startswith("cancel_upload:"):
            target = int(data.split(":")[1])
            UPLOADS[target] = True
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❌ Upload cancelled"
            })
            return {"ok": True}

        return {"ok": True}

    # ================= MESSAGE =================
    message = update.get("message")
    if not message:
        return {"ok": True}

    if message["from"]["id"] != ADMIN_ID:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    # Dashboard
    if text == "/dashboard":
        send_dashboard(chat_id, 0)
        return {"ok": True}

    # Media detect
    media = None
    for k in ("document", "video", "audio", "voice"):
        if k in message:
            media = message[k]
            break

    if not media:
        tg("sendMessage", {"chat_id": chat_id, "text": HINT_MSG})
        return {"ok": True}

    # ================= START UPLOAD =================
    file_id = media["file_id"]
    size = media.get("file_size", 0)
    name = media.get("file_name", f"file_{int(time.time())}")

    msg = tg("sendMessage", {
        "chat_id": chat_id,
        "text": "⬇️ Downloading… 0%",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "❌ Cancel", "callback_data": "cancel_upload:0"}
            ]]
        }
    }).json()["result"]

    UPLOADS[msg["message_id"]] = False

    threading.Thread(
        target=process_file,
        args=(chat_id, msg["message_id"], file_id, name, size)
    ).start()

    return {"ok": True}

# ================= FILE PROCESS =================
def process_file(chat_id, msg_id, file_id, name, size):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10
        ).json()

        path = r["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"

        start = time.time()
        downloaded = 0

        with requests.get(url, stream=True) as resp:
            with open(name, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    if UPLOADS.get(msg_id):
                        raise Exception("CANCEL")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

        if UPLOADS.get(msg_id):
            raise Exception("CANCEL")

        s3.upload_file(name, R2_BUCKET, name)

        safe = quote(name)
        public = f"{R2_PUBLIC_BASE}/{safe}"

        final_text = (
            "✅ Upload complete!\n\n"
            f"📁 {name}\n"
            f"📦 Size: {human(size)}\n\n"
            f"`{public}`"
        )

        final_markup = {
            "inline_keyboard": [[
                {"text": "🔗 Open", "url": public},
                {"text": "🗑 Delete", "callback_data": f"ask_delete:{name}"}
            ]]
        }

        MESSAGE_CACHE[msg_id] = {"text": final_text, "reply_markup": final_markup}

        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "reply_markup": final_markup
        })

    except:
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=name)
        except:
            pass

        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": "❌ Upload cancelled"
        })

    finally:
        try:
            os.remove(name)
        except:
            pass
        UPLOADS.pop(msg_id, None)