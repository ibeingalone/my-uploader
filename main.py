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

# ================= GLOBAL STATE =================
UPLOADS = {}          # msg_id -> cancel flag
MESSAGE_CACHE = {}   # msg_id -> original uploaded message snapshot

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

        # CANCEL UPLOAD / DOWNLOAD
        if data.startswith("cancel_upload:"):
            target = int(data.split(":")[1])
            UPLOADS[target] = True
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❌ Operation cancelled"
            })
            return {"ok": True}

        # ASK DELETE
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

        # CONFIRM DELETE
        if data.startswith("confirm_delete:"):
            name = data.split(":", 1)[1]
            try:
                s3.delete_object(Bucket=R2_BUCKET, Key=name)
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": "🗑 File deleted successfully"
                })
            except:
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": "❌ Delete failed"
                })
            return {"ok": True}

        # CANCEL DELETE → RESTORE ORIGINAL MESSAGE
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

        # DASHBOARD FILE TAP
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

            MESSAGE_CACHE[msg_id] = {
                "text": text,
                "reply_markup": markup
            }

            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": markup
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

    # DASHBOARD COMMAND
    if text == "/dashboard":
        objects = s3.list_objects_v2(Bucket=R2_BUCKET).get("Contents", [])
        if not objects:
            tg("sendMessage", {"chat_id": chat_id, "text": "📂 No files found"})
            return {"ok": True}

        keyboard = []
        for o in objects[:50]:
            name = o["Key"]
            keyboard.append([
                {"text": f"📁 {name}", "callback_data": f"show_file:{name}"},
                {"text": "🗑", "callback_data": f"ask_delete:{name}"}
            ])

        tg("sendMessage", {
            "chat_id": chat_id,
            "text": "📊 Dashboard",
            "reply_markup": {"inline_keyboard": keyboard}
        })
        return {"ok": True}

    # MEDIA DETECT
    media = None
    if "document" in message:
        media = message["document"]
    elif "video" in message:
        media = message["video"]
    elif "audio" in message:
        media = message["audio"]
    elif "voice" in message:
        media = message["voice"]

    # TEXT / START → HINT ONLY
    if not media:
        tg("sendMessage", {"chat_id": chat_id, "text": HINT_MSG})
        return {"ok": True}

    # ================= START PROCESS =================
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
    cancelled = False
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
                        cancelled = True
                        raise Exception("CANCEL")
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    percent = downloaded * 100 / size if size else 0
                    speed = downloaded / max(time.time() - start, 1) / 1024 / 1024
                    eta = (size - downloaded) / max(speed * 1024 * 1024, 1)

                    tg("editMessageText", {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": f"⬇️ Downloading\n{percent:.1f}% | {speed:.2f} MB/s\nETA: {int(eta)}s",
                        "reply_markup": {
                            "inline_keyboard": [[
                                {"text": "❌ Cancel", "callback_data": f"cancel_upload:{msg_id}"}
                            ]]
                        }
                    })

        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": "⬆️ Uploading to R2…",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "❌ Cancel", "callback_data": f"cancel_upload:{msg_id}"}
                ]]
            }
        })

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

        MESSAGE_CACHE[msg_id] = {
            "text": final_text,
            "reply_markup": final_markup
        }

        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "reply_markup": final_markup
        })

    except:
        # ===== OPTION 1 FIX =====
        # If cancelled OR failed → ensure R2 cleanup
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