"""Project-approved, narrowly scoped readers and notification destinations."""
import hashlib
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import tomllib


CAPABILITIES = {
    "project-files": ("Project files and tables", "Read local names and revisions", "local"),
    "microsoft-mail": ("Outlook", "Read the subjects of five recent messages", "Microsoft"),
    "microsoft-files": ("OneDrive", "Read the names of five drive items", "Microsoft"),
    "google-mail": ("Gmail", "Read up to five recent message subjects", "Google"),
    "google-drive": ("Google Drive", "Read the names of five files", "Google"),
    "aws-s3": ("Amazon S3", "List up to five objects in the configured bucket", "AWS"),
    "aws-bedrock": ("Amazon Bedrock", "List available foundation models", "AWS"),
    "email": ("Email notifications", "Deliver a link to the project inbox", "Notifications"),
    "teams": ("Teams notifications", "Deliver an approval or result link", "Notifications"),
    "slack": ("Slack notifications", "Deliver an approval or result link", "Notifications"),
}


def provisioned(root, project):
    path = Path(root) / "operations.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as source:
            config = tomllib.load(source)
        return config.get("projects", {}).get(project, {})
    except (OSError, ValueError, AttributeError):
        raise ValueError("The administrator needs to check operations.toml.") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, *, token=None, body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=8) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("Service response was too large.")
            if body is not None:
                return None
            return json.loads(raw)
    except HTTPError as exc:
        raise ValueError(f"Service returned HTTP {exc.code}. Check its permissions and connection.") from None
    except (URLError, TimeoutError, UnicodeError, OSError, json.JSONDecodeError):
        raise ValueError("The service did not return a usable response.") from None


def secret(cfg, field):
    value = os.environ.get(cfg.get(field) or "")
    if not value:
        raise ValueError("Ask an administrator to supply the registered credential.")
    return value


def clean(value):
    return " ".join(str(value or "").split())[:240]


def read_items(kind, cfg, local, *, token=None):
    if kind == "project-files":
        return [clean(item["name"]) for item in (local.get("files", []) + local.get("tables", []))[:5]]
    if kind.startswith("microsoft-"):
        token = token or secret(cfg, "token_env")
        files = kind == "microsoft-files"
        url = ("https://graph.microsoft.com/v1.0/me/drive/root/children?$top=5&$select=name" if files
               else "https://graph.microsoft.com/v1.0/me/messages?$top=5&$select=subject")
        records = fetch(url, token=token).get("value", [])
        return [clean(item.get("name" if files else "subject")) for item in records[:5]]
    if kind == "google-drive":
        url = "https://www.googleapis.com/drive/v3/files?pageSize=5&fields=files(name)&q=trashed%3Dfalse"
        return [clean(i.get("name")) for i in fetch(url, token=token or secret(cfg, "token_env")).get("files", [])[:5]]
    if kind == "google-mail":
        from urllib.parse import quote
        token = token or secret(cfg, "token_env")
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        results = []
        for item in fetch(url + "?maxResults=5", token=token).get("messages", [])[:5]:
            doc = fetch(url + "/" + quote(item["id"], safe="") + "?format=metadata&metadataHeaders=Subject", token=token)
            headers = doc.get("payload", {}).get("headers", [])
            results.append(next((clean(h["value"]) for h in headers if h.get("name", "").lower()=="subject"), "Untitled message"))
        return results
    if kind in ("aws-s3", "aws-bedrock"):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise ValueError("Install the optional AWS support in the server environment.") from None
        try:
            session = boto3.Session(profile_name=cfg.get("profile"), region_name=cfg.get("region"))
            options = Config(connect_timeout=4, read_timeout=8, retries={"max_attempts": 1})
            if kind == "aws-s3":
                response = session.client("s3", config=options).list_objects_v2(Bucket=cfg["bucket"], MaxKeys=5, Prefix=cfg.get("prefix",""))
                return [clean(obj["Key"]) for obj in response.get("Contents", [])]
            response = session.client("bedrock", config=options).list_foundation_models()
            return [clean(model["modelName"]) for model in response.get("modelSummaries", [])[:5]]
        except Exception:
            raise ValueError("AWS could not complete this read. Check the region, role, and resource permissions.") from None
    raise ValueError("Select a readable connection.")


def notify(kind, cfg, title, link):
    if kind == "email":
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg["sender"], cfg["recipient"], "Penstock · " + title
        msg.set_content(title + "\n\nOpen Penstock to review the current details.\n" + link)
        with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), timeout=8, context=ssl.create_default_context()) as smtp:
            if cfg.get("password_env"):
                smtp.login(cfg["username"], secret(cfg, "password_env"))
            smtp.send_message(msg)
        return
    url = notification_webhook(kind, cfg)
    if kind == "slack":
        payload = {"text": title + "\n" + link, "unfurl_links": False, "unfurl_media": False}
    else:
        payload = {"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive",
                   "content":{"type":"AdaptiveCard","version":"1.2","body":[{"type":"TextBlock","text":title,"wrap":True}],
                              "actions":[{"type":"Action.OpenUrl","title":"Review in Penstock","url":link}]}}]}
    fetch(url, body=payload)


def notification_webhook(kind, cfg):
    url = secret(cfg, "webhook_env")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = host == "hooks.slack.com" if kind == "slack" else kind == "teams" and any(host.endswith(s) for s in
              (".webhook.office.com", ".logic.azure.com", ".environment.api.powerplatform.com"))
    if parts.scheme != "https" or not allowed or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        raise ValueError("The notification destination does not match the selected service.")
    return url


def notification_binding(kind, cfg):
    if kind == "email":
        route = [cfg[field] for field in ("host", "sender", "recipient")]
        route.extend([int(cfg.get("port", 465)), cfg.get("username"), cfg.get("password_env")])
    else:
        route = [notification_webhook(kind, cfg)]
    raw = json.dumps([kind, cfg["studio_url"], route], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
