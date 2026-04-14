import os
import hashlib
import hmac
import base64
from collections import defaultdict

import anthropic
import httpx
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SYSTEM_PROMPT = (
    "你是藍圈科技股份有限公司的繁體中文客服助理。"
    "請用親切、專業的語氣回答使用者的問題。"
    "如果遇到無法回答的問題，請引導使用者聯繫真人客服。"
)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 簡易多輪對話記憶，key 為 user_id，value 為 messages list
conversation_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 20  # 每位使用者最多保留的訊息輪數


def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_value).decode(), signature)


async def reply_message(reply_token: str, text: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
        )


def chat_with_claude(user_id: str, user_message: str) -> str:
    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})

    # 只保留最近 MAX_HISTORY 則訊息
    if len(history) > MAX_HISTORY:
        conversation_history[user_id] = history[-MAX_HISTORY:]
        history = conversation_history[user_id]

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    assistant_text = response.content[0].text
    history.append({"role": "assistant", "content": assistant_text})
    return assistant_text


@app.post("/callback")
async def callback(request: Request) -> dict:
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    data = await request.json()

    for event in data.get("events", []):
        if event["type"] != "message" or event["message"]["type"] != "text":
            continue

        user_id = event["source"]["userId"]
        user_text = event["message"]["text"]
        reply_token = event["replyToken"]

        try:
            assistant_reply = chat_with_claude(user_id, user_text)
        except Exception as e:
            assistant_reply = f"系統暫時無法回應，請稍後再試。（錯誤：{type(e).__name__}）"
        await reply_message(reply_token, assistant_reply)

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
