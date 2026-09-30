"""Почтовый poller для Yandex Cloud Functions.

Точка входа: ``src.email_poller.handler``.

Забирает непрочитанные письма из INBOX, отправляет текст агенту AI Studio
(Responses API), отвечает отправителю по SMTP и помечает письмо как \\Seen.
Если заданы YDB_ENDPOINT и YDB_DATABASE, история переписки хранится в YDB
и передаётся агенту как контекст.
"""

import email
import email.policy
import email.utils
import imaplib
import json
import logging
import os
import smtplib
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RESPONSES_URL = (
    os.getenv("ENDPOINT") or "https://rest-assistant.api.cloud.yandex.net/v1/responses"
)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.yandex.ru")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
SMTP_USER = os.getenv("SMTP_USER", "") or IMAP_USER
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "") or IMAP_PASSWORD
HELPDESK_MAILBOX = os.getenv("HELPDESK_MAILBOX", "") or SMTP_USER

FOLDER_ID = os.getenv("FOLDER_ID", "")
AGENT_ID = os.getenv("AGENT_ID", "")
MODEL = os.getenv("MODEL", "")
YC_API_KEY = os.getenv("YC_API_KEY", "")

YDB_ENDPOINT = os.getenv("YDB_ENDPOINT", "")
YDB_DATABASE = os.getenv("YDB_DATABASE", "")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))


# ---------------------------------------------------------------- YDB history


class History:
    """История переписки в YDB: одна ветка на адрес отправителя."""

    TABLE = "email_history"

    def __init__(self):
        import ydb

        self._ydb = ydb
        self._driver = ydb.Driver(
            endpoint=YDB_ENDPOINT,
            database=YDB_DATABASE,
            credentials=ydb.credentials_from_env_variables(),
        )
        self._driver.wait(timeout=10, fail_fast=True)
        self._pool = ydb.QuerySessionPool(self._driver)
        self._pool.execute_with_retries(
            f"""
            CREATE TABLE IF NOT EXISTS `{self.TABLE}` (
                thread_id Utf8,
                created_at Timestamp,
                role Utf8,
                content Utf8,
                PRIMARY KEY (thread_id, created_at)
            )
            """
        )

    def load(self, thread_id: str) -> list[dict]:
        result_sets = self._pool.execute_with_retries(
            f"""
            DECLARE $thread_id AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT role, content, created_at FROM `{self.TABLE}`
            WHERE thread_id = $thread_id
            ORDER BY created_at DESC
            LIMIT $limit;
            """,
            {
                "$thread_id": (thread_id, self._ydb.PrimitiveType.Utf8),
                "$limit": (HISTORY_LIMIT, self._ydb.PrimitiveType.Uint64),
            },
        )
        rows = [row for rs in result_sets for row in rs.rows]
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def append(self, thread_id: str, role: str, content: str) -> None:
        self._pool.execute_with_retries(
            f"""
            DECLARE $thread_id AS Utf8;
            DECLARE $role AS Utf8;
            DECLARE $content AS Utf8;
            UPSERT INTO `{self.TABLE}` (thread_id, created_at, role, content)
            VALUES ($thread_id, CurrentUtcTimestamp(), $role, $content);
            """,
            {
                "$thread_id": (thread_id, self._ydb.PrimitiveType.Utf8),
                "$role": (role, self._ydb.PrimitiveType.Utf8),
                "$content": (content, self._ydb.PrimitiveType.Utf8),
            },
        )

    def close(self) -> None:
        self._pool.stop()
        self._driver.stop()


# ------------------------------------------------------------- Responses API


def _auth_header(context) -> str:
    if YC_API_KEY:
        return f"Api-Key {YC_API_KEY}"


    token = getattr(context, "token", None) or {}
    access_token = token.get("access_token") if isinstance(token, dict) else None
    access_token = access_token or os.getenv("YC_IAM_TOKEN", "")

    if not access_token:
        raise RuntimeError("No credentials for Responses API: set YC_API_KEY or SA")

    return f"Bearer {access_token}"


def _extract_text(data: dict) -> str:
    if data.get("output_text"):
        return data["output_text"]

    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))

    return "".join(parts).strip()


def ask_agent(messages: list[dict], context) -> str:
    payload: dict = {"input": messages}

    if AGENT_ID:
        payload["prompt"] = {"id": AGENT_ID}
    elif MODEL:
        payload["model"] = MODEL
    else:
        payload["model"] = f"gpt://{FOLDER_ID}/yandexgpt/latest"

    headers = {
        "Content-Type": "application/json",
        "Authorization": _auth_header(context),
    }
    if FOLDER_ID:
        headers["OpenAI-Project"] = FOLDER_ID

    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))

    text = _extract_text(data)
    if not text:
        raise RuntimeError(f"Empty response from agent: {data}")

    return text


# ---------------------------------------------------------------------- mail


def get_text(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain",))
    if body is None:
        return ""
    return body.get_content().strip()


def build_reply(original: EmailMessage, to_addr: str, text: str) -> EmailMessage:
    reply = EmailMessage()
    subject = original.get("Subject", "")

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()

    reply["Subject"] = subject
    reply["From"] = HELPDESK_MAILBOX
    reply["To"] = to_addr
    reply["Date"] = email.utils.formatdate(localtime=True)
    reply["Message-ID"] = email.utils.make_msgid()

    if original.get("Message-ID"):
        reply["In-Reply-To"] = original["Message-ID"]
        references = original.get("References", "")
        reply["References"] = f"{references} {original['Message-ID']}".strip()

    reply.set_content(text)
    return reply


def process_message(
    imap: imaplib.IMAP4_SSL,
    smtp: smtplib.SMTP_SSL,
    num: bytes,
    history: History | None,
    context,
) -> bool:
    status, data = imap.fetch(num, "(BODY.PEEK[])")
    if status != "OK" or not data or data[0] is None:
        logger.warning("Failed to fetch message %s", num)
        return False

    msg = email.message_from_bytes(data[0][1], policy=email.policy.default)
    _, from_addr = email.utils.parseaddr(msg.get("From", ""))
    from_addr = from_addr.lower()

    if not from_addr or from_addr == HELPDESK_MAILBOX.lower():
        imap.store(num, "+FLAGS", "\\Seen")
        return False

    text = get_text(msg)
    if not text:
        logger.info("Message %s from %s has no plain text body", num, from_addr)
        imap.store(num, "+FLAGS", "\\Seen")
        return False

    messages = history.load(from_addr) if history else []
    messages.append({"role": "user", "content": text})

    answer = ask_agent(messages, context)

    smtp.send_message(build_reply(msg, from_addr, answer))
    imap.store(num, "+FLAGS", "\\Seen")

    if history:
        history.append(from_addr, "user", text)
        history.append(from_addr, "assistant", answer)

    logger.info("Replied to %s (message %s)", from_addr, num)
    return True


def handler(event, context):
    history = History() if YDB_ENDPOINT and YDB_DATABASE else None
    processed, failed = 0, 0
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        imap.login(IMAP_USER, IMAP_PASSWORD)
        try:
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            nums = data[0].split() if status == "OK" and data and data[0] else []
            if not nums:
                return {"statusCode": 200, "body": json.dumps({"processed": 0})}

            with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                for num in nums:
                    try:
                        if process_message(imap, smtp, num, history, context):
                            processed += 1
                    except Exception:
                        # Письмо остаётся непрочитанным и будет обработано в следующий запуск
                        failed += 1
                        logger.exception("Failed to process message %s", num)
        finally:
            try:
                imap.close()
            except imaplib.IMAP4.error:
                pass
            imap.logout()
    finally:
        if history:
            history.close()

    return {
        "statusCode": 200,
        "body": json.dumps({"processed": processed, "failed": failed}),
    }
