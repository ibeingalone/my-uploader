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

# msg_id -> cancel flag
UPLOADS = {}

# ================= HELPERS =================
def tg(method, payload):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=10
    )

# ================= ROUTES =================
@app.get("/")
def root():
    return {"status": "OK", "message": "Bot running"}

@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    # ---------- CALLBACK HANDLER ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq["data"]
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]

        # CANCEL (download or upload)
        if data.startswith("cancel_upload:"):
            target = int(data.split(":")[1])
            UPLOADS[target] = True
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❌ Operation cancelled"
            })
            return {"ok": True}

        # ASK DELETE CONFIRM
        if data.startswith("ask_delete:"):
            name = data.split(":", 1)[1]
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": f"⚠️ Confirm delete?\n\n📁 {name}",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Yes, Delete", "callback_data": f"confirm_delete:{name}"},
                        {"text": "❌ Cancel", "callback_data": "cancel_delete"}
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

        # CANCEL DELETE
        if data == "cancel_delete":
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "❎ Delete cancelled"
            })
            return {"ok": True}

        return {"ok": True}

    # ---------- MESSAGE ----------
    message = update.get("message")
    if not message:
        return {"ok": True}

    if message["from"]["id"] != ADMIN_ID:
        return {"ok": True}

    chat_id = message["chat"]["id"]

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
    else:
        tg("sendMessage", {"chat_id": chat_id, "text": "❌ Send or forward a file"})
        return {"ok": True}

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
        # GET FILE PATH
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10
        ).json()

        path = r["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"

        start = time.time()
        downloaded = 0

        # ===== DOWNLOAD =====
        with requests.get(url, stream=True) as resp:
            with open(name, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    if UPLOADS.get(msg_id):
                        return
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
                        "text": (
                            f"⬇️ Downloading\n"
                            f"{percent:.1f}% | {speed:.2f} MB/s\n"
                            f"ETA: {int(eta)}s"
                        ),
                        "reply_markup": {
                            "inline_keyboard": [[
                                {"text": "❌ Cancel", "callback_data": f"cancel_upload:{msg_id}"}
                            ]]
                        }
                    })

        # ===== UPLOAD =====
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

        # upload in chunks to allow cancel
        with open(name, "rb") as f:
            s3.upload_fileobj(
                f,
                R2_BUCKET,
                name,
                Callback=lambda x: (_ for _ in ()).throw(Exception("CANCEL"))
                if UPLOADS.get(msg_id) else None
            )

        safe = quote(name)
        public = f"{R2_PUBLIC_BASE}/{safe}"

        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": f"✅ Upload complete!\n\n📁 {name}",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "🔗 Open", "url": public},
                    {"text": "🗑 Delete", "callback_data": f"ask_delete:{name}"}
                ]]
            }
        })

    except:
        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": "❌ Upload failed or cancelled"
        })

    finally:
        try:
            os.remove(name)
        except:
            pass
        UPLOADS.pop(msg_id, None)
