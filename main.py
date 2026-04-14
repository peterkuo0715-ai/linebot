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
- 明確要求記錄的事項（例如：「幫我記錄...」）

不算承諾的例子：
- 一般問候或閒聊
- 提問（「什麼時候開會？」）
- 描述過去已完成的事
- 抱怨或情緒發洩

請以以下 JSON 格式回覆，不要包含其他文字：
{{"is_commitment": true, "items": [{{"content": "承諾的具體內容", "due_date": "YYYY-MM-DD 或 null", "assignee": "負責人名稱 或 null"}}]}}

今天的日期是 {today}。
如果提到「明天」，請換算成具體日期。
如果提到「下週X」，請換算成具體日期。
如果沒有找到任何承諾，回傳 {{"is_commitment": false, "items": []}}"""

HELPER_TRIGGER = "小幫手"

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
    source_name = Column(String(128), nullable=True)
    user_id = Column(String(64), nullable=False)
    user_name = Column(String(128), nullable=True)
    content = Column(Text, nullable=False)
    due_date = Column(Date, nullable=True)
    status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=func.now())


class GroupAlias(Base):
    __tablename__ = "group_aliases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    alias = Column(String(32), nullable=False, unique=True)
    group_id = Column(String(64), nullable=False)
    group_name = Column(String(128), nullable=True)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)


engine = None
SessionLocal = None

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)

# ---------------------------------------------------------------------------
# Scheduler & App
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler()


def migrate_db():
    """Add missing columns to existing tables."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "commitments" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("commitments")]
        if "source_name" not in cols:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE commitments ADD COLUMN source_name VARCHAR(128)"
                ))
                conn.commit()
                logger.info("Added source_name column to commitments")
    if "settings" not in insp.get_table_names():
        Base.metadata.tables["settings"].create(engine)
        logger.info("Created settings table")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        Base.metadata.create_all(engine)
        migrate_db()
        logger.info("Database tables ready")
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


async def get_group_name(group_id: str) -> str:
    url = f"https://api.line.me/v2/bot/group/{group_id}/summary"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
        )
        if resp.status_code == 200:
            return resp.json().get("groupName", "未知群組")
    return "未知群組"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def get_setting(key: str) -> str | None:
    if not SessionLocal:
        return None
    db = SessionLocal()
    try:
        s = db.query(Setting).filter_by(key=key).first()
        return s.value if s else None
    finally:
        db.close()


def set_setting(key: str, value: str) -> None:
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        s = db.query(Setting).filter_by(key=key).first()
        if s:
            s.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Group alias helpers
# ---------------------------------------------------------------------------


def set_group_alias(alias: str, group_id: str, group_name: str) -> None:
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        existing = db.query(GroupAlias).filter_by(alias=alias.upper()).first()
        if existing:
            existing.group_id = group_id
            existing.group_name = group_name
        else:
            db.add(GroupAlias(alias=alias.upper(), group_id=group_id, group_name=group_name))
        db.commit()
    finally:
        db.close()


def get_group_by_alias(alias: str) -> GroupAlias | None:
    if not SessionLocal:
        return None
    db = SessionLocal()
    try:
        return db.query(GroupAlias).filter_by(alias=alias.upper()).first()
    finally:
        db.close()


def list_group_aliases() -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        aliases = db.query(GroupAlias).order_by(GroupAlias.alias).all()
        if not aliases:
            return "尚未綁定任何群組編號。\n\n到各群組傳「綁定 G10」即可綁定。"
        lines = ["群組列表：\n"]
        for a in aliases:
            lines.append(f"  {a.alias} → {a.group_name}")
        return "\n".join(lines)
    finally:
        db.close()


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
        json_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        json_str = json_match.group(1).strip() if json_match else raw.strip()
        return json.loads(json_str)
    except Exception as e:
        logger.warning(f"Commitment extraction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def save_commitments(
    extraction: dict, source_type: str, source_id: str,
    user_id: str, user_name: str, source_name: str = "",
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
                source_name=source_name,
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
BIND_RE = re.compile(r"^綁定\s+(\S+)(?:\s+(.+))?$")
GROUP_MSG_RE = re.compile(r"^([A-Za-z]\d+)\s+(.+)$", re.DOTALL)


def cmd_report_all() -> str:
    """管理群組用：列出所有群組的待辦"""
    if not SessionLocal:
        return "資料庫未連線，無法查詢。"
    db = SessionLocal()
    try:
        items = (
            db.query(Commitment)
            .filter_by(status="pending")
            .order_by(Commitment.created_at)
            .all()
        )
        if not items:
            return "目前沒有待辦事項。"
        lines = ["全部待辦清單：\n"]
        for c in items:
            due = f"（截止：{c.due_date}）" if c.due_date else ""
            source = f"[{c.source_name}] " if c.source_name else ""
            assignee = f"{c.user_name}: " if c.user_name else ""
            lines.append(f"#{c.id} {source}{assignee}{c.content} {due}")
        return "\n".join(lines)
    finally:
        db.close()


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


def cmd_weekly_report_all() -> str:
    """管理群組用：列出所有群組本週待辦"""
    if not SessionLocal:
        return "資料庫未連線，無法查詢。"
    db = SessionLocal()
    try:
        today = date.today()
        end_of_week = today + timedelta(days=(6 - today.weekday()))
        items = (
            db.query(Commitment)
            .filter_by(status="pending")
            .filter(Commitment.due_date <= end_of_week)
            .order_by(Commitment.due_date)
            .all()
        )
        overdue = [c for c in items if c.due_date and c.due_date < today]
        this_week = [c for c in items if c not in overdue]

        if not items:
            return "本週沒有到期的待辦事項。"

        lines = ["本週報告（所有群組）：\n"]
        if overdue:
            lines.append("⚠ 已逾期：")
            for c in overdue:
                source = f"[{c.source_name}] " if c.source_name else ""
                lines.append(f"  #{c.id} {source}{c.user_name}: {c.content}（截止：{c.due_date}）")
        if this_week:
            lines.append("本週到期：")
            for c in this_week:
                source = f"[{c.source_name}] " if c.source_name else ""
                lines.append(f"  #{c.id} {source}{c.user_name}: {c.content}（截止：{c.due_date}）")
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


def cmd_complete(item_id: int) -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        c = db.query(Commitment).filter_by(id=item_id, status="pending").first()
        if not c:
            return f"找不到待辦事項 #{item_id}，或該事項已完成/刪除。"
        c.status = "done"
        db.commit()
        return f"已完成：#{c.id} {c.content}"
    finally:
        db.close()


def cmd_delete(item_id: int) -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        c = db.query(Commitment).filter_by(id=item_id, status="pending").first()
        if not c:
            return f"找不到待辦事項 #{item_id}，或該事項已完成/刪除。"
        c.status = "deleted"
        db.commit()
        return f"已刪除：#{c.id} {c.content}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Daily report job → push to admin group only
# ---------------------------------------------------------------------------


async def daily_report_job():
    if not SessionLocal:
        return
    admin_group = get_setting("admin_group_id")
    if not admin_group:
        logger.info("No admin group set, skipping daily report")
        return

    db = SessionLocal()
    try:
        today = date.today()
        pending = (
            db.query(Commitment)
            .filter_by(status="pending")
            .order_by(Commitment.due_date.nulls_last(), Commitment.created_at)
            .all()
        )
        if not pending:
            await push_message(admin_group, "每日待辦報告：\n\n目前沒有待辦事項。")
            return

        overdue = [c for c in pending if c.due_date and c.due_date < today]
        today_items = [c for c in pending if c.due_date == today]
        other = [c for c in pending if c not in overdue and c not in today_items]

        lines = ["每日待辦報告：\n"]
        if overdue:
            lines.append("⚠ 逾期：")
            for c in overdue:
                source = f"[{c.source_name}] " if c.source_name else ""
                lines.append(f"  #{c.id} {source}{c.user_name}: {c.content}（截止：{c.due_date}）")
        if today_items:
            lines.append("今日到期：")
            for c in today_items:
                source = f"[{c.source_name}] " if c.source_name else ""
                lines.append(f"  #{c.id} {source}{c.user_name}: {c.content}")
        if other:
            lines.append("待處理：")
            for c in other:
                source = f"[{c.source_name}] " if c.source_name else ""
                due = f"（截止：{c.due_date}）" if c.due_date else ""
                lines.append(f"  #{c.id} {source}{c.user_name}: {c.content} {due}")

        lines.append(f"\n共 {len(pending)} 項待辦")
        await push_message(admin_group, "\n".join(lines))
        logger.info("Daily report sent to admin group")
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
    admin_group = get_setting("admin_group_id")

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
        is_admin_group = admin_group and source_id == admin_group

        try:
            # --- Admin group setup command ---
            if user_text == "設定為管理群組" and source_type == "group":
                set_setting("admin_group_id", source_id)
                await reply_message(reply_token, "已設定此群組為管理群組！\n\n所有群組的承諾記錄和每日報告都會發送到這裡。\n\n可用指令：\n• 報告 — 所有待辦\n• 本週報告 — 本週到期\n• 完成 #N — 標記完成\n• 刪除 #N — 刪除事項\n• 群組列表 — 查看所有綁定群組\n• G10 訊息內容 — 發話到指定群組")
                continue

            # --- Bind group alias (in any group) ---
            bm = BIND_RE.match(user_text)
            if bm and source_type == "group":
                alias = bm.group(1).upper()
                custom_name = bm.group(2)
                gname = custom_name if custom_name else await get_group_name(source_id)
                set_group_alias(alias, source_id, gname)
                await reply_message(reply_token, f"已綁定此群組為 {alias}（{gname}）")
                continue

            # --- Group list command ---
            if user_text == "群組列表":
                await reply_message(reply_token, list_group_aliases())
                continue

            # --- Remote message from admin group: G10 message ---
            if is_admin_group:
                gm = GROUP_MSG_RE.match(user_text)
                if gm:
                    target_alias = gm.group(1).upper()
                    msg_content = gm.group(2).strip()
                    target = get_group_by_alias(target_alias)
                    if target:
                        polished = call_llm([
                            {"role": "system", "content": (
                                "你是藍圈科技的商務助理。請將以下訊息改寫成專業、禮貌的客戶溝通訊息。"
                                "保留原意，不要加入原文沒有的資訊。使用繁體中文。簡潔即可，不要太長。"
                            )},
                            {"role": "user", "content": msg_content},
                        ], max_tokens=256)
                        await push_message(target.group_id, polished)
                        await reply_message(reply_token, f"已發送到 [{target_alias} {target.group_name}]：\n{polished}")
                    else:
                        await reply_message(reply_token, f"找不到群組 {target_alias}，請先到該群組傳「綁定 {target_alias}」")
                    continue

            # --- Commands (admin group sees all, others see own) ---
            if user_text in ("報告", "待辦清單"):
                if is_admin_group:
                    await reply_message(reply_token, cmd_report_all())
                else:
                    await reply_message(reply_token, cmd_report(source_id))
                continue
            if user_text == "本週報告":
                if is_admin_group:
                    await reply_message(reply_token, cmd_weekly_report_all())
                else:
                    await reply_message(reply_token, cmd_weekly_report(source_id))
                continue
            m = COMPLETE_RE.match(user_text)
            if m:
                await reply_message(reply_token, cmd_complete(int(m.group(1))))
                continue
            m = DELETE_RE.match(user_text)
            if m:
                await reply_message(reply_token, cmd_delete(int(m.group(1))))
                continue

            # --- 1:1 chat: conversation + commitment extraction ---
            if source_type == "user":
                try:
                    assistant_reply = chat_with_llm(user_id, user_text)
                except Exception as e:
                    logger.error(f"LLM chat error: {e}")
                    assistant_reply = "系統暫時無法回應，請稍後再試。"
                await reply_message(reply_token, assistant_reply)

                # Commitment extraction
                user_name = await get_user_name(user_id)
                extraction = extract_commitments(user_text, user_name)
                if extraction and extraction.get("is_commitment"):
                    items = extraction.get("items", [])
                    items_text = "、".join(i["content"] for i in items)
                    save_commitments(
                        extraction, source_type, source_id, user_id, user_name,
                        source_name="私訊",
                    )
                    await push_message(user_id, f"已記錄待辦：{items_text}")
                    if admin_group:
                        await push_message(
                            admin_group,
                            f"[私訊] {user_name} 新增待辦：{items_text}",
                        )

            # --- Group chat ---
            elif source_type in ("group", "room"):
                user_name = await get_user_name(user_id, group_id)
                group_name = await get_group_name(source_id) if source_type == "group" else "聊天室"

                # 「小幫手」trigger → conversation reply in group
                if user_text.startswith(HELPER_TRIGGER):
                    question = user_text[len(HELPER_TRIGGER):].strip()
                    if question:
                        try:
                            assistant_reply = chat_with_llm(
                                f"group_{source_id}", question
                            )
                        except Exception as e:
                            logger.error(f"LLM chat error: {e}")
                            assistant_reply = "系統暫時無法回應，請稍後再試。"
                        await reply_message(reply_token, assistant_reply)
                    continue

                # Commitment extraction (silent monitoring)
                extraction = extract_commitments(user_text, user_name)
                if extraction and extraction.get("is_commitment"):
                    items = extraction.get("items", [])
                    items_text = "、".join(i["content"] for i in items)
                    save_commitments(
                        extraction, source_type, source_id, user_id, user_name,
                        source_name=group_name,
                    )
                    if not is_admin_group:
                        await reply_message(reply_token, f"已記錄：{items_text}")
                    if admin_group and not is_admin_group:
                        await push_message(
                            admin_group,
                            f"[{group_name}] {user_name} 新增待辦：{items_text}",
                        )

        except Exception as e:
            logger.error(f"Event processing error: {e}")
            try:
                await reply_message(reply_token, f"處理訊息時發生錯誤：{type(e).__name__}")
            except Exception:
                pass

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
