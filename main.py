import os
import re
import json
import hashlib
import hmac
import base64
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Date, DateTime, func,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

SYSTEM_PROMPT = (
    "你是藍圈科技股份有限公司的繁體中文客服助理。"
    "請用親切、專業的語氣回答使用者的問題。"
    "如果遇到無法回答的問題，請引導使用者聯繫真人客服。"
    "請一律使用繁體中文回覆。"
)

COMMITMENT_PROMPT = """你是一個專門分析對話的助理。你的任務是判斷以下訊息是否包含承諾、約定、待辦事項或任務。

承諾的定義包括：
- 明確答應要做某件事（例如：「我明天會把報告交出來」）
- 約定時間或事項（例如：「我們週五開會」）
- 分配任務（例如：「小明負責寫文件」）
- 設定截止日期（例如：「下週一前完成」）
- 客戶要求或老闆交代的事項

不算承諾的例子：
- 一般問候或閒聊
- 提問（「什麼時候開會？」）
- 描述過去已完成的事

請以以下 JSON 格式回覆，不要包含其他文字：
{{"is_commitment": true, "items": [{{"content": "承諾的具體內容", "due_date": "YYYY-MM-DD 或 null", "assignee": "負責人名稱 或 null"}}]}}

今天的日期是 {today}。
如果提到「明天」，請換算成具體日期。
如果提到「下週X」，請換算成具體日期。
如果沒有找到任何承諾，回傳 {{"is_commitment": false, "items": []}}"""

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class Commitment(Base):
    __tablename__ = "commitments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(String(10), nullable=False)
    source_id = Column(String(64), nullable=False)
    user_id = Column(String(64), nullable=False)
    user_name = Column(String(128), nullable=True)
    content = Column(Text, nullable=False)
    due_date = Column(Date, nullable=True)
    status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(String(10), nullable=False)
    source_id = Column(String(64), nullable=False, unique=True)


engine = None
SessionLocal = None

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)

# ---------------------------------------------------------------------------
# Scheduler & App
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        Base.metadata.create_all(engine)
        logger.info("Database tables created")
    scheduler.add_job(
        daily_report_job,
        CronTrigger(hour=9, minute=0, timezone="Asia/Taipei"),
        id="daily_report",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Conversation history (in-memory)
# ---------------------------------------------------------------------------
conversation_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 20

# ---------------------------------------------------------------------------
# LINE helpers
# ---------------------------------------------------------------------------


def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_value).decode(), signature)


async def reply_message(reply_token: str, text: str) -> None:
    if len(text) > 5000:
        text = text[:4997] + "..."
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


async def push_message(to: str, text: str) -> None:
    if len(text) > 5000:
        text = text[:4997] + "..."
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": to,
                "messages": [{"type": "text", "text": text}],
            },
        )


async def get_user_name(user_id: str, group_id: str | None = None) -> str:
    if group_id:
        url = f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
    else:
        url = f"https://api.line.me/v2/bot/profile/{user_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
        )
        if resp.status_code == 200:
            return resp.json().get("displayName", "未知")
    return "未知"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def call_llm(messages: list[dict], max_tokens: int = 1024) -> str:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "max_tokens": max_tokens,
                "messages": messages,
            },
        )
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def chat_with_llm(user_id: str, user_message: str) -> str:
    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > MAX_HISTORY:
        conversation_history[user_id] = history[-MAX_HISTORY:]
        history = conversation_history[user_id]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    assistant_text = call_llm(messages)
    history.append({"role": "assistant", "content": assistant_text})
    return assistant_text


def extract_commitments(message_text: str, user_name: str) -> dict | None:
    today_str = date.today().isoformat()
    system_msg = COMMITMENT_PROMPT.format(today=today_str)
    user_msg = f"發送者：{user_name}\n訊息內容：{message_text}"

    try:
        raw = call_llm(
            [{"role": "system", "content": system_msg},
             {"role": "user", "content": user_msg}],
            max_tokens=512,
        )
        # Try to extract JSON from markdown code fences
        json_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        json_str = json_match.group(1).strip() if json_match else raw.strip()
        return json.loads(json_str)
    except Exception as e:
        logger.warning(f"Commitment extraction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def ensure_subscription(source_type: str, source_id: str) -> None:
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        existing = db.query(Subscription).filter_by(source_id=source_id).first()
        if not existing:
            db.add(Subscription(source_type=source_type, source_id=source_id))
            db.commit()
    finally:
        db.close()


def save_commitments(
    extraction: dict, source_type: str, source_id: str,
    user_id: str, user_name: str,
) -> list[Commitment]:
    if not SessionLocal:
        return []
    saved = []
    db = SessionLocal()
    try:
        for item in extraction.get("items", []):
            due = None
            if item.get("due_date"):
                try:
                    due = date.fromisoformat(item["due_date"])
                except ValueError:
                    pass
            c = Commitment(
                source_type=source_type,
                source_id=source_id,
                user_id=user_id,
                user_name=item.get("assignee") or user_name,
                content=item["content"],
                due_date=due,
                status="pending",
            )
            db.add(c)
            saved.append(c)
        db.commit()
        for c in saved:
            db.refresh(c)
    finally:
        db.close()
    return saved


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
COMPLETE_RE = re.compile(r"^完成\s*#?(\d+)$")
DELETE_RE = re.compile(r"^刪除\s*#?(\d+)$")


def cmd_report(source_id: str) -> str:
    if not SessionLocal:
        return "資料庫未連線，無法查詢。"
    db = SessionLocal()
    try:
        items = (
            db.query(Commitment)
            .filter_by(source_id=source_id, status="pending")
            .order_by(Commitment.created_at)
            .all()
        )
        if not items:
            return "目前沒有待辦事項。"
        lines = ["待辦清單：\n"]
        for c in items:
            due = f"（截止：{c.due_date}）" if c.due_date else ""
            assignee = f"[{c.user_name}] " if c.user_name else ""
            lines.append(f"#{c.id} {assignee}{c.content} {due}")
        return "\n".join(lines)
    finally:
        db.close()


def cmd_weekly_report(source_id: str) -> str:
    if not SessionLocal:
        return "資料庫未連線，無法查詢。"
    db = SessionLocal()
    try:
        today = date.today()
        end_of_week = today + timedelta(days=(6 - today.weekday()))
        items = (
            db.query(Commitment)
            .filter_by(source_id=source_id, status="pending")
            .filter(Commitment.due_date <= end_of_week)
            .order_by(Commitment.due_date)
            .all()
        )
        overdue = [c for c in items if c.due_date and c.due_date < today]
        this_week = [c for c in items if c not in overdue]

        if not items:
            return "本週沒有到期的待辦事項。"

        lines = ["本週報告：\n"]
        if overdue:
            lines.append("⚠ 已逾期：")
            for c in overdue:
                lines.append(f"  #{c.id} {c.content}（截止：{c.due_date}）")
        if this_week:
            lines.append("本週到期：")
            for c in this_week:
                lines.append(f"  #{c.id} {c.content}（截止：{c.due_date}）")
        return "\n".join(lines)
    finally:
        db.close()


def cmd_complete(item_id: int, source_id: str) -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        c = (
            db.query(Commitment)
            .filter_by(id=item_id, source_id=source_id, status="pending")
            .first()
        )
        if not c:
            return f"找不到待辦事項 #{item_id}，或該事項已完成/刪除。"
        c.status = "done"
        db.commit()
        return f"已完成：#{c.id} {c.content}"
    finally:
        db.close()


def cmd_delete(item_id: int, source_id: str) -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        c = (
            db.query(Commitment)
            .filter_by(id=item_id, source_id=source_id, status="pending")
            .first()
        )
        if not c:
            return f"找不到待辦事項 #{item_id}，或該事項已完成/刪除。"
        c.status = "deleted"
        db.commit()
        return f"已刪除：#{c.id} {c.content}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Daily report job
# ---------------------------------------------------------------------------


async def daily_report_job():
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        subs = db.query(Subscription).all()
        today = date.today()

        for sub in subs:
            pending = (
                db.query(Commitment)
                .filter_by(source_id=sub.source_id, status="pending")
                .order_by(Commitment.due_date.nulls_last(), Commitment.created_at)
                .all()
            )
            if not pending:
                continue

            overdue = [c for c in pending if c.due_date and c.due_date < today]
            today_items = [c for c in pending if c.due_date == today]
            other = [c for c in pending if c not in overdue and c not in today_items]

            lines = ["每日待辦報告：\n"]
            if overdue:
                lines.append("⚠ 逾期：")
                for c in overdue:
                    lines.append(f"  #{c.id} [{c.user_name}] {c.content}（截止：{c.due_date}）")
            if today_items:
                lines.append("今日到期：")
                for c in today_items:
                    lines.append(f"  #{c.id} [{c.user_name}] {c.content}")
            if other:
                lines.append("待處理：")
                for c in other:
                    due = f"（截止：{c.due_date}）" if c.due_date else ""
                    lines.append(f"  #{c.id} [{c.user_name}] {c.content} {due}")

            await push_message(sub.source_id, "\n".join(lines))
            logger.info(f"Daily report sent to {sub.source_id}")
    except Exception as e:
        logger.error(f"Daily report job failed: {e}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


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

        source = event["source"]
        source_type = source.get("type", "user")
        user_id = source.get("userId", "")

        if source_type == "group":
            source_id = source["groupId"]
            group_id = source_id
        elif source_type == "room":
            source_id = source["roomId"]
            group_id = None
        else:
            source_id = user_id
            group_id = None

        user_text = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        # Auto-register subscription
        ensure_subscription(source_type, source_id)

        # --- Command handling ---
        if user_text in ("報告", "待辦清單"):
            await reply_message(reply_token, cmd_report(source_id))
            continue
        if user_text == "本週報告":
            await reply_message(reply_token, cmd_weekly_report(source_id))
            continue
        m = COMPLETE_RE.match(user_text)
        if m:
            await reply_message(reply_token, cmd_complete(int(m.group(1)), source_id))
            continue
        m = DELETE_RE.match(user_text)
        if m:
            await reply_message(reply_token, cmd_delete(int(m.group(1)), source_id))
            continue

        # --- 1:1 chat: conversation + commitment extraction ---
        if source_type == "user":
            try:
                assistant_reply = chat_with_llm(user_id, user_text)
            except Exception as e:
                logger.error(f"LLM chat error: {e}")
                assistant_reply = "系統暫時無法回應，請稍後再試。"
            await reply_message(reply_token, assistant_reply)

            # Background commitment extraction
            user_name = await get_user_name(user_id)
            extraction = extract_commitments(user_text, user_name)
            if extraction and extraction.get("is_commitment"):
                items = extraction.get("items", [])
                items_text = "、".join(i["content"] for i in items)
                save_commitments(
                    extraction, source_type, source_id, user_id, user_name
                )
                await push_message(user_id, f"已記錄待辦：{items_text}")

        # --- Group chat: commitment extraction only ---
        elif source_type in ("group", "room"):
            user_name = await get_user_name(user_id, group_id)
            extraction = extract_commitments(user_text, user_name)
            if extraction and extraction.get("is_commitment"):
                items = extraction.get("items", [])
                items_text = "、".join(i["content"] for i in items)
                save_commitments(
                    extraction, source_type, source_id, user_id, user_name
                )
                await reply_message(reply_token, f"已記錄：{items_text}")

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
