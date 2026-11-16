"""SMTP report delivery with explicit status, safe logging and attachment checks."""
from __future__ import annotations
import datetime as dt, email.message, hashlib, json, logging, re, smtplib, ssl
from email.utils import format_datetime, parseaddr
from pathlib import Path

LOG = logging.getLogger("weekly_report.email")

def mask_email(value: str) -> str:
    address = parseaddr(value or "")[1]
    if "@" not in address: return "[invalid-email]"
    local, domain = address.rsplit("@", 1)
    return f"{local[:1] or '*'}***@{domain}"

def _redact(text: object, secrets: tuple[str, ...] = ()) -> str:
    value = str(text)
    for secret in secrets:
        if secret: value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", lambda m: mask_email(m.group(0)), value)
    value = re.sub(r"(?i)(password|api[_ -]?key|token)\s*[:=]\s*\S+", r"\1=[REDACTED]", value)
    return value[:1000]

def validate_attachments(report_dir: Path, date_str: str) -> list[dict]:
    attachments = []
    for ext, maintype, subtype in (("pdf", "application", "pdf"), ("html", "text", "html")):
        path = report_dir / f"Weekly_Research_Report_{date_str}.{ext}"
        if not path.is_file(): raise FileNotFoundError(f"required attachment missing: {path.name}")
        data = path.read_bytes()
        if not data: raise ValueError(f"required attachment is empty: {path.name}")
        if ext == "pdf" and (not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]):
            raise ValueError(f"PDF structure check failed: {path.name}")
        if ext == "html" and not re.search(rb"(?is)<(?:!doctype\s+html|html\b)", data[:4096]):
            raise ValueError(f"HTML structure check failed: {path.name}")
        attachments.append({"path": path, "data": data, "maintype": maintype, "subtype": subtype,
                            "filename": path.name, "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
    return attachments

def build_message(report_dir: Path, date_str: str, recipient: str, sender: str,
                  start: str, end: str, count: int, test_mode: bool = False):
    attachments = validate_attachments(report_dir, date_str)
    subject = f"【精神医学与意识科学前沿周报】{date_str}｜抑郁症·游戏成瘾·味觉受体·脑屏障·意识"
    if test_mode: subject = "[测试邮件] " + subject
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
    domain = parseaddr(sender)[1].rsplit("@", 1)[-1]
    stable_id = hashlib.sha256(f"{date_str}|{recipient}|{subject}".encode("utf-8")).hexdigest()[:16]
    msg["Message-ID"] = f"<weekly-{date_str}-{stable_id}@{domain}>"
    prefix = "【测试投递】" if test_mode else ""
    msg.set_content(f"{prefix}覆盖日期：{start} 至 {end}\n本期正式纳入：{count} 篇。\n附件包括 PDF 与离线交互 HTML。\n")
    for item in attachments:
        msg.add_attachment(item["data"], maintype=item["maintype"], subtype=item["subtype"], filename=item["filename"])
    return msg, attachments

def deliver_report(report_dir: Path, date_str: str, start: str, end: str,
                   count: int, env: dict[str, str], send: bool, confirm: bool,
                   test_mode: bool = False) -> dict:
    report_dir = Path(report_dir)
    recipient = env.get("REPORT_RECIPIENT", "").strip()
    sender = (env.get("SMTP_FROM") or env.get("SMTP_USER") or "").strip()
    result = {"status": "PREVIEW_ONLY", "report_date": date_str,
              "recipient_masked": mask_email(recipient), "sender_masked": mask_email(sender),
              "smtp_connected": False, "smtp_authenticated": False,
              "smtp_submission": "not_attempted", "recipient_refusals": [],
              "attachments": [], "test_mode": bool(test_mode), "final_delivery_confirmed": False}
    secrets = (env.get("SMTP_PASSWORD", ""),)
    try:
        if not recipient: raise ValueError("REPORT_RECIPIENT is missing")
        if not sender: raise ValueError("SMTP_FROM and SMTP_USER are both missing")
        msg, attachments = build_message(report_dir, date_str, recipient, sender, start, end, count, test_mode)
        result["message_id"] = msg["Message-ID"]
        result["attachments"] = [{k: item[k] for k in ("filename", "size_bytes", "sha256")} for item in attachments]
        LOG.info("mail headers prepared: from=%s to=%s report=%s", mask_email(sender), mask_email(recipient), date_str)
        if not (send and confirm):
            LOG.info("email status=PREVIEW_ONLY recipient=%s", mask_email(recipient))
        else:
            required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")
            missing = [key for key in required if not env.get(key)]
            if missing: raise ValueError("missing SMTP configuration: " + ", ".join(missing))
            host, port = env["SMTP_HOST"].strip(), int(env["SMTP_PORT"])
            LOG.info("SMTP connecting host=%s port=%s", host, port)
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
                result["smtp_connected"] = True
                LOG.info("SMTP connection established")
                smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
                result["smtp_authenticated"] = True
                LOG.info("SMTP authentication succeeded account=%s", mask_email(env["SMTP_USER"]))
                refused = smtp.send_message(msg, from_addr=sender, to_addrs=[recipient])
                if refused:
                    result["status"], result["smtp_submission"] = "REJECTED", "recipient_refused"
                    result["recipient_refusals"] = [{"recipient_masked": mask_email(addr), "response": _redact(reason, secrets)} for addr, reason in refused.items()]
                    LOG.error("SMTP recipient rejected: %s", result["recipient_refusals"])
                else:
                    result["status"], result["smtp_submission"] = "SMTP_ACCEPTED", "accepted_by_smtp"
                    LOG.info("SMTP accepted message for recipient=%s message_id=%s", mask_email(recipient), msg["Message-ID"])
    except smtplib.SMTPRecipientsRefused as exc:
        result["status"], result["smtp_submission"] = "REJECTED", "recipient_refused"
        result["recipient_refusals"] = [{"recipient_masked": mask_email(addr), "response": _redact(reason, secrets)} for addr, reason in exc.recipients.items()]
        LOG.error("SMTP rejected all recipients: %s", result["recipient_refusals"])
    except Exception as exc:
        result["status"], result["smtp_submission"] = "FAILED", "failed_or_unknown"
        result["error"] = _redact(f"{type(exc).__name__}: {exc}", secrets)
        LOG.exception("SMTP delivery attempt failed; recipient=%s error=%s", mask_email(recipient), result["error"])
    result["final_delivery_confirmed"] = False
    result["delivery_note"] = ("SMTP acceptance is not proof of final inbox delivery; no DSN confirmation was received."
                               if result["status"] == "SMTP_ACCEPTED" else "Final delivery status is unknown.")
    (report_dir / "email_delivery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"status": result["status"], "from_masked": result["sender_masked"],
                "to_masked": result["recipient_masked"], "subject": msg["Subject"] if "msg" in locals() else "",
                "message_id": result.get("message_id", ""), "attachments": result["attachments"]}
    (report_dir / "email_preview.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
