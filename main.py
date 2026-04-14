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
ERP_API_URL = os.environ.get("ERP_API_URL", "https://unifi-erp.vercel.app/api/external/linebot")
ERP_API_TOKEN = os.environ.get("ERP_API_TOKEN", "")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

SYSTEM_PROMPT = (
    "你是藍圈科技股份有限公司的繁體中文客服助理。"
    "請用親切、專業的語氣回答使用者的問題。"
    "如果遇到無法回答的問題，請引導使用者聯繫真人客服："
    "電話 02-21001860、電子郵件 sales@brtech.com.tw、官方網站 hi5.com.tw。"
    "請一律使用繁體中文回覆。"
    "【重要】絕對不要捏造或猜測商品價格。"
    "如果沒有收到 ERP 系統的價格資料，請回覆「查無此商品」或建議用戶提供正確型號。"
)

QUERY_PARSE_PROMPT = """你是一個查詢解析助理。請從使用者的問題中提取：
1. customer_code: 客戶代號（格式通常是英文字母+數字，例如 K10、C001），如果沒有提到則為 null
2. product_keyword: 要查詢的商品關鍵字，如果沒有提到則為 null

請以 JSON 格式回覆，不要包含其他文字：
{{"customer_code": "K10", "product_keyword": "U7-Pro"}}

範例：
「K10的U7-Pro報多少」→ {{"customer_code": "K10", "product_keyword": "U7-Pro"}}
「查一下UDR定價」→ {{"customer_code": null, "product_keyword": "UDR"}}
「U7多少錢」→ {{"customer_code": null, "product_keyword": "U7"}}
「K05 有什麼交換器」→ {{"customer_code": "K05", "product_keyword": "交換器"}}"""

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


class Staff(Base):
    __tablename__ = "staff"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, unique=True)
    user_name = Column(String(128), nullable=True)
    email = Column(String(128), nullable=True)
    role = Column(String(32), nullable=True)


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
    if "staff" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("staff")]
        with engine.connect() as conn:
            if "email" not in cols:
                conn.execute(text("ALTER TABLE staff ADD COLUMN email VARCHAR(128)"))
                logger.info("Added email column to staff")
            if "role" not in cols:
                conn.execute(text("ALTER TABLE staff ADD COLUMN role VARCHAR(32)"))
                logger.info("Added role column to staff")
            conn.commit()


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

# Registration state: user_id → {"step": "username"|"pin", "username": "..."}
registration_state: dict[str, dict] = {}

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
# Staff helpers
# ---------------------------------------------------------------------------


def register_staff(user_id: str, user_name: str, email: str = "", role: str = "") -> None:
    if not SessionLocal:
        return
    db = SessionLocal()
    try:
        existing = db.query(Staff).filter_by(user_id=user_id).first()
        if existing:
            existing.user_name = user_name
            if email:
                existing.email = email
            if role:
                existing.role = role
        else:
            db.add(Staff(user_id=user_id, user_name=user_name, email=email, role=role))
        db.commit()
    finally:
        db.close()


def get_staff_email(user_id: str) -> str:
    if not SessionLocal:
        return ""
    db = SessionLocal()
    try:
        s = db.query(Staff).filter_by(user_id=user_id).first()
        return s.email or "" if s else ""
    finally:
        db.close()


def unregister_staff(user_id: str) -> bool:
    if not SessionLocal:
        return False
    db = SessionLocal()
    try:
        s = db.query(Staff).filter_by(user_id=user_id).first()
        if s:
            db.delete(s)
            db.commit()
            return True
        return False
    finally:
        db.close()


def is_staff(user_id: str) -> bool:
    if not SessionLocal:
        return False
    db = SessionLocal()
    try:
        return db.query(Staff).filter_by(user_id=user_id).first() is not None
    finally:
        db.close()


def list_staff() -> str:
    if not SessionLocal:
        return "資料庫未連線。"
    db = SessionLocal()
    try:
        members = db.query(Staff).order_by(Staff.user_name).all()
        if not members:
            return "尚未註冊任何員工。\n\n員工請私訊 Bot 傳「註冊員工」。"
        lines = ["員工名冊：\n"]
        for m in members:
            lines.append(f"  • {m.user_name}")
        return "\n".join(lines)
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
# ERP API
# ---------------------------------------------------------------------------


def erp_query(action: str, **params) -> dict | list | None:
    if not ERP_API_TOKEN:
        return None
    try:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{ERP_API_URL}?action={action}&{qs}"
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {ERP_API_TOKEN}"},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"ERP query failed: {e}")
    return None


def parse_product_query(question: str) -> dict:
    """Use LLM to extract customer_code and product_keyword from question."""
    try:
        raw = call_llm(
            [{"role": "system", "content": QUERY_PARSE_PROMPT},
             {"role": "user", "content": question}],
            max_tokens=128,
        )
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.warning(f"Query parse failed: {e}")
    return {"customer_code": None, "product_keyword": question}


PRICE_KEYWORDS = re.compile(
    r"(查|報價|價錢|多少錢|價格|定價|售價|查價|幾塊|怎麼賣|U\d|UDR|UDM|UDW|USW|UCG|UXG|UAP)",
    re.IGNORECASE,
)


def is_product_query(text: str) -> bool:
    """Detect if a message is asking about products/pricing."""
    return bool(PRICE_KEYWORDS.search(text))


def build_erp_context(question: str, group_source_id: str | None = None,
                      allow_customer_pricing: bool = False,
                      force_customer_code: str | None = None) -> str:
    """Query ERP and build context string for LLM."""
    parsed = parse_product_query(question)
    customer_code = force_customer_code or parsed.get("customer_code")
    keyword = parsed.get("product_keyword") or question

    erp_context = ""
    products = erp_search_with_fallback(keyword)
    if not products:
        return "\n\n[ERP 系統查無「" + keyword + "」相關商品。請告知用戶查無此商品，不要自行編造價格。]"

    plines = []
    for p in products[:5]:
        price = f"${p['msrp']:,}" if p.get("msrp") else "洽詢"
        plines.append(f"{p['sku']} - {p['name']} - 建議售價{price} - {p.get('availability', '')}")
    erp_context = "\n\n以下是 ERP 系統中的商品資料（價格單位為新台幣 NTD），請嚴格根據這些資料回答，絕對不要捏造價格：\n" + "\n".join(plines)

    if not allow_customer_pricing:
        erp_context += "\n注意：此用戶沒有客戶專屬定價權限，只提供建議售價。不要透露任何折扣資訊。"
        return erp_context

    # Determine customer code: explicit in question > group alias
    if not customer_code and group_source_id and SessionLocal:
        db = SessionLocal()
        try:
            alias_obj = db.query(GroupAlias).filter_by(group_id=group_source_id).first()
            if alias_obj:
                customer_code = alias_obj.alias
        finally:
            db.close()

    # Get customer-specific quotes
    if customer_code and products:
        customer_info = erp_get_customer(customer_code)
        if customer_info:
            erp_context += f"\n\n客戶資訊：代號 {customer_code}，名稱 {customer_info.get('name', '')}，等級 {customer_info.get('tier', '')}"
        for p in products[:3]:
            quote = erp_query("quote", code=customer_code, sku=p["sku"])
            if quote and isinstance(quote, dict) and "error" not in quote:
                pr = quote.get("pricing", {})
                erp_context += (
                    f"\n{p['sku']} 客戶報價：建議售價 NT${pr.get('msrp', 0):,}，"
                    f"客戶價 NT${pr.get('unitPrice', 0):,}，"
                    f"折扣 {pr.get('discount', 0)}%，"
                    f"價格來源：{pr.get('source', 'msrp')}，"
                    f"庫存：{quote.get('availability', '洽詢')}"
                )

        erp_context += (
            "\n\n回覆格式範例：「U7-Pro 您的專屬價 NT$6,934（建議售價 NT$7,299，折扣 5%）・約四個工作天可交貨」"
            "\n價格來源說明：customer_specific=客戶專屬價、tier=等級定價、brand=品牌折扣、msrp=無特價"
        )

    return erp_context


def erp_search_with_fallback(keyword: str) -> list | None:
    """Search ERP with fallback: try multiple formats."""
    variations = [
        keyword,                                          # U7-Pro (original)
        keyword.replace(" ", "-"),                         # U7 PRO → U7-PRO
        keyword.replace(" ", ""),                           # U7 PRO → U7PRO
        keyword.replace("-", " "),                          # U7-Pro → U7 Pro
        keyword.replace("-", ""),                           # U7-Pro → U7Pro
        re.sub(r"([A-Za-z])(\d)", r"\1-\2", keyword),      # U7PRO → U7-PRO
        re.sub(r"(\d)([A-Za-z])", r"\1-\2", keyword.replace(" ", "")),  # U7 PRO → U7-PRO
    ]
    seen = set()
    for v in variations:
        v = v.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        results = erp_query("products", search=v)
        if results and isinstance(results, list) and len(results) > 0:
            return results
    return None


def erp_search_products(keyword: str) -> str:
    results = erp_search_with_fallback(keyword)
    if not results:
        return f"找不到與「{keyword}」相關的商品。"
    items = results[:10]
    lines = [f"搜尋「{keyword}」找到 {len(results)} 項商品：\n"]
    for p in items:
        price = f"${p['msrp']:,}" if p.get("msrp") else "洽詢"
        stock = p.get("availability", "")
        lines.append(f"• {p['sku']} — {p['name']}  建議售價 {price}  {stock}")
    if len(results) > 10:
        lines.append(f"\n...還有 {len(results) - 10} 項，請用更精確的關鍵字搜尋")
    return "\n".join(lines)


def erp_get_quote(customer_code: str, sku: str) -> str:
    result = erp_query("quote", code=customer_code, sku=sku)
    if not result or isinstance(result, dict) and "error" in result:
        err = result.get("error", "") if isinstance(result, dict) else ""
        return f"無法查詢報價：{err}" if err else "無法查詢報價"
    return json.dumps(result, ensure_ascii=False)


def erp_verify_staff(username: str, pin: str) -> dict | None:
    if not ERP_API_TOKEN:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                ERP_API_URL,
                headers={
                    "Authorization": f"Bearer {ERP_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"action": "verify_staff", "username": username, "pin": pin},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"ERP verify_staff failed: {e}")
    return None


def erp_create_quote(customer_code: str, items: list[dict], created_by_email: str) -> dict | None:
    if not ERP_API_TOKEN:
        return None
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                ERP_API_URL,
                headers={
                    "Authorization": f"Bearer {ERP_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "action": "create_quote",
                    "customerCode": customer_code,
                    "createdByEmail": created_by_email,
                    "items": items,
                },
            )
            return resp.json()
    except Exception as e:
        logger.warning(f"ERP create_quote failed: {e}")
    return None


def erp_download_pdf(quote_id: str) -> bytes | None:
    if not ERP_API_TOKEN:
        return None
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{ERP_API_URL}?action=pdf&quoteId={quote_id}&template=detailed",
                headers={"Authorization": f"Bearer {ERP_API_TOKEN}"},
            )
            if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", ""):
                return resp.content
    except Exception as e:
        logger.warning(f"ERP download_pdf failed: {e}")
    return None


# In-memory PDF cache for temporary download links
pdf_cache: dict[str, bytes] = {}


def erp_get_customer(customer_code: str) -> dict | None:
    result = erp_query("customer", code=customer_code)
    if isinstance(result, dict) and "error" not in result:
        return result
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
REMOTE_MSG_RE = re.compile(r"^@([A-Za-z0-9]+)\s+(.+)$", re.DOTALL)
PRICE_QUERY_RE = re.compile(r"^\$(.+)$")
QUOTE_RE = re.compile(r"^報價\s+([A-Za-z0-9]+)\s+(.+)$", re.DOTALL)
QUOTE_NO_CODE_RE = re.compile(r"^報價\s+(.+)$", re.DOTALL)  # 沒指定客戶代號


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
            # --- Help command ---
            if (user_text == "-BOT" or user_text == "-bot") and is_admin_group and is_staff(user_id):
                help_lines = [
                    "藍圈科技 Bot 指令一覽：",
                    "",
                    "【查價】",
                    "  $U7-Pro → 查建議售價",
                    "  $K01 U7-Pro → 查客戶專屬報價（員工限定）",
                    "",
                    "【報價單】（員工限定）",
                    "  報價 K01",
                    "  1. U7-Pro x40",
                    "  2. U7-Lite x10",
                    "  → 建立報價單（僅管理群組顯示）",
                    "",
                    "  @K01 報價",
                    "  1. U7-Pro x40",
                    "  → 建立報價單 + 發送到客戶群組",
                    "",
                    "【遠端發話】（員工限定）",
                    "  @K01 訊息內容 → 發送到群組（自動潤飾）",
                    "",
                    "【待辦管理】（員工限定）",
                    "  報告 → 所有群組待辦",
                    "  本週報告 → 本週到期事項",
                    "  完成 #1 → 標記完成",
                    "  刪除 #1 → 刪除事項",
                    "",
                    "【群組管理】（員工限定）",
                    "  綁定 K01 → 綁定群組編號",
                    "  群組列表 → 查看所有綁定群組",
                    "  設定為管理群組 → 設定管理中心",
                    "  員工名冊 → 查看員工列表",
                    "",
                    "【AI 對話】",
                    "  小幫手 問題 → AI 回覆（群組用）",
                    "  直接打字 → AI 回覆（私訊用）",
                    "",
                    "【自動功能】",
                    "  • 群組承諾自動擷取（區分我方/客戶）",
                    "  • 每天早上 9:00 推播待辦報告",
                    "",
                    "輸入 -規則 查看詳細使用規則",
                ]
                await reply_message(reply_token, "\n".join(help_lines))
                continue

            if (user_text == "-規則" or user_text == "-规则") and is_admin_group and is_staff(user_id):
                rules = [
                    "使用規則說明：",
                    "",
                    "━━━ 指令前綴 ━━━",
                    "$ → 查價（任何人可查建議售價）",
                    "@ → 對外操作（發話/發報價單到客戶群組）",
                    "小幫手 → AI 對話（群組用）",
                    "- → 系統指令（如 -BOT、-規則）",
                    "",
                    "━━━ 報價單規則 ━━━",
                    "「報價 K01 品項」→ 只在管理群組顯示（預覽）",
                    "「@K01 報價 品項」→ 建立並發送到客戶群組",
                    "",
                    "品項格式（建議用編號分行）：",
                    "  1. SKU x數量",
                    "  2. SKU *數量",
                    "  支援 x X × * 當數量符號",
                    "  不寫數量預設為 1",
                    "",
                    "━━━ 權限規則 ━━━",
                    "所有人可用：",
                    "  • $查價（建議售價）",
                    "  • 小幫手 對話",
                    "  • 註冊員工（需 ERP 驗證）",
                    "",
                    "員工才能用：",
                    "  • $K01 查客戶專屬價",
                    "  • 報價/@ 報價 建立報價單",
                    "  • @遠端發話",
                    "  • 報告/本週報告/完成/刪除",
                    "  • 綁定/群組列表/員工名冊",
                    "  • -BOT/-規則",
                    "",
                    "管理群組限定：",
                    "  • 設定為管理群組",
                    "  • -BOT / -規則",
                    "  • @遠端發話",
                    "  • 查所有群組報告",
                    "",
                    "━━━ 查價權限 ━━━",
                    "員工在管理群組 → 可查任何客戶報價",
                    "員工在客戶群組 → 可查該群組客戶報價",
                    "客戶在自己群組 → 可查自己的報價",
                    "客戶查別人代號 → 只看到建議售價",
                    "私訊查價 → 只看到建議售價",
                    "",
                    "━━━ 群組行為 ━━━",
                    "客戶群組：自動監聽承諾（區分我方/客戶）",
                    "管理群組：接收所有承諾通知 + 每日報告",
                    "私訊：一般 AI 對話 + 自動擷取承諾",
                ]
                await reply_message(reply_token, "\n".join(rules))
                continue

            # --- Staff registration with ERP verification (1:1 only) ---
            if source_type == "user" and user_id in registration_state:
                state = registration_state[user_id]
                if state["step"] == "username":
                    state["username"] = user_text
                    state["step"] = "pin"
                    await reply_message(reply_token, "請輸入你的 LINE Bot 驗證碼（PIN）：")
                    continue
                elif state["step"] == "pin":
                    result = erp_verify_staff(state["username"], user_text)
                    del registration_state[user_id]
                    if result and result.get("verified"):
                        register_staff(user_id, result["name"], result.get("email", ""), result.get("role", ""))
                        await reply_message(
                            reply_token,
                            f"驗證成功！已註冊 {result['name']} 為員工（{result.get('role', '')}）。\n\nBot 在群組中會區分你的訊息為「我方」。"
                        )
                    else:
                        err = result.get("error", "驗證失敗") if result else "無法連接 ERP"
                        await reply_message(reply_token, f"驗證失敗：{err}\n\n請重新輸入「註冊員工」再試。")
                    continue

            if user_text == "註冊員工" and source_type == "user":
                registration_state[user_id] = {"step": "username"}
                await reply_message(reply_token, "請輸入你的 ERP 帳號（姓名或 email）：")
                continue
            if user_text == "取消員工" and source_type == "user":
                if unregister_staff(user_id):
                    await reply_message(reply_token, "已取消員工身份。")
                else:
                    await reply_message(reply_token, "你尚未註冊為員工。")
                continue

            # --- Staff list (admin group) ---
            if user_text == "員工名冊" and is_admin_group and is_staff(user_id):
                await reply_message(reply_token, list_staff())
                continue

            # --- Admin group setup command ---
            if user_text == "設定為管理群組" and source_type == "group" and is_staff(user_id):
                set_setting("admin_group_id", source_id)
                await reply_message(reply_token, "已設定此群組為管理群組！\n\n輸入 -BOT 查看所有可用指令。")
                continue

            # --- Bind group alias (in any group) ---
            bm = BIND_RE.match(user_text)
            if bm and source_type == "group" and is_staff(user_id):
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

            # --- @K01 message → Remote message (LLM polished) ---
            rm = REMOTE_MSG_RE.match(user_text)
            if rm and is_admin_group and is_staff(user_id):
                target_alias = rm.group(1).upper()
                msg_content = rm.group(2).strip()
                target = get_group_by_alias(target_alias)
                if target:
                    # @K01 報價 ... → create quote and send to customer group
                    if msg_content.startswith("報價"):
                        quote_items_str = msg_content[2:].strip()
                        if not quote_items_str:
                            await reply_message(reply_token, f"請提供品項。範例：@K01 報價\n1. U7-Pro x40\n2. U7-Lite x10")
                            continue
                        # Reuse quote parsing logic
                        q_items = []
                        parts = re.split(r"[\n,、]+", quote_items_str)
                        parse_ok = True
                        for part in parts:
                            part = re.sub(r"^\d+[\.\)、]\s*", "", part.strip())
                            if not part:
                                continue
                            m_item = re.match(r"(.+?)\s*[xX×\*]\s*(\d+)\s*$", part)
                            if not m_item:
                                m_item = re.match(r"(.+?)\s+(\d+)\s*$", part)
                            if m_item:
                                raw_sku = m_item.group(1).strip()
                                qty = int(m_item.group(2))
                            else:
                                raw_sku = part.strip()
                                qty = 1
                            products = erp_search_with_fallback(raw_sku)
                            if products and len(products) == 1:
                                q_items.append({"sku": products[0]["sku"], "quantity": qty})
                            elif products and len(products) > 1:
                                exact = [p for p in products if p["sku"].lower() == raw_sku.lower().replace(" ", "-")]
                                if exact:
                                    q_items.append({"sku": exact[0]["sku"], "quantity": qty})
                                else:
                                    options = "\n".join(f"  {i+1}. {p['sku']}" for i, p in enumerate(products[:5]))
                                    await reply_message(reply_token, f"「{raw_sku}」找到多個商品：\n{options}\n請用正確 SKU 重試。")
                                    parse_ok = False
                                    break
                            else:
                                await reply_message(reply_token, f"找不到商品「{raw_sku}」")
                                parse_ok = False
                                break
                        if not parse_ok or not q_items:
                            continue
                        staff_email = get_staff_email(user_id)
                        if not staff_email:
                            await reply_message(reply_token, "你的帳號沒有綁定 email，請重新「註冊員工」。")
                            continue
                        result = erp_create_quote(target_alias, q_items, staff_email)
                        if result and result.get("success"):
                            # Reply to admin group
                            lines = [f"報價單 {result['quoteNumber']} 已建立！\n",
                                     f"客戶：{result['customer']['name']}（{target_alias}）"]
                            for item in result.get("items", []):
                                lines.append(f"• {item['sku']} x{item['quantity']} 單價 NT${item['unitPrice']:,} 小計 NT${item['lineTotal']:,}")
                            lines.append(f"\n總金額：NT${result['totalAmount']:,}（{result.get('taxMode', '含稅')}）")
                            quote_db_id = result.get("quoteId", "")
                            pdf_dl = ""
                            if quote_db_id:
                                pdf_dl = f"https://web-production-87474.up.railway.app/pdf/{quote_db_id}"
                                lines.append(f"📄 PDF：{pdf_dl}")
                            await reply_message(reply_token, "\n".join(lines))
                            # Send to customer group
                            cust_lines = [f"您好，以下是您的報價單 {result['quoteNumber']}：\n"]
                            for item in result.get("items", []):
                                cust_lines.append(f"• {item['name']} x{item['quantity']}  NT${item['unitPrice']:,}")
                            cust_lines.append(f"\n總金額：NT${result['totalAmount']:,}（{result.get('taxMode', '含稅')}）")
                            if pdf_dl:
                                cust_lines.append(f"\n📄 報價單下載：{pdf_dl}")
                            cust_lines.append("\n如有任何問題請隨時告知，謝謝！")
                            await push_message(target.group_id, "\n".join(cust_lines))
                            await push_message(source_id, f"已發送報價單到 [{target_alias} {target.group_name}]")
                        else:
                            err = result.get("error", "未知錯誤") if result else "無法連接 ERP"
                            await reply_message(reply_token, f"建立報價單失敗：{err}")
                        continue

                    erp_data = ""
                    if is_product_query(msg_content):
                        # Extract product keywords from message
                        parsed = parse_product_query(msg_content)
                        keywords = parsed.get("product_keyword") or msg_content
                        # Search each keyword separately (split by 跟/和/、/,)
                        all_keywords = re.split(r"[跟和、,\s]+", keywords)
                        erp_lines = []
                        for kw in all_keywords:
                            kw = kw.strip()
                            if not kw:
                                continue
                            products = erp_search_with_fallback(kw)
                            if products and isinstance(products, list):
                                for p in products[:2]:
                                    quote = erp_query("quote", code=target_alias, sku=p["sku"])
                                    if quote and isinstance(quote, dict) and "error" not in quote:
                                        pr = quote.get("pricing", {})
                                        erp_lines.append(
                                            f"{p['sku']}：客戶價 NT${pr.get('unitPrice',0):,}"
                                            f"（建議售價 NT${pr.get('msrp',0):,}，折扣 {pr.get('discount',0)}%）"
                                            f"，{quote.get('availability','')}"
                                        )
                        if erp_lines:
                            erp_data = "\n\nERP 報價資料（客戶 " + target_alias + "）：\n" + "\n".join(erp_lines)

                    system_prompt = (
                        "你是藍圈科技的商務助理。請將以下訊息改寫成專業、禮貌的客戶溝通訊息。"
                        "使用繁體中文。簡潔即可，不要太長。務必帶入實際價格數字。"
                    )
                    if erp_data:
                        system_prompt += erp_data
                    polished = call_llm([
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": msg_content},
                    ], max_tokens=256)
                    await push_message(target.group_id, polished)
                    await reply_message(reply_token, f"已發送到 [{target_alias} {target.group_name}]：\n{polished}")
                else:
                    await reply_message(reply_token, f"找不到群組 {target_alias}，請先到該群組傳「綁定 {target_alias}」")
                continue

            # --- Create quote: 報價 K01 U7-Pro x40, U7-Lite x10 ---
            qm = QUOTE_RE.match(user_text)
            # Parse quote command
            cust_code = None
            items_str = None
            if qm and is_staff(user_id):
                cust_code = qm.group(1).upper()
                items_str = qm.group(2).strip()
            elif not qm and is_staff(user_id):
                # Try without customer code → use group alias
                qm_nc = QUOTE_NO_CODE_RE.match(user_text)
                if qm_nc and source_type == "group" and SessionLocal:
                    db = SessionLocal()
                    try:
                        alias_obj = db.query(GroupAlias).filter_by(group_id=source_id).first()
                    finally:
                        db.close()
                    if alias_obj:
                        cust_code = alias_obj.alias
                        items_str = qm_nc.group(1).strip()

            if cust_code and items_str:
                # Parse items - supports:
                # "U7-Pro x40, U7-Lite x10" (comma separated)
                # "1. U7-Pro x40\n2. 2216 x10" (numbered lines)
                quote_items = []
                # Split by newlines, commas, or 、
                parts = re.split(r"[\n,、]+", items_str)
                for part in parts:
                    part = re.sub(r"^\d+[\.\)、]\s*", "", part.strip())  # Remove "1." "2)" etc.
                    if not part:
                        continue
                    m_item = re.match(r"(.+?)\s*[xX×\*]\s*(\d+)\s*$", part)
                    if not m_item:
                        m_item = re.match(r"(.+?)\s+(\d+)\s*$", part)
                    if not m_item:
                        # No quantity specified, default to 1
                        m_item = re.match(r"(.+)", part)
                        if m_item and m_item.group(1).strip():
                            raw_sku = m_item.group(1).strip()
                            products = erp_search_with_fallback(raw_sku)
                            if products:
                                resolved_sku = products[0]["sku"]
                                quote_items.append({"sku": resolved_sku, "quantity": 1})
                            else:
                                await reply_message(reply_token, f"找不到商品「{raw_sku}」，請確認型號。")
                                quote_items = []
                                break
                            continue
                    if m_item:
                        raw_sku = m_item.group(1).strip()
                        qty = int(m_item.group(2))
                        # Resolve SKU via ERP search fallback
                        products = erp_search_with_fallback(raw_sku)
                        if products and len(products) == 1:
                            quote_items.append({"sku": products[0]["sku"], "quantity": qty})
                        elif products and len(products) > 1:
                            exact = [p for p in products if p["sku"].lower() == raw_sku.lower().replace(" ", "-")]
                            if exact:
                                quote_items.append({"sku": exact[0]["sku"], "quantity": qty})
                            else:
                                # Multiple matches → ask user to choose
                                options = "\n".join(
                                    f"  {i+1}. {p['sku']} — {p['name']}"
                                    for i, p in enumerate(products[:5])
                                )
                                await reply_message(
                                    reply_token,
                                    f"「{raw_sku}」找到多個商品，請用正確 SKU 重新報價：\n\n{options}"
                                )
                                quote_items = []
                                break
                        else:
                            await reply_message(reply_token, f"找不到商品「{raw_sku}」，請確認型號。")
                            quote_items = []
                            break

                if not quote_items:
                    await push_message(
                        source_id,
                        "無法解析報價品項。格式範例：\n\n報價 K01\n1. UCK-G2-SSD x1\n2. USW-PRO-XG-10-POE x1\n\n或：報價 K01 UCK-G2-SSD *1, CKG2-RM *1"
                    )
                    continue

                staff_email = get_staff_email(user_id)
                if not staff_email:
                    await reply_message(reply_token, "你的帳號沒有綁定 email，請重新「註冊員工」。")
                    continue

                result = erp_create_quote(cust_code, quote_items, staff_email)
                if result and result.get("success"):
                    lines = [
                        f"報價單 {result['quoteNumber']} 已建立！\n",
                        f"客戶：{result['customer']['name']}（{result['customer']['code']}）",
                        f"建立者：{result.get('createdBy', '')}",
                        "",
                    ]
                    for item in result.get("items", []):
                        lines.append(
                            f"• {item['sku']} x{item['quantity']}"
                            f"  單價 NT${item['unitPrice']:,}"
                            f"  小計 NT${item['lineTotal']:,}"
                        )
                    lines.append(f"\n總金額：NT${result['totalAmount']:,}（{result.get('taxMode', '含稅')}）")

                    await reply_message(reply_token, "\n".join(lines))

                    # Download PDF and create download link
                    pdf_url_raw = result.get("pdfDownload") or result.get("pdfUrl") or ""
                    quote_db_id = result.get("quoteId") or ""
                    # Extract quoteId from URL or use direct field
                    if not quote_db_id:
                        id_match = re.search(r"quoteId=([^&]+)", pdf_url_raw)
                        if id_match:
                            quote_db_id = id_match.group(1)
                    if quote_db_id:
                        try:
                            pdf_bytes = erp_download_pdf(quote_db_id)
                            if pdf_bytes:
                                pdf_cache[quote_db_id] = pdf_bytes
                                download_url = f"https://web-production-87474.up.railway.app/pdf/{quote_db_id}"
                                await push_message(
                                    source_id,
                                    f"📄 報價單 PDF 下載連結：\n{download_url}"
                                )
                            else:
                                logger.warning("PDF download returned empty")
                                await push_message(source_id, "⚠ PDF 下載失敗，請到 ERP 系統查看。")
                        except Exception as e:
                            logger.error(f"PDF download/send error: {e}")
                            await push_message(source_id, "⚠ PDF 產生中，請稍後到 ERP 系統下載。")
                    # Notify admin group
                    if admin_group and source_id != admin_group:
                        await push_message(
                            admin_group,
                            f"報價單 {result['quoteNumber']} 已建立\n"
                            f"客戶：{result['customer']['name']}\n"
                            f"金額：NT${result['totalAmount']:,}"
                        )
                else:
                    err = result.get("error", "未知錯誤") if result else "無法連接 ERP"
                    await reply_message(reply_token, f"建立報價單失敗：{err}")
                continue

            # --- $query → Price query ---
            pq = PRICE_QUERY_RE.match(user_text)
            if pq:
                query_text = pq.group(1).strip()
                staff_user = is_staff(user_id)
                # Parse to check if query contains a customer code
                parsed_pq = parse_product_query(query_text)
                explicit_customer = parsed_pq.get("customer_code")

                # Resolve customer code for pricing
                resolved_customer = None
                can_see_customer_price = False

                if staff_user and explicit_customer and is_admin_group:
                    can_see_customer_price = True
                    resolved_customer = explicit_customer
                elif staff_user and explicit_customer and not is_admin_group:
                    # Staff in non-admin group can't query other customers
                    can_see_customer_price = False
                elif staff_user and not explicit_customer:
                    # Staff without customer code → use group alias
                    can_see_customer_price = True
                    if SessionLocal:
                        db = SessionLocal()
                        try:
                            alias_obj = db.query(GroupAlias).filter_by(group_id=source_id).first()
                            if alias_obj:
                                resolved_customer = alias_obj.alias
                        finally:
                            db.close()
                elif not staff_user and not explicit_customer and SessionLocal:
                    # Non-staff in bound group → own price
                    db = SessionLocal()
                    try:
                        alias_obj = db.query(GroupAlias).filter_by(group_id=source_id).first()
                        if alias_obj:
                            can_see_customer_price = True
                            resolved_customer = alias_obj.alias
                    finally:
                        db.close()

                erp_context = build_erp_context(
                    query_text, source_id,
                    allow_customer_pricing=can_see_customer_price,
                    force_customer_code=resolved_customer,
                )
                if erp_context:
                    system_msg = SYSTEM_PROMPT + erp_context + "\n請根據以上資料整理成清楚的報價回覆。"
                    assistant_reply = call_llm(
                        [{"role": "system", "content": system_msg},
                         {"role": "user", "content": query_text}],
                    )
                else:
                    assistant_reply = f"找不到與「{query_text}」相關的商品。"
                await reply_message(reply_token, assistant_reply)
                continue

            # --- Commands (admin group sees all, others see own) ---
            if user_text in ("報告", "待辦清單") and is_staff(user_id):
                if is_admin_group:
                    await reply_message(reply_token, cmd_report_all())
                else:
                    await reply_message(reply_token, cmd_report(source_id))
                continue
            if user_text == "本週報告" and is_staff(user_id):
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
                    erp_context = ""
                    # Check current message and recent history for product context
                    recent_texts = [user_text]
                    for msg in conversation_history.get(user_id, [])[-4:]:
                        if msg.get("role") in ("user", "assistant"):
                            recent_texts.append(msg.get("content", ""))
                    if any(is_product_query(t) for t in recent_texts):
                        # Find the best keyword from recent messages
                        for t in recent_texts:
                            if is_product_query(t):
                                erp_context = build_erp_context(t, allow_customer_pricing=False)
                                if "[ERP 系統查無" not in erp_context:
                                    break
                    system_msg = SYSTEM_PROMPT + erp_context
                    history = conversation_history[user_id]
                    history.append({"role": "user", "content": user_text})
                    if len(history) > MAX_HISTORY:
                        conversation_history[user_id] = history[-MAX_HISTORY:]
                        history = conversation_history[user_id]
                    msgs = [{"role": "system", "content": system_msg}] + history
                    assistant_reply = call_llm(msgs)
                    history.append({"role": "assistant", "content": assistant_reply})
                except Exception as e:
                    logger.error(f"LLM chat error: {e}")
                    assistant_reply = "系統暫時無法回應，請稍後再試。"
                await reply_message(reply_token, assistant_reply)

                # Commitment extraction (skip product/price queries)
                if not is_product_query(user_text):
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

                # 「小幫手」trigger → conversation reply with ERP context
                if user_text.startswith(HELPER_TRIGGER):
                    question = user_text[len(HELPER_TRIGGER):].strip()
                    if question:
                        try:
                            has_alias = False
                            if SessionLocal:
                                db = SessionLocal()
                                try:
                                    has_alias = db.query(GroupAlias).filter_by(group_id=source_id).first() is not None
                                finally:
                                    db.close()
                            erp_context = build_erp_context(
                                question, source_id,
                                allow_customer_pricing=(is_admin_group or has_alias),
                            )
                            system_msg = SYSTEM_PROMPT + erp_context
                            history = conversation_history.get(f"group_{source_id}", [])
                            history.append({"role": "user", "content": question})
                            if len(history) > MAX_HISTORY:
                                history = history[-MAX_HISTORY:]
                            conversation_history[f"group_{source_id}"] = history
                            msgs = [{"role": "system", "content": system_msg}] + history
                            assistant_reply = call_llm(msgs)
                            history.append({"role": "assistant", "content": assistant_reply})
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
                    sender_is_staff = is_staff(user_id)
                    role_tag = "我方承諾" if sender_is_staff else "客戶要求"
                    save_commitments(
                        extraction, source_type, source_id, user_id, user_name,
                        source_name=group_name,
                    )
                    if not is_admin_group:
                        await reply_message(reply_token, f"已記錄（{role_tag}）：{items_text}")
                    if admin_group and not is_admin_group:
                        await push_message(
                            admin_group,
                            f"[{group_name}] [{role_tag}] {user_name}：{items_text}",
                        )

        except Exception as e:
            logger.error(f"Event processing error: {e}")
            try:
                await reply_message(reply_token, f"處理訊息時發生錯誤：{type(e).__name__}")
            except Exception:
                pass

    return {"status": "ok"}


@app.get("/pdf/{quote_id}")
async def download_pdf(quote_id: str):
    from fastapi.responses import Response
    # Try from cache first
    pdf_bytes = pdf_cache.get(quote_id)
    if not pdf_bytes:
        # Download from ERP on-the-fly
        pdf_bytes = erp_download_pdf(quote_id)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF not found")
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f"inline; filename={quote_id}.pdf"
    })


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
