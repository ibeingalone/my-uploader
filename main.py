from fastapi import FastAPI, Request
import os, requests, time, threading
import boto3

app = FastAPI()

# ===== SAFE ENV (get + explicit check) =====
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

# ===== R2 CLIENT =====
s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

UPLOADS = {}  # message_id -> cancel flag


def tg(method, payload):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=10
    )


@app.get("/")
def root():
    return {"status": "OK", "message": "Bot running"}


@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    # 🔐 ADMIN ONLY
    if user_id != ADMIN_ID:
        return {"ok": True}

    # ❌ CANCEL
    if message.get("text") == "/cancel":
        for k in list(UPLOADS.keys()):
            UPLOADS[k] = True
        tg("sendMessage", {"chat_id": chat_id, "text": "❌ Upload cancelled"})
        return {"ok": True}

    media = None
    media_type = None

    if "document" in message:
        media = message["document"]
        media_type = "Document"
    elif "video" in message:
        media = message["video"]
        media_type = "Video"
    elif "audio" in message:
        media = message["audio"]
        media_type = "Audio"
    elif "voice" in message:
        media = message["voice"]
        media_type = "Voice"
    else:
        tg("sendMessage", {"chat_id": chat_id, "text": "❌ Send a file"})
        return {"ok": True}

    file_id = media["file_id"]
    size = media.get("file_size", 0)
    name = media.get("file_name", f"{media_type.lower()}_{int(time.time())}")

    msg = tg("sendMessage", {
        "chat_id": chat_id,
        "text": f"⬇️ Downloading {media_type}… 0%"
    }).json()["result"]

    threading.Thread(
        target=process_file,
        args=(chat_id, msg["message_id"], file_id, name, size)
    ).start()

    return {"ok": True}


def process_file(chat_id, msg_id, file_id, name, size):
    UPLOADS[msg_id] = False

    # ===== GET FILE PATH =====
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
                    return
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                percent = (downloaded * 100 / size) if size else 0
                speed = downloaded / max(time.time() - start, 1) / 1024 / 1024
                eta = (size - downloaded) / max(speed * 1024 * 1024, 1) if size else 0

                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": (
                        f"⬇️ Downloading\n"
                        f"{percent:.1f}% | {speed:.2f} MB/s\n"
                        f"ETA: {int(eta)}s"
                    )
                })

    # ===== UPLOAD TO R2 =====
    tg("editMessageText", {
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": "⬆️ Uploading to R2…"
    })

    s3.upload_file(name, R2_BUCKET, name)

    public = f"{R2_PUBLIC_BASE}/{name}"

    tg("editMessageText", {
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": (
            "✅ Upload complete!\n\n"
            f"📁 {name}\n"
            f"🔗 {public}"
        )
    })

    try:
        os.remove(name)
    except:
        pass

    UPLOADS.pop(msg_id, None)
