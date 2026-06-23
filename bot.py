"""
Telegram Job Link Parser Bot
Send any job posting URL — the bot extracts title, company, and contact
info, saves it with status "applied" and the date you sent the link.
"""

import os
import re
import sqlite3
import csv
import io
import logging
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "jobs.db")
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

VALID_STATUSES = ["applied", "phone screen", "interview", "offer", "rejected", "withdrawn"]

STATUS_EMOJI = {
    "applied":      "📤",
    "phone screen": "📞",
    "interview":    "🗓",
    "offer":        "🎉",
    "rejected":     "❌",
    "withdrawn":    "🚫",
}

# ConversationHandler states
ASK_DATE, ASK_JOB, ASK_NEW_STATUS = range(3)


# ---------------------------------------------------------------------------
# Markdown helper
# ---------------------------------------------------------------------------

def escape_md(text) -> str:
    """Escape text for Telegram MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#\+\-=|{}.!\\])', r'\\\1', str(text or ""))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                title            TEXT,
                company          TEXT,
                url              TEXT UNIQUE,
                sent_at          TEXT,
                status           TEXT DEFAULT 'applied',
                contact_name     TEXT,
                contact_email    TEXT,
                contact_phone    TEXT,
                contact_linkedin TEXT
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for col, definition in [
            ("status",           "TEXT DEFAULT 'applied'"),
            ("contact_name",     "TEXT"),
            ("contact_email",    "TEXT"),
            ("contact_phone",    "TEXT"),
            ("contact_linkedin", "TEXT"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {definition}")
        conn.commit()


def save_job(
    title, company, url, sent_at,
    contact_name="", contact_email="", contact_phone="", contact_linkedin="",
):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (title, company, url, sent_at, status,
                contact_name, contact_email, contact_phone, contact_linkedin)
               VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?)""",
            (title, company, url, sent_at,
             contact_name, contact_email, contact_phone, contact_linkedin),
        )
        conn.commit()
        return cur.lastrowid or 0


def update_status(job_id: int, status: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
        )
        conn.commit()
        return cur.rowcount > 0


def list_jobs(status_filter: str = "") -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY sent_at DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY sent_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def jobs_on_date(day: str) -> list[dict]:
    """Return jobs whose sent_at starts with the given YYYY-MM-DD date."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs WHERE sent_at LIKE ? ORDER BY sent_at DESC",
            (f"{day}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def delete_job(job_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Scraping + extraction
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s]+")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_page_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    meta_bits = []
    for name in ("og:title", "og:site_name", "twitter:title", "twitter:site"):
        tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            meta_bits.append(f"META {name}: {tag['content']}")
    page_title = soup.title.get_text(strip=True) if soup.title else ""
    h1 = " | ".join(h.get_text(strip=True) for h in soup.find_all("h1")[:3])
    body = soup.get_text(separator=" ", strip=True)[:4000]
    return "\n".join(filter(None, [
        f"PAGE TITLE: {page_title}",
        f"H1: {h1}",
        *meta_bits,
        f"BODY EXCERPT:\n{body}",
    ]))


def extract_job_info(page_text: str, url: str) -> dict:
    message = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    "From the job posting page content below, extract the following.\n"
                    "If a field is not found, write 'N/A' for it.\n\n"
                    "Reply in this EXACT format and nothing else:\n"
                    "TITLE: <job title>\n"
                    "COMPANY: <hiring company name, NOT the ATS vendor>\n"
                    "CONTACT_NAME: <recruiter or hiring manager name>\n"
                    "CONTACT_EMAIL: <recruiter or contact email>\n"
                    "CONTACT_PHONE: <recruiter or contact phone number>\n"
                    "CONTACT_LINKEDIN: <LinkedIn profile URL of the contact>\n\n"
                    f"URL: {url}\n\n"
                    f"{page_text}"
                ),
            }
        ],
    )
    text = message.content[0].text.strip()
    fields = {
        "title": "Unknown", "company": "Unknown",
        "contact_name": "", "contact_email": "",
        "contact_phone": "", "contact_linkedin": "",
    }
    key_map = {
        "TITLE": "title", "COMPANY": "company",
        "CONTACT_NAME": "contact_name", "CONTACT_EMAIL": "contact_email",
        "CONTACT_PHONE": "contact_phone", "CONTACT_LINKEDIN": "contact_linkedin",
    }
    for line in text.splitlines():
        for prefix, field in key_map.items():
            if line.startswith(f"{prefix}:"):
                value = line.removeprefix(f"{prefix}:").strip()
                fields[field] = "" if value == "N/A" else value
    return fields


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_job(j: dict, show_url: bool = True) -> str:
    emoji = STATUS_EMOJI.get(j.get("status", "applied"), "📤")
    lines = [
        f"\#{j['id']} {emoji} *{escape_md(j['title'])}* — {escape_md(j['company'])}",
        f"Status: {escape_md(j.get('status', 'applied').title())}",
        f"Applied: {escape_md(j['sent_at'])}",
    ]
    if show_url:
        lines.append(escape_md(j["url"]))
    contacts = []
    if j.get("contact_name"):
        contacts.append(f"Name: {escape_md(j['contact_name'])}")
    if j.get("contact_email"):
        contacts.append(f"Email: {escape_md(j['contact_email'])}")
    if j.get("contact_phone"):
        contacts.append(f"Phone: {escape_md(j['contact_phone'])}")
    if j.get("contact_linkedin"):
        contacts.append(f"LinkedIn: {escape_md(j['contact_linkedin'])}")
    if contacts:
        lines.append("Contact — " + " \| ".join(contacts))
    return "\n".join(lines)


def parse_date_input(text: str) -> str | None:
    """Parse 'today', 'yesterday', or YYYY-MM-DD. Returns YYYY-MM-DD or None."""
    text = text.strip().lower()
    if text == "today":
        return date.today().strftime("%Y-%m-%d")
    if text == "yesterday":
        return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi\! Send me any job posting link and I'll save it as *applied*\.\n\n"
        "*Commands:*\n"
        "/list — all saved jobs\n"
        "/list <status> — filter by status\n"
        "/job <id> — full details for one job\n"
        "/status — update a job's status\n"
        "/export — download all jobs as CSV\n"
        "/delete <id> — remove a job\n\n"
        "*Valid statuses:*\n"
        "applied · phone screen · interview · offer · rejected · withdrawn",
        parse_mode="MarkdownV2",
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = URL_RE.findall(text)
    if not urls:
        await update.message.reply_text("Please send a valid job posting URL.")
        return
    url = urls[0]
    sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    msg = await update.message.reply_text("Fetching job details...")
    try:
        page_text = fetch_page_text(url)
        info = extract_job_info(page_text, url)
    except Exception as exc:
        logger.error("Failed to parse %s: %s", url, exc)
        await msg.edit_text(f"Could not parse that page: {exc}")
        return
    job_id = save_job(
        title=info["title"], company=info["company"],
        url=url, sent_at=sent_at,
        contact_name=info["contact_name"], contact_email=info["contact_email"],
        contact_phone=info["contact_phone"], contact_linkedin=info["contact_linkedin"],
    )
    if job_id == 0:
        await msg.edit_text(
            f"Already saved: *{escape_md(info['title'])}* at *{escape_md(info['company'])}*",
            parse_mode="MarkdownV2",
        )
        return
    job = get_job(job_id)
    await msg.edit_text(f"Saved\!\n\n{format_job(job)}", parse_mode="MarkdownV2")


async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /job <id>")
        return
    job = get_job(int(args[0]))
    if not job:
        await update.message.reply_text("No job found with that ID.")
        return
    await update.message.reply_text(format_job(job), parse_mode="MarkdownV2")


# -----------  Interactive /status conversation  -----------

async def cmd_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What date did you apply? Reply with:\n"
        "• today\n"
        "• yesterday\n"
        "• a date like 2026-06-23",
        reply_markup=ReplyKeyboardMarkup(
            [["today", "yesterday"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return ASK_DATE


async def received_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = parse_date_input(update.message.text or "")
    if not day:
        await update.message.reply_text(
            "Didn't recognise that date. Try 'today', 'yesterday', or YYYY-MM-DD."
        )
        return ASK_DATE

    jobs = jobs_on_date(day)
    if not jobs:
        await update.message.reply_text(
            f"No jobs found for {day}.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    context.user_data["status_jobs"] = jobs
    lines = [f"{i+1}. {j['title']} — {j['company']}" for i, j in enumerate(jobs)]
    keyboard = [[str(i + 1)] for i in range(len(jobs))]
    await update.message.reply_text(
        f"Jobs applied on {day}:\n\n" + "\n".join(lines) + "\n\nPick a number:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_JOB


async def received_job_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    jobs = context.user_data.get("status_jobs", [])
    if not text.isdigit() or not (1 <= int(text) <= len(jobs)):
        await update.message.reply_text(f"Please pick a number between 1 and {len(jobs)}.")
        return ASK_JOB

    chosen = jobs[int(text) - 1]
    context.user_data["status_job_id"] = chosen["id"]

    keyboard = [[s] for s in VALID_STATUSES]
    await update.message.reply_text(
        f"Updating: *{chosen['title']}* — {chosen['company']}\n\nNew status?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_NEW_STATUS


async def received_new_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = (update.message.text or "").strip().lower()
    job_id = context.user_data.get("status_job_id")

    if new_status not in VALID_STATUSES:
        await update.message.reply_text(
            f"Unknown status. Pick one: {', '.join(VALID_STATUSES)}"
        )
        return ASK_NEW_STATUS

    if update_status(job_id, new_status):
        emoji = STATUS_EMOJI[new_status]
        await update.message.reply_text(
            f"Updated to {emoji} {new_status.title()}",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text("Something went wrong.", reply_markup=ReplyKeyboardRemove())

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# -----------  Other commands  -----------

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_filter = " ".join(context.args).lower() if context.args else ""
    if status_filter and status_filter not in VALID_STATUSES:
        await update.message.reply_text(
            f"Unknown status. Valid: {', '.join(VALID_STATUSES)}"
        )
        return
    jobs = list_jobs(status_filter)
    if not jobs:
        label = f"'{status_filter}'" if status_filter else "any"
        await update.message.reply_text(f"No jobs with status {label}.")
        return
    lines = [format_job(j) for j in jobs]
    chunk, chunks = [], []
    for line in lines:
        if sum(len(l) for l in chunk) + len(line) > 3800:
            chunks.append("\n\n".join(chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append("\n\n".join(chunk))
    for part in chunks:
        await update.message.reply_text(part, parse_mode="MarkdownV2")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = list_jobs()
    if not jobs:
        await update.message.reply_text("No jobs saved yet.")
        return
    fields = ["id", "title", "company", "status", "sent_at",
              "contact_name", "contact_email", "contact_phone", "contact_linkedin", "url"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(jobs)
    bio = io.BytesIO(buf.getvalue().encode())
    bio.name = "jobs.csv"
    await update.message.reply_document(
        bio, filename="jobs.csv", caption=f"{len(jobs)} jobs exported."
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /delete <id>")
        return
    job_id = int(args[0])
    if delete_job(job_id):
        await update.message.reply_text(f"Job #{job_id} deleted.")
    else:
        await update.message.reply_text(f"No job found with id {job_id}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    init_db()
    app = Application.builder().token(token).build()

    status_conv = ConversationHandler(
        entry_points=[CommandHandler("status", cmd_status_start)],
        states={
            ASK_DATE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, received_date)],
            ASK_JOB:        [MessageHandler(filters.TEXT & ~filters.COMMAND, received_job_choice)],
            ASK_NEW_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_new_status)],
        },
        fallbacks=[CommandHandler("cancel", cancel_status)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(status_conv)
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
