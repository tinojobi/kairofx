"""Telegram alerts. Silent if not configured."""
import html
import logging
import re

import requests

import config

log = logging.getLogger(__name__)

_PARSE_ERROR_SNIPPET = "can't parse entities"


def _strip_markdown(text: str) -> str:
    """Best-effort plain-text fallback for Telegram Markdown failures."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    return text.replace("`", "").replace("*", "").replace("_", "")


def escape(text: object) -> str:
    return html.escape(str(text), quote=False)


def _post(url: str, payload: dict) -> requests.Response:
    return requests.post(url, data=payload, timeout=10)


def _coerce_response_json(response: requests.Response) -> dict | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _build_delivery_result(mode: str, response: requests.Response | None = None, *, delivered: bool = False,
                           fallback_used: bool = False, fallback_from: str | None = None,
                           exception: Exception | None = None) -> dict:
    result = {
        "delivered": delivered,
        "mode": mode,
        "fallback_used": fallback_used,
    }
    if fallback_from:
        result["fallback_from"] = fallback_from
    if response is not None:
        result["status_code"] = response.status_code
        payload = _coerce_response_json(response)
        if payload is not None:
            result["telegram_ok"] = bool(payload.get("ok"))
            if payload.get("description"):
                result["description"] = payload["description"]
            message = payload.get("result") or {}
            if isinstance(message, dict):
                if "message_id" in message:
                    result["message_id"] = message["message_id"]
                if "date" in message:
                    result["telegram_date"] = message["date"]
                chat = message.get("chat") or {}
                if isinstance(chat, dict) and "id" in chat:
                    result["chat_id"] = chat["id"]
        elif response.text:
            result["response_text"] = response.text[:500]
    if exception is not None:
        result["exception"] = str(exception)
    return result


def _send_plain(url: str, text: str, *, fallback_from: str | None = None) -> dict:
    retry = _post(url, {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    })
    if retry.ok:
        return _build_delivery_result(
            "plain",
            retry,
            delivered=True,
            fallback_used=fallback_from is not None,
            fallback_from=fallback_from,
        )
    log.warning("Telegram fallback send failed: %s %s", retry.status_code, retry.text)
    return _build_delivery_result(
        "plain",
        retry,
        delivered=False,
        fallback_used=fallback_from is not None,
        fallback_from=fallback_from,
    )


def send(text: str) -> dict:
    """Legacy Markdown sender with plain-text fallback."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skipping alert")
        return {"delivered": False, "mode": "markdown", "skipped": True, "reason": "not_configured"}

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    markdown_payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        r = _post(url, markdown_payload)
        if r.ok:
            return _build_delivery_result("markdown", r, delivered=True)

        if r.status_code == 400 and _PARSE_ERROR_SNIPPET in r.text:
            delivery = _send_plain(url, _strip_markdown(text), fallback_from="markdown")
            if delivery["delivered"]:
                log.info("Telegram Markdown parse failed; delivered plain-text fallback")
            return delivery

        log.warning("Telegram send failed: %s %s", r.status_code, r.text)
        return _build_delivery_result("markdown", r, delivered=False)
    except Exception as e:
        log.warning("Telegram exception: %s", e)
        return _build_delivery_result("markdown", delivered=False, exception=e)


def send_html(text: str) -> dict:
    """Preferred sender: pre-escaped HTML with plain-text fallback."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skipping alert")
        return {"delivered": False, "mode": "html", "skipped": True, "reason": "not_configured"}

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = _post(url, payload)
        if r.ok:
            return _build_delivery_result("html", r, delivered=True)

        if r.status_code == 400 and _PARSE_ERROR_SNIPPET in r.text:
            plain_text = re.sub(r"</?[^>]+>", "", text)
            delivery = _send_plain(url, html.unescape(plain_text), fallback_from="html")
            if delivery["delivered"]:
                log.info("Telegram HTML parse failed; delivered plain-text fallback")
            return delivery

        log.warning("Telegram send failed: %s %s", r.status_code, r.text)
        return _build_delivery_result("html", r, delivered=False)
    except Exception as e:
        log.warning("Telegram exception: %s", e)
        return _build_delivery_result("html", delivered=False, exception=e)
