import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config_manager import ConfigManager
from api_client import AlpinnApiClient, AlpinnApiError, ApiRateLimitError


load_dotenv()


def normalize_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("L'URL doit commencer par http:// ou https://")
    return url.rstrip("/")


class AlpinnBot(commands.Bot):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = ConfigManager("bot_config.json")
        self.api_client = AlpinnApiClient(rate_limit_seconds=60)
        self.auto_refresh_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        save_reconciled_config()
        if self.auto_refresh_task is None:
            self.auto_refresh_task = asyncio.create_task(auto_refresh_worker())
        print(f"Connecte en tant que {self.user} (ID: {self.user.id if self.user else 'N/A'})")


intents = discord.Intents.default()
intents.message_content = True
bot = AlpinnBot(command_prefix="!", intents=intents, help_command=None)
REBOOT_REQUESTED = False
AUTOSTART_SCRIPT_RELATIVE_PATH = os.path.join("RUN_Ubuntu", "Manage_Autostart.sh")
CONFIG_BACKUP_DIR_NAME = "config_backups"
UPDATE_DELAY_FILE_NAME = ".update_poll_minutes"
BACKGROUND_MODE_FILE_NAME = ".background_mode"


ENDPOINT_NAMES = ["association", "news", "statuts", "staff", "activities", "events"]
META_KEYS = {
    "success",
    "version",
    "resource",
    "timestamp",
    "timezone",
    "request_id",
    "status_code",
}


def build_request_config() -> Dict[str, Any]:
    data = bot.config.load()
    base_url = data.get("base_url")
    api_key = os.getenv("ALPINN_API_KEY", "").strip()
    if not base_url or not api_key:
        raise ValueError("Configuration incomplete. Utilise !set_base_url et definis ALPINN_API_KEY dans l'environnement.")
    channels = data.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}
    auto_enabled_endpoints = data.get("auto_enabled_endpoints", [])
    if not isinstance(auto_enabled_endpoints, list):
        auto_enabled_endpoints = []
    auto_messages = data.get("auto_messages", {})
    if not isinstance(auto_messages, dict):
        auto_messages = {}
    return {
        "base_url": base_url,
        "api_key": api_key,
        "channels": channels,
        "auto_enabled_endpoints": auto_enabled_endpoints,
        "auto_messages": auto_messages,
    }


def is_admin_user(ctx: commands.Context) -> bool:
    return bool(ctx.guild and getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.administrator)


@bot.check
def admin_only_global_check(ctx: commands.Context) -> bool:
    return is_admin_user(ctx)


def has_alpinn_api_key() -> bool:
    return bool(os.getenv("ALPINN_API_KEY", "").strip())


async def ensure_api_key_or_warn(ctx: commands.Context) -> bool:
    if has_alpinn_api_key():
        return True
    await ctx.send(
        "Impossible d'activer: la cle API AlpInn.ch est manquante. "
        "Definis `ALPINN_API_KEY` dans `.env` puis redemarre le bot."
    )
    return False


def endpoint_channel_ids(channels: Dict[str, Any], endpoint_name: str) -> List[int]:
    raw = channels.get(endpoint_name)
    if raw is None:
        return []
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, str) and raw.isdigit():
        return [int(raw)]
    if isinstance(raw, list):
        result: List[int] = []
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    return []


def normalize_channels(channels: Any) -> Dict[str, List[int]]:
    if not isinstance(channels, dict):
        channels = {}
    normalized: Dict[str, List[int]] = {}
    for ep in ENDPOINT_NAMES:
        ids = endpoint_channel_ids(channels, ep)
        unique_ids = []
        for cid in ids:
            if cid not in unique_ids:
                unique_ids.append(cid)
        if unique_ids:
            normalized[ep] = unique_ids
    return normalized


def reconcile_config_state(base: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(base)
    channels = normalize_channels(cfg.get("channels", {}))

    enabled = cfg.get("auto_enabled_endpoints", [])
    if not isinstance(enabled, list):
        enabled = []
    enabled = [ep for ep in enabled if ep in ENDPOINT_NAMES and channels.get(ep)]

    auto_messages = cfg.get("auto_messages", {})
    auto_signatures = cfg.get("auto_signatures", {})
    if not isinstance(auto_messages, dict):
        auto_messages = {}
    if not isinstance(auto_signatures, dict):
        auto_signatures = {}

    cleaned_auto_messages: Dict[str, Dict[str, int]] = {}
    cleaned_auto_signatures: Dict[str, Dict[str, str]] = {}
    for ep in ENDPOINT_NAMES:
        valid_channels = {str(cid) for cid in channels.get(ep, [])}
        msgs = endpoint_auto_messages(auto_messages, ep)
        signs = endpoint_auto_signatures(auto_signatures, ep)
        msgs = {k: v for k, v in msgs.items() if k in valid_channels}
        signs = {k: v for k, v in signs.items() if k in valid_channels and k in msgs}
        if msgs:
            cleaned_auto_messages[ep] = msgs
        if signs:
            cleaned_auto_signatures[ep] = signs

    news_channels = {str(cid) for cid in channels.get("news", [])}
    assoc_channels = {str(cid) for cid in channels.get("association", [])}

    auto_news_messages = cfg.get("auto_news_messages", {})
    auto_news_signatures = cfg.get("auto_news_signatures", {})
    if not isinstance(auto_news_messages, dict):
        auto_news_messages = {}
    if not isinstance(auto_news_signatures, dict):
        auto_news_signatures = {}

    cleaned_news_messages: Dict[str, Dict[str, int]] = {}
    cleaned_news_signatures: Dict[str, Dict[str, str]] = {}
    for channel_key, by_news in auto_news_messages.items():
        if channel_key not in news_channels or not isinstance(by_news, dict):
            continue
        cleaned_items: Dict[str, int] = {}
        for key, msg_id in by_news.items():
            try:
                cleaned_items[str(key)] = int(msg_id)
            except (TypeError, ValueError):
                continue
        if cleaned_items:
            cleaned_news_messages[channel_key] = cleaned_items
            sigs = auto_news_signatures.get(channel_key, {})
            if isinstance(sigs, dict):
                cleaned_news_signatures[channel_key] = {
                    str(k): str(v) for k, v in sigs.items() if str(k) in cleaned_items
                }

    auto_association_messages = cfg.get("auto_association_messages", {})
    auto_association_signatures = cfg.get("auto_association_signatures", {})
    if not isinstance(auto_association_messages, dict):
        auto_association_messages = {}
    if not isinstance(auto_association_signatures, dict):
        auto_association_signatures = {}

    cleaned_assoc_messages: Dict[str, Dict[str, int]] = {}
    cleaned_assoc_signatures: Dict[str, Dict[str, str]] = {}
    for channel_key, by_assoc in auto_association_messages.items():
        if channel_key not in assoc_channels or not isinstance(by_assoc, dict):
            continue
        cleaned_items: Dict[str, int] = {}
        for key, msg_id in by_assoc.items():
            try:
                cleaned_items[str(key)] = int(msg_id)
            except (TypeError, ValueError):
                continue
        if cleaned_items:
            cleaned_assoc_messages[channel_key] = cleaned_items
            sigs = auto_association_signatures.get(channel_key, {})
            if isinstance(sigs, dict):
                cleaned_assoc_signatures[channel_key] = {
                    str(k): str(v) for k, v in sigs.items() if str(k) in cleaned_items
                }

    cfg["channels"] = channels
    cfg["auto_enabled_endpoints"] = enabled
    cfg["auto_messages"] = cleaned_auto_messages
    cfg["auto_signatures"] = cleaned_auto_signatures
    cfg["auto_news_messages"] = cleaned_news_messages
    cfg["auto_news_signatures"] = cleaned_news_signatures
    cfg["auto_association_messages"] = cleaned_assoc_messages
    cfg["auto_association_signatures"] = cleaned_assoc_signatures
    return cfg


def save_reconciled_config(patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = bot.config.load()
    if patch:
        cfg.update(patch)
    cfg = reconcile_config_state(cfg)
    bot.config.update(cfg)
    return cfg


def config_file_path() -> Path:
    return Path(bot.config.path)


def config_backup_dir() -> Path:
    return config_file_path().parent / CONFIG_BACKUP_DIR_NAME


def list_config_backups() -> List[Path]:
    backup_dir = config_backup_dir()
    if not backup_dir.exists():
        return []
    files = [p for p in backup_dir.glob("*.json") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def create_config_backup(prefix: str = "bot_config_backup") -> Path:
    cfg_path = config_file_path()
    if not cfg_path.exists():
        bot.config.load()
    backup_dir = config_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{prefix}_{timestamp}.json"
    shutil.copy2(cfg_path, backup_path)
    return backup_path


def endpoint_auto_messages(auto_messages: Dict[str, Any], endpoint_name: str) -> Dict[str, int]:
    raw = auto_messages.get(endpoint_name, {})
    if isinstance(raw, dict):
        normalized: Dict[str, int] = {}
        for key, value in raw.items():
            try:
                normalized[str(int(key))] = int(value)
            except (TypeError, ValueError):
                continue
        return normalized
    return {}


def endpoint_auto_signatures(auto_signatures: Dict[str, Any], endpoint_name: str) -> Dict[str, str]:
    raw = auto_signatures.get(endpoint_name, {})
    if isinstance(raw, dict):
        normalized: Dict[str, str] = {}
        for key, value in raw.items():
            try:
                normalized[str(int(key))] = str(value)
            except (TypeError, ValueError):
                continue
        return normalized
    return {}


def extract_query_pairs(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    params: Dict[str, str] = {}
    parts = [p.strip() for p in raw.split() if p.strip()]
    for part in parts:
        if "=" not in part:
            raise ValueError(f"Parametre invalide: {part}. Format attendu: cle=valeur")
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Parametre invalide: {part}")
        params[key] = value
    return params


def endpoint_path(name: str) -> str:
    allowed = {
        "association": "/api/v1/association.php",
        "news": "/api/v1/news.php",
        "statuts": "/api/v1/statuts.php",
        "staff": "/api/v1/staff.php",
        "activities": "/api/v1/activities.php",
        "events": "/api/v1/events.php",
    }
    if name not in allowed:
        raise ValueError(f"Endpoint inconnu: {name}")
    return allowed[name]


def has_displayable_data(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, (list, tuple, set)):
        return len(payload) > 0
    if isinstance(payload, dict):
        if not payload:
            return False
        # Common API wrappers: {"data": []}, {"items": []}, {"results": []}
        for key in ("data", "items", "results"):
            if key in payload:
                value = payload.get(key)
                if isinstance(value, (list, tuple, set, dict)):
                    return len(value) > 0
                return value is not None and str(value).strip() != ""
        return True
    if isinstance(payload, str):
        return payload.strip() != ""
    return True


def no_data_message(ep_name: str) -> str:
    return f"Aucune donnee disponible actuellement pour l'endpoint `{ep_name}`."


def looks_like_image_url(value: str) -> bool:
    url = value.strip().lower()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    return any(url.endswith(ext) for ext in image_exts) or "image" in url


def find_image_url(value: Any, depth: int = 0) -> Optional[str]:
    if depth > 3:
        return None
    if isinstance(value, str) and looks_like_image_url(value):
        return value.strip()
    if isinstance(value, dict):
        preferred_keys = [
            "image",
            "image_url",
            "thumbnail",
            "thumbnail_url",
            "photo",
            "picture",
            "banner",
            "cover",
            "avatar",
            "url",
        ]
        for key in preferred_keys:
            if key in value:
                found = find_image_url(value.get(key), depth + 1)
                if found:
                    return found
        for sub in value.values():
            found = find_image_url(sub, depth + 1)
            if found:
                return found
    if isinstance(value, list):
        for sub in value[:10]:
            found = find_image_url(sub, depth + 1)
            if found:
                return found
    return None


def extract_image_url(payload: Any) -> Optional[str]:
    return find_image_url(payload, depth=0)


def build_image_embed(ep_name: str, payload: Any, for_auto: bool) -> Optional[discord.Embed]:
    image_url = extract_image_url(payload)
    if not image_url:
        return None
    title = f"{ep_name.upper()} - Image"
    description = "Mise a jour auto" if for_auto else "Resultat"
    embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
    embed.set_image(url=image_url)
    return embed


def truncate_text(value: str, max_len: int = 220) -> str:
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def html_to_markdown(value: str) -> str:
    text = value
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*p[^>]*>", "", text)
    text = re.sub(r"(?i)<\s*(strong|b)\s*>", "**", text)
    text = re.sub(r"(?i)</\s*(strong|b)\s*>", "**", text)
    text = re.sub(r"(?i)<\s*(em|i)\s*>", "*", text)
    text = re.sub(r"(?i)</\s*(em|i)\s*>", "*", text)
    text = re.sub(r"(?i)<\s*u\s*>", "__", text)
    text = re.sub(r"(?i)</\s*u\s*>", "__", text)
    text = re.sub(r"(?i)<\s*(s|strike)\s*>", "~~", text)
    text = re.sub(r"(?i)</\s*(s|strike)\s*>", "~~", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)</\s*li\s*>", "", text)
    text = re.sub(r"(?i)</?\s*(ul|ol)\s*>", "\n", text)
    text = re.sub(
        r"(?is)<\s*a[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</\s*a\s*>",
        lambda m: f"[{html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip() or m.group(1)}]({m.group(1).strip()})",
        text,
    )
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_rich_text(value: str, max_len: int) -> str:
    text = html_to_markdown(value) if ("<" in value and ">" in value) else value
    return truncate_text(text, max_len)


def item_title(item: Dict[str, Any], index: int) -> str:
    for key in ("title", "name", "nom", "label", "event", "headline"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return format_rich_text(val, 80)
    return f"Element {index}"


def item_summary_lines(item: Dict[str, Any], max_fields: int = 4) -> List[str]:
    preferred = [
        "date",
        "created_at",
        "published_at",
        "updated_at",
        "start_at",
        "end_at",
        "status",
        "author",
        "location",
        "description",
        "excerpt",
        "subtitle",
    ]
    blocked_keys = {
        "id",
        "content",
        "summary",
        "category",
        "categories",
        "body",
        "text",
        "html",
        "markdown",
    }
    lines: List[str] = []
    used = 0

    for key in preferred:
        if key in item and used < max_fields:
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                str_value = str(value)
                if isinstance(value, str):
                    str_value = format_rich_text(value, 160)
                else:
                    str_value = truncate_text(str_value, 160)
                lines.append(f"`{key}`: {str_value}")
                used += 1

    if used < max_fields:
        for key, value in item.items():
            key_lower = key.lower()
            if key in preferred:
                continue
            if key_lower in blocked_keys or key_lower.endswith("_id"):
                continue
            if used >= max_fields:
                break
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                str_value = str(value)
                if isinstance(value, str):
                    str_value = format_rich_text(value, 160)
                else:
                    str_value = truncate_text(str_value, 160)
                lines.append(f"`{key}`: {str_value}")
                used += 1
    return lines


def extract_news_url(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("url", "scroll_url", "external_url", "news_url", "link", "permalink"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip().startswith(("http://", "https://")):
                return raw.strip()
        links = value.get("links")
        if isinstance(links, dict):
            for key in ("self", "public", "web", "details"):
                raw = links.get(key)
                if isinstance(raw, str) and raw.strip().startswith(("http://", "https://")):
                    return raw.strip()
    return None


def news_item_key(item: Dict[str, Any], index: int) -> str:
    url = extract_news_url(item)
    if url:
        return f"url:{url}"
    slug = item.get("slug")
    if isinstance(slug, str) and slug.strip():
        return f"slug:{slug.strip()}"
    item_id = item.get("id")
    if item_id is not None and str(item_id).strip():
        return f"id:{item_id}"
    title = item.get("title") or item.get("name") or item.get("headline")
    if isinstance(title, str) and title.strip():
        return f"title:{title.strip().lower()}"
    return f"idx:{index}"


def news_item_title(item: Dict[str, Any], index: int) -> str:
    title = item.get("title") or item.get("name") or item.get("headline")
    if isinstance(title, str) and title.strip():
        return format_rich_text(title, 120)
    return f"News {index}"


def news_item_text(item: Dict[str, Any]) -> str:
    for key in ("content", "article", "body", "text", "description", "summary", "excerpt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return format_rich_text(value, 1400)
    return "Aucun texte disponible."


def news_message_content(item: Dict[str, Any], index: int, for_auto: bool) -> str:
    epoch = int(time.time())
    title = news_item_title(item, index)
    body = news_item_text(item)
    lines = [f"**{title}**"]
    if for_auto:
        lines.append(f"Mise a jour <t:{epoch}:R>")
    lines.append(body)
    news_url = extract_news_url(item)
    if news_url:
        lines.append(f"[Voir la news]({news_url})")
    text = "\n\n".join([line for line in lines if line.strip()])
    if len(text) <= 1900:
        return text
    return text[:1880] + "\n..."


def news_message_signature(item: Dict[str, Any], index: int, for_auto: bool) -> str:
    content = news_message_content(item, index, for_auto)
    image_url = extract_image_url(item) or ""
    raw = f"{content}\n[image]{image_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def association_sections(payload: Any) -> Dict[str, Any]:
    primary = pick_primary_block(payload)
    if isinstance(primary, dict):
        sections: Dict[str, Any] = {}
        for key, value in primary.items():
            if key in META_KEYS:
                continue
            if value is None:
                continue
            sections[key] = value
        return sections
    return {}


def association_section_title(name: str) -> str:
    mapping = {
        "members": "Membres",
        "membres": "Membres",
        "values": "Valeurs",
        "valeurs": "Valeurs",
        "volunteers": "Benevoles",
        "benevoles": "Benevoles",
        "partners": "Partenaires",
        "partenaires": "Partenaires",
        "reports": "Rapports",
        "rapports": "Rapports",
        "association_url": "Lien Association",
    }
    lower = name.lower()
    return mapping.get(lower, name.replace("_", " ").capitalize())


def association_section_content(name: str, value: Any, for_auto: bool) -> str:
    title = association_section_title(name)
    epoch = int(time.time())
    header = f"**ASSOCIATION - {title}**"
    lines = [header]
    if for_auto:
        lines.append(f"Mise a jour <t:{epoch}:R>")

    if isinstance(value, str):
        txt = format_rich_text(value, 1500)
        if value.startswith(("http://", "https://")):
            lines.append(f"[Voir]({value})")
        else:
            lines.append(txt)
        return "\n\n".join(lines)[:1900]

    if isinstance(value, list):
        if not value:
            lines.append("Aucune donnee.")
            return "\n\n".join(lines)[:1900]
        max_items = 8
        for idx, item in enumerate(value[:max_items], start=1):
            if isinstance(item, dict):
                lines.append(f"**{idx}. {item_title(item, idx)}**")
                summary = item_summary_lines(item, max_fields=4)
                if summary:
                    for s in summary:
                        lines.append(f"- {s}")
            else:
                lines.append(f"- {truncate_text(str(item), 200)}")
        if len(value) > max_items:
            lines.append(f"... et {len(value) - max_items} autre(s).")
        content = "\n".join(lines)
        return content[:1900]

    if isinstance(value, dict):
        count = 0
        for k, v in value.items():
            if k in META_KEYS:
                continue
            if isinstance(v, (str, int, float, bool)) and str(v).strip():
                vv = format_rich_text(v, 220) if isinstance(v, str) else truncate_text(str(v), 220)
                lines.append(f"- `{k}`: {vv}")
                count += 1
            if count >= 10:
                lines.append("- ...")
                break
        if count == 0:
            compact = truncate_text(json.dumps(value, ensure_ascii=False), 1400)
            lines.append(f"```json\n{compact}\n```")
        content = "\n".join(lines)
        return content[:1900]

    lines.append(truncate_text(str(value), 1500))
    return "\n\n".join(lines)[:1900]


def association_section_signature(name: str, value: Any, for_auto: bool) -> str:
    content = association_section_content(name, value, for_auto)
    image_url = extract_image_url(value) or ""
    raw = f"{content}\n[image]{image_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_items(payload: Any, depth: int = 0) -> Optional[List[Any]]:
    if depth > 3:
        return None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "rows", "news", "events", "posts", "articles"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = extract_items(value, depth + 1)
                if nested is not None:
                    return nested
        for value in payload.values():
            if isinstance(value, dict):
                nested = extract_items(value, depth + 1)
                if nested is not None:
                    return nested
    return None


def pick_primary_block(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    for key in ("association", "data", "result", "content", "details", "payload"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            return value
    for key, value in payload.items():
        if key in META_KEYS:
            continue
        if isinstance(value, (dict, list)):
            return value
    return payload


def styled_payload_content(ep_name: str, payload: Any, for_auto: bool) -> str:
    epoch = int(time.time())
    header = f"**{ep_name.upper()}** - Mise a jour <t:{epoch}:R>" if for_auto else f"**{ep_name.upper()}**"
    if not has_displayable_data(payload):
        return f"{header}\n{no_data_message(ep_name)}"

    primary_payload = pick_primary_block(payload)
    items = extract_items(primary_payload)
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    lines: List[str] = [header]
    if isinstance(items, list):
        lines.append(f"Total: **{len(items)}**")
        if isinstance(pagination, dict):
            page = pagination.get("page")
            total_pages = pagination.get("total_pages")
            total = pagination.get("total")
            if page is not None and total_pages is not None:
                lines.append(f"Page **{page}/{total_pages}**")
            if total is not None:
                lines.append(f"Total API: **{total}**")
        if isinstance(meta, dict) and meta:
            lines.append(f"Meta: {truncate_text(json.dumps(meta, ensure_ascii=False), 200)}")
        if not items:
            lines.append(no_data_message(ep_name))
            return "\n".join(lines)

        max_items = 5
        for idx, raw in enumerate(items[:max_items], start=1):
            if isinstance(raw, dict):
                lines.append(f"\n**{idx}. {item_title(raw, idx)}**")
                for field_line in item_summary_lines(raw, max_fields=4):
                    lines.append(f"- {field_line}")
                if ep_name == "news":
                    news_url = extract_news_url(raw)
                    if news_url:
                        lines.append(f"- [Voir la news]({news_url})")
            else:
                lines.append(f"\n**{idx}.** {truncate_text(str(raw), 220)}")

        if len(items) > max_items:
            lines.append(f"\n... et **{len(items) - max_items}** autre(s) element(s).")
    elif isinstance(primary_payload, dict):
        # Vue compacte pour objet simple.
        field_count = 0
        for key, value in primary_payload.items():
            if key in META_KEYS:
                continue
            if field_count >= 8:
                lines.append("- ...")
                break
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                str_value = str(value)
                if isinstance(value, str):
                    str_value = format_rich_text(value, 200)
                else:
                    str_value = truncate_text(str_value, 200)
                lines.append(f"- `{key}`: {str_value}")
                field_count += 1
        if field_count == 0:
            compact_json = json.dumps(primary_payload, ensure_ascii=False)
            lines.append(f"```json\n{truncate_text(compact_json, 1200)}\n```")
    else:
        lines.append(f"- {truncate_text(str(primary_payload), 1200)}")

    content = "\n".join(lines)
    if len(content) <= 1900:
        return content
    return content[:1880] + "\n..."


def endpoint_message_content(ep_name: str, payload: Dict[str, Any]) -> str:
    return styled_payload_content(ep_name, payload, for_auto=True)


def payload_signature(ep_name: str, payload: Any) -> str:
    image_url = extract_image_url(payload) or ""
    content = endpoint_message_content(ep_name, payload)
    raw = f"{content}\n[image]{image_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def upsert_endpoint_message(ep_name: str, payload: Dict[str, Any], channel_id: int) -> None:
    cfg = bot.config.load()
    auto_messages = cfg.get("auto_messages", {})
    auto_signatures = cfg.get("auto_signatures", {})
    if not isinstance(auto_messages, dict):
        return
    if not isinstance(auto_signatures, dict):
        auto_signatures = {}

    endpoint_auto = endpoint_auto_messages(auto_messages, ep_name)
    endpoint_sign = endpoint_auto_signatures(auto_signatures, ep_name)

    content = endpoint_message_content(ep_name, payload)
    embed = build_image_embed(ep_name, payload, for_auto=True)
    new_signature = payload_signature(ep_name, payload)
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:  # noqa: BLE001
            return

    message_id = endpoint_auto.get(str(channel_id))
    if message_id:
        try:
            msg = await channel.fetch_message(int(message_id))
            if endpoint_sign.get(str(channel_id)) == new_signature:
                return
            await msg.edit(content=content, embed=embed)
            endpoint_sign[str(channel_id)] = new_signature
            auto_messages[ep_name] = endpoint_auto
            auto_signatures[ep_name] = endpoint_sign
            bot.config.update({"auto_messages": auto_messages, "auto_signatures": auto_signatures})
            return
        except Exception:  # noqa: BLE001
            pass

    try:
        msg = await channel.send(content=content, embed=embed)
        endpoint_auto[str(channel_id)] = msg.id
        endpoint_sign[str(channel_id)] = new_signature
    except Exception:  # noqa: BLE001
        return

    auto_messages[ep_name] = endpoint_auto
    auto_signatures[ep_name] = endpoint_sign
    bot.config.update({"auto_messages": auto_messages, "auto_signatures": auto_signatures})


async def upsert_news_messages(payload: Any, channel_id: int) -> None:
    cfg = bot.config.load()
    auto_news_messages = cfg.get("auto_news_messages", {})
    auto_news_signatures = cfg.get("auto_news_signatures", {})
    if not isinstance(auto_news_messages, dict):
        auto_news_messages = {}
    if not isinstance(auto_news_signatures, dict):
        auto_news_signatures = {}

    channel_key = str(channel_id)
    channel_messages = auto_news_messages.get(channel_key, {})
    channel_signatures = auto_news_signatures.get(channel_key, {})
    if not isinstance(channel_messages, dict):
        channel_messages = {}
    if not isinstance(channel_signatures, dict):
        channel_signatures = {}

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:  # noqa: BLE001
            return

    raw_items = extract_items(payload) or []
    items = [it for it in raw_items if isinstance(it, dict)]
    active_keys: List[str] = []

    for idx, item in enumerate(items, start=1):
        key = news_item_key(item, idx)
        active_keys.append(key)
        content = news_message_content(item, idx, for_auto=True)
        embed = build_image_embed("news", item, for_auto=True)
        signature = news_message_signature(item, idx, for_auto=True)

        message_id = channel_messages.get(key)
        if message_id:
            try:
                msg = await channel.fetch_message(int(message_id))
                if channel_signatures.get(key) != signature:
                    await msg.edit(content=content, embed=embed)
                    channel_signatures[key] = signature
                continue
            except Exception:  # noqa: BLE001
                pass

        try:
            msg = await channel.send(content=content, embed=embed)
            channel_messages[key] = msg.id
            channel_signatures[key] = signature
        except Exception:  # noqa: BLE001
            continue

    stale_keys = [k for k in list(channel_messages.keys()) if k not in active_keys]
    for stale_key in stale_keys:
        stale_msg_id = channel_messages.get(stale_key)
        if stale_msg_id:
            try:
                msg = await channel.fetch_message(int(stale_msg_id))
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
        channel_messages.pop(stale_key, None)
        channel_signatures.pop(stale_key, None)

    auto_news_messages[channel_key] = channel_messages
    auto_news_signatures[channel_key] = channel_signatures
    bot.config.update({"auto_news_messages": auto_news_messages, "auto_news_signatures": auto_news_signatures})


async def upsert_association_messages(payload: Any, channel_id: int) -> None:
    cfg = bot.config.load()
    auto_association_messages = cfg.get("auto_association_messages", {})
    auto_association_signatures = cfg.get("auto_association_signatures", {})
    if not isinstance(auto_association_messages, dict):
        auto_association_messages = {}
    if not isinstance(auto_association_signatures, dict):
        auto_association_signatures = {}

    channel_key = str(channel_id)
    channel_messages = auto_association_messages.get(channel_key, {})
    channel_signatures = auto_association_signatures.get(channel_key, {})
    if not isinstance(channel_messages, dict):
        channel_messages = {}
    if not isinstance(channel_signatures, dict):
        channel_signatures = {}

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:  # noqa: BLE001
            return

    sections = association_sections(payload)
    active_keys: List[str] = []

    for section_name, section_value in sections.items():
        key = section_name
        active_keys.append(key)
        content = association_section_content(section_name, section_value, for_auto=True)
        embed = build_image_embed("association", section_value, for_auto=True)
        signature = association_section_signature(section_name, section_value, for_auto=True)

        message_id = channel_messages.get(key)
        if message_id:
            try:
                msg = await channel.fetch_message(int(message_id))
                if channel_signatures.get(key) != signature:
                    await msg.edit(content=content, embed=embed)
                    channel_signatures[key] = signature
                continue
            except Exception:  # noqa: BLE001
                pass

        try:
            msg = await channel.send(content=content, embed=embed)
            channel_messages[key] = msg.id
            channel_signatures[key] = signature
        except Exception:  # noqa: BLE001
            continue

    stale_keys = [k for k in list(channel_messages.keys()) if k not in active_keys]
    for stale_key in stale_keys:
        stale_msg_id = channel_messages.get(stale_key)
        if stale_msg_id:
            try:
                msg = await channel.fetch_message(int(stale_msg_id))
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
        channel_messages.pop(stale_key, None)
        channel_signatures.pop(stale_key, None)

    auto_association_messages[channel_key] = channel_messages
    auto_association_signatures[channel_key] = channel_signatures
    bot.config.update(
        {
            "auto_association_messages": auto_association_messages,
            "auto_association_signatures": auto_association_signatures,
        }
    )


async def auto_refresh_endpoint_channel(ep_name: str, channel_id: int) -> int:
    try:
        cfg = build_request_config()
        path = endpoint_path(ep_name)
        print(f"[auto-refresh] Request {ep_name} -> channel {channel_id}")
        payload = await bot.api_client.get_json(cfg["base_url"], path, cfg["api_key"], {})
        if ep_name == "news":
            await upsert_news_messages(payload, channel_id)
        elif ep_name == "association":
            await upsert_association_messages(payload, channel_id)
        else:
            await upsert_endpoint_message(ep_name, payload, channel_id)
        print(f"[auto-refresh] Done {ep_name} -> channel {channel_id}")
        return 60
    except ApiRateLimitError as exc:
        print(f"[auto-refresh] Cooldown local {ep_name}: {exc.remaining_seconds}s")
        return max(1, exc.remaining_seconds)
    except ValueError as exc:
        print(f"[auto-refresh] Config error on {ep_name}: {exc}")
        return 60
    except AlpinnApiError as exc:
        print(f"[auto-refresh] API error on {ep_name}: {exc}")
        if exc.retry_after:
            return max(1, exc.retry_after + 1)
        return 65
    except Exception:  # noqa: BLE001
        print(f"[auto-refresh] Unexpected error on {ep_name}")
        return 60


def build_refresh_jobs() -> List[tuple[str, int]]:
    cfg = bot.config.load()
    enabled = cfg.get("auto_enabled_endpoints", [])
    channels = cfg.get("channels", {})
    if not isinstance(enabled, list):
        enabled = []
    if not isinstance(channels, dict):
        channels = {}

    valid_enabled = [ep for ep in enabled if ep in ENDPOINT_NAMES]
    jobs: List[tuple[str, int]] = []
    for ep_name in valid_enabled:
        for channel_id in endpoint_channel_ids(channels, ep_name):
            jobs.append((ep_name, channel_id))
    return jobs


async def auto_refresh_worker() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        jobs = build_refresh_jobs()
        if not jobs:
            await asyncio.sleep(10)
            continue

        for ep_name, channel_id in jobs:
            if bot.is_closed():
                return
            delay = await auto_refresh_endpoint_channel(ep_name, channel_id)
            await asyncio.sleep(max(1, delay))


async def call_and_send(ctx: commands.Context, ep_name: str, params: Dict[str, str]) -> None:
    try:
        cfg = build_request_config()
        path = endpoint_path(ep_name)
        configured_channel_ids = endpoint_channel_ids(cfg["channels"], ep_name)
        if configured_channel_ids and ctx.channel.id not in configured_channel_ids:
            mentions = " ".join([f"<#{cid}>" for cid in configured_channel_ids])
            await ctx.send(f"Cet endpoint est reserve a: {mentions}")
            return
        payload = await bot.api_client.get_json(cfg["base_url"], path, cfg["api_key"], params)
    except ValueError as exc:
        await ctx.send(f"Erreur config: {exc}")
        return
    except ApiRateLimitError as exc:
        await ctx.send(f"Cooldown global API: attends encore {exc.remaining_seconds}s.")
        return
    except AlpinnApiError as exc:
        if exc.retry_after:
            await ctx.send(f"Erreur API: {exc}. Reessaie dans {exc.retry_after}s.")
            return
        await ctx.send(f"Erreur API: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await ctx.send(f"Erreur inattendue: {exc}")
        return

    if not has_displayable_data(payload):
        await ctx.send(no_data_message(ep_name))
        return

    if ep_name == "news":
        raw_items = extract_items(payload) or []
        items = [it for it in raw_items if isinstance(it, dict)]
        if not items:
            await ctx.send(no_data_message(ep_name))
            return
        max_items = min(len(items), 10)
        for idx, item in enumerate(items[:max_items], start=1):
            await ctx.send(
                content=news_message_content(item, idx, for_auto=False),
                embed=build_image_embed("news", item, for_auto=False),
            )
        if len(items) > max_items:
            await ctx.send(f"Affichage limite a {max_items} news. Utilise `limit=` pour filtrer.")
        return

    if ep_name == "association":
        sections = association_sections(payload)
        if not sections:
            await ctx.send(no_data_message(ep_name))
            return
        sent = 0
        for section_name, section_value in sections.items():
            if sent >= 10:
                await ctx.send("Affichage limite a 10 sections.")
                break
            await ctx.send(
                content=association_section_content(section_name, section_value, for_auto=False),
                embed=build_image_embed("association", section_value, for_auto=False),
            )
            sent += 1
        return

    await ctx.send(
        content=styled_payload_content(ep_name, payload, for_auto=False),
        embed=build_image_embed(ep_name, payload, for_auto=False),
    )


@bot.event
async def on_ready() -> None:
    print(f"Bot pret: {bot.user}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Acces refuse: ce bot est reserve aux administrateurs du serveur.")
        return
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"Erreur: {error}")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    msg = (
        "**Commandes disponibles**\n"
        "`!set_base_url <url>`: Definit l'URL de base API.\n"
        "`!set_channel <endpoint> <#salon>`: Ajoute un salon pour un endpoint (admin).\n"
        "`!unset_channel <endpoint> [#salon]`: Retire un salon ou tous les salons d'un endpoint (admin).\n"
        "`!enable_endpoint <endpoint>`: Active l'auto-affichage endpoint (admin).\n"
        "`!disable_endpoint <endpoint>`: Desactive l'auto-affichage endpoint (admin).\n"
        "`!enable_all_endpoints`: Active l'auto-affichage de tous les endpoints configures (admin).\n"
        "`!refresh_all_now`: Force une passe de mise a jour sur tous les endpoints actifs (admin).\n"
        "`!auto_status`: Etat du mode auto (endpoints actifs).\n"
        "`!enable_news`: Active l'auto-affichage de news (admin).\n"
        "`!disable_news`: Desactive l'auto-affichage de news (admin).\n"
        "`!clear <#salon|all>`: Supprime les messages auto du salon ou de tous les salons (admin).\n"
        "`!reboot`: Redemarre le bot (admin).\n"
        "`!autostart <on|off|status>`: Active/desactive/affiche le demarrage auto Linux (admin).\n"
        "`!background_mode <on|off|status>`: Active/desactive/affiche le lancement en arriere-plan du script (admin).\n"
        "`!set_update_delay <minutes>`: Definit le delai (minutes) entre verifications de mise a jour du bot (admin).\n"
        "`!show_logs [lignes]`: Affiche les dernieres lignes du log update sur Discord (admin).\n"
        "`!backup_config`: Cree une sauvegarde horodatee de bot_config.json (admin).\n"
        "`!restore_config [fichier.json]`: Restaure une backup (ou la plus recente) avec backup temporaire auto avant restore (admin).\n"
        "`!show_channels`: Affiche les associations endpoint/salon.\n"
        "`!show_config`: Affiche la config actuelle (sans afficher la cle).\n"
        "`!endpoints`: Liste les endpoints disponibles.\n"
        "`!list_endpoints`: Alias de !endpoints.\n"
        "`!fetch <endpoint> [k=v ...]`: Appel generique.\n"
        "`!association`\n"
        "`!news [k=v ...]`\n"
        "`!statuts [k=v ...]`\n"
        "`!staff [k=v ...]`\n"
        "`!activities [k=v ...]`\n"
        "`!events [k=v ...]`\n"
        "Cooldown global API: 1 requete / 60 secondes."
    )
    await ctx.send(msg)


@bot.command(name="set_base_url")
@commands.has_permissions(administrator=True)
async def set_base_url(ctx: commands.Context, url: str) -> None:
    try:
        final_url = normalize_base_url(url)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    bot.config.update({"base_url": final_url})
    await ctx.send(f"Base URL enregistree: `{final_url}`")


@bot.command(name="set_channel")
@commands.has_permissions(administrator=True)
async def set_channel(ctx: commands.Context, endpoint: str, channel: discord.TextChannel) -> None:
    endpoint_name = endpoint.lower()
    try:
        endpoint_path(endpoint_name)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    cfg = bot.config.load()
    channels = cfg.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}
    ids = endpoint_channel_ids(channels, endpoint_name)
    if channel.id not in ids:
        ids.append(channel.id)
    channels[endpoint_name] = ids
    save_reconciled_config({"channels": channels})
    await ctx.send(f"Salon {channel.mention} associe a `{endpoint_name}`.")


@bot.command(name="unset_channel")
@commands.has_permissions(administrator=True)
async def unset_channel(ctx: commands.Context, endpoint: str, channel: Optional[discord.TextChannel] = None) -> None:
    endpoint_name = endpoint.lower()
    try:
        endpoint_path(endpoint_name)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    cfg = bot.config.load()
    channels = normalize_channels(cfg.get("channels", {}))
    auto_messages = cfg.get("auto_messages", {})
    auto_signatures = cfg.get("auto_signatures", {})
    auto_news_messages = cfg.get("auto_news_messages", {})
    auto_news_signatures = cfg.get("auto_news_signatures", {})
    auto_association_messages = cfg.get("auto_association_messages", {})
    auto_association_signatures = cfg.get("auto_association_signatures", {})
    if not isinstance(auto_messages, dict):
        auto_messages = {}
    if not isinstance(auto_signatures, dict):
        auto_signatures = {}
    if not isinstance(auto_news_messages, dict):
        auto_news_messages = {}
    if not isinstance(auto_news_signatures, dict):
        auto_news_signatures = {}
    if not isinstance(auto_association_messages, dict):
        auto_association_messages = {}
    if not isinstance(auto_association_signatures, dict):
        auto_association_signatures = {}
    existing_ids = endpoint_channel_ids(channels, endpoint_name)
    if not existing_ids:
        await ctx.send(f"Aucune association configuree pour `{endpoint_name}`.")
        return

    endpoint_auto = auto_messages.get(endpoint_name, {})
    endpoint_sign = auto_signatures.get(endpoint_name, {})
    if not isinstance(endpoint_auto, dict):
        endpoint_auto = {}
    if not isinstance(endpoint_sign, dict):
        endpoint_sign = {}

    if channel is None:
        channels.pop(endpoint_name, None)
        auto_messages.pop(endpoint_name, None)
        auto_signatures.pop(endpoint_name, None)
        if endpoint_name == "news":
            auto_news_messages = {}
            auto_news_signatures = {}
        if endpoint_name == "association":
            auto_association_messages = {}
            auto_association_signatures = {}
        save_reconciled_config(
            {
                "channels": channels,
                "auto_messages": auto_messages,
                "auto_signatures": auto_signatures,
                "auto_news_messages": auto_news_messages,
                "auto_news_signatures": auto_news_signatures,
                "auto_association_messages": auto_association_messages,
                "auto_association_signatures": auto_association_signatures,
            }
        )
        await ctx.send(f"Tous les salons ont ete retires pour `{endpoint_name}`.")
        return

    if channel.id not in existing_ids:
        await ctx.send(f"{channel.mention} n'est pas associe a `{endpoint_name}`.")
        return

    remaining = [cid for cid in existing_ids if cid != channel.id]
    if remaining:
        channels[endpoint_name] = remaining
    else:
        channels.pop(endpoint_name, None)

    endpoint_auto.pop(str(channel.id), None)
    endpoint_sign.pop(str(channel.id), None)
    if endpoint_auto:
        auto_messages[endpoint_name] = endpoint_auto
    else:
        auto_messages.pop(endpoint_name, None)
    if endpoint_sign:
        auto_signatures[endpoint_name] = endpoint_sign
    else:
        auto_signatures.pop(endpoint_name, None)
    if endpoint_name == "news":
        auto_news_messages.pop(str(channel.id), None)
        auto_news_signatures.pop(str(channel.id), None)
    if endpoint_name == "association":
        auto_association_messages.pop(str(channel.id), None)
        auto_association_signatures.pop(str(channel.id), None)
    save_reconciled_config(
        {
            "channels": channels,
            "auto_messages": auto_messages,
            "auto_signatures": auto_signatures,
            "auto_news_messages": auto_news_messages,
            "auto_news_signatures": auto_news_signatures,
            "auto_association_messages": auto_association_messages,
            "auto_association_signatures": auto_association_signatures,
        }
    )
    await ctx.send(f"Salon {channel.mention} retire de `{endpoint_name}`.")


@bot.command(name="show_channels")
async def show_channels(ctx: commands.Context) -> None:
    cfg = bot.config.load()
    channels = cfg.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}

    lines = []
    for name in ENDPOINT_NAMES:
        ids = endpoint_channel_ids(channels, name)
        value = " ".join([f"<#{cid}>" for cid in ids]) if ids else "(non defini)"
        lines.append(f"- `{name}` -> {value}")
    await ctx.send("Associations endpoint/salon:\n" + "\n".join(lines))


def set_endpoint_auto_state(endpoint_name: str, enabled_state: bool) -> str:
    cfg = save_reconciled_config()
    enabled = cfg.get("auto_enabled_endpoints", [])
    if not isinstance(enabled, list):
        enabled = []
    enabled = [ep for ep in enabled if ep in ENDPOINT_NAMES]

    if enabled_state and endpoint_name not in enabled:
        enabled.append(endpoint_name)
    if not enabled_state and endpoint_name in enabled:
        enabled.remove(endpoint_name)

    save_reconciled_config({"auto_enabled_endpoints": enabled})
    return ", ".join(enabled) if enabled else "(aucun)"


@bot.command(name="enable_endpoint")
@commands.has_permissions(administrator=True)
async def enable_endpoint(ctx: commands.Context, endpoint: str) -> None:
    if not await ensure_api_key_or_warn(ctx):
        return
    endpoint_name = endpoint.lower()
    try:
        endpoint_path(endpoint_name)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    channels = bot.config.load().get("channels", {})
    if not isinstance(channels, dict) or not endpoint_channel_ids(channels, endpoint_name):
        await ctx.send(f"Associe d'abord un salon: `!set_channel {endpoint_name} #salon`.")
        return
    state = set_endpoint_auto_state(endpoint_name, True)
    await ctx.send(f"Auto-affichage actif pour `{endpoint_name}`. Endpoints actifs: {state}")


@bot.command(name="disable_endpoint")
@commands.has_permissions(administrator=True)
async def disable_endpoint(ctx: commands.Context, endpoint: str) -> None:
    endpoint_name = endpoint.lower()
    try:
        endpoint_path(endpoint_name)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    state = set_endpoint_auto_state(endpoint_name, False)
    await ctx.send(f"Auto-affichage desactive pour `{endpoint_name}`. Endpoints actifs: {state}")


@bot.command(name="enable_news")
@commands.has_permissions(administrator=True)
async def enable_news(ctx: commands.Context) -> None:
    await enable_endpoint(ctx, "news")


@bot.command(name="enable_all_endpoints")
@commands.has_permissions(administrator=True)
async def enable_all_endpoints(ctx: commands.Context) -> None:
    if not await ensure_api_key_or_warn(ctx):
        return
    cfg = bot.config.load()
    channels = cfg.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}

    enabled_now = []
    missing_channels = []
    for endpoint_name in ENDPOINT_NAMES:
        if endpoint_channel_ids(channels, endpoint_name):
            set_endpoint_auto_state(endpoint_name, True)
            enabled_now.append(endpoint_name)
        else:
            missing_channels.append(endpoint_name)

    if not enabled_now:
        await ctx.send("Aucun endpoint active: configure d'abord des salons avec `!set_channel <endpoint> #salon`.")
        return

    msg = "Auto-affichage active pour: " + ", ".join(enabled_now) + "."
    if missing_channels:
        msg += " Salons manquants: " + ", ".join(missing_channels) + "."
    await ctx.send(msg)


@bot.command(name="disable_news")
@commands.has_permissions(administrator=True)
async def disable_news(ctx: commands.Context) -> None:
    await disable_endpoint(ctx, "news")


@bot.command(name="auto_status")
async def auto_status(ctx: commands.Context) -> None:
    cfg = bot.config.load()
    enabled = cfg.get("auto_enabled_endpoints", [])
    if not isinstance(enabled, list):
        enabled = []
    enabled = [ep for ep in enabled if ep in ENDPOINT_NAMES]
    jobs = build_refresh_jobs()
    lines = [f"- `{ep}`" for ep in enabled] if enabled else ["- (aucun)"]
    await ctx.send(
        f"Auto-refresh: 1 requete toutes les 60s (jobs actifs: {len(jobs)}).\n"
        "Endpoints actifs:\n"
        + "\n".join(lines)
    )


@bot.command(name="refresh_all_now")
@commands.has_permissions(administrator=True)
async def refresh_all_now(ctx: commands.Context) -> None:
    if not await ensure_api_key_or_warn(ctx):
        return
    jobs = build_refresh_jobs()
    if not jobs:
        await ctx.send(
            "Aucun job actif. Active d'abord des endpoints et associe des salons "
            "avec `!set_channel` + `!enable_endpoint`."
        )
        return

    await ctx.send(
        f"Mise a jour forcee lancee pour {len(jobs)} job(s) endpoint/salon. "
        "Le bot respecte 1 requete/60s, donc cela peut prendre plusieurs minutes."
    )
    for ep_name, channel_id in jobs:
        delay = await auto_refresh_endpoint_channel(ep_name, channel_id)
        await asyncio.sleep(max(1, delay))
    await ctx.send("Mise a jour forcee terminee.")


def parse_channel_target(ctx: commands.Context, token: str) -> Optional[int]:
    mention_match = re.fullmatch(r"<#(\d+)>", token.strip())
    if mention_match:
        return int(mention_match.group(1))
    if token.isdigit():
        return int(token)
    if ctx.guild:
        by_name = discord.utils.get(ctx.guild.text_channels, name=token.lstrip("#"))
        if by_name:
            return by_name.id
    return None


async def remove_tracked_message(channel_id: int, message_id: int) -> bool:
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:  # noqa: BLE001
            return False
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()
        return True
    except Exception:  # noqa: BLE001
        return False


@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def clear(ctx: commands.Context, target: str) -> None:
    cfg = bot.config.load()
    auto_messages = cfg.get("auto_messages", {})
    auto_signatures = cfg.get("auto_signatures", {})
    auto_news_messages = cfg.get("auto_news_messages", {})
    auto_news_signatures = cfg.get("auto_news_signatures", {})
    auto_association_messages = cfg.get("auto_association_messages", {})
    auto_association_signatures = cfg.get("auto_association_signatures", {})
    if not isinstance(auto_messages, dict):
        auto_messages = {}
    if not isinstance(auto_signatures, dict):
        auto_signatures = {}
    if not isinstance(auto_news_messages, dict):
        auto_news_messages = {}
    if not isinstance(auto_news_signatures, dict):
        auto_news_signatures = {}
    if not isinstance(auto_association_messages, dict):
        auto_association_messages = {}
    if not isinstance(auto_association_signatures, dict):
        auto_association_signatures = {}

    deleted = 0
    target_lower = target.lower()

    if target_lower == "all":
        for ep_name in ENDPOINT_NAMES:
            endpoint_auto = endpoint_auto_messages(auto_messages, ep_name)
            for channel_id_str, message_id in endpoint_auto.items():
                if await remove_tracked_message(int(channel_id_str), int(message_id)):
                    deleted += 1
        for channel_key, by_news in list(auto_news_messages.items()):
            if isinstance(by_news, dict):
                for _, message_id in by_news.items():
                    if await remove_tracked_message(int(channel_key), int(message_id)):
                        deleted += 1
        for channel_key, by_assoc in list(auto_association_messages.items()):
            if isinstance(by_assoc, dict):
                for _, message_id in by_assoc.items():
                    if await remove_tracked_message(int(channel_key), int(message_id)):
                        deleted += 1

        save_reconciled_config(
            {
                "auto_messages": {},
                "auto_signatures": {},
                "auto_news_messages": {},
                "auto_news_signatures": {},
                "auto_association_messages": {},
                "auto_association_signatures": {},
            }
        )
        await ctx.send(
            f"Clear termine: {deleted} message(s) supprime(s). "
            "Seules les donnees de suivi des messages ont ete nettoyees."
        )
        return

    channel_id = parse_channel_target(ctx, target)
    if channel_id is None:
        await ctx.send("Salon invalide. Utilise `!clear #salon` ou `!clear all`.")
        return

    channel_key = str(channel_id)
    for ep_name in ENDPOINT_NAMES:
        endpoint_auto = endpoint_auto_messages(auto_messages, ep_name)
        endpoint_sign = endpoint_auto_signatures(auto_signatures, ep_name)
        msg_id = endpoint_auto.get(channel_key)
        if msg_id and await remove_tracked_message(channel_id, int(msg_id)):
            deleted += 1
        endpoint_auto.pop(channel_key, None)
        endpoint_sign.pop(channel_key, None)
        if endpoint_auto:
            auto_messages[ep_name] = endpoint_auto
        else:
            auto_messages.pop(ep_name, None)
        if endpoint_sign:
            auto_signatures[ep_name] = endpoint_sign
        else:
            auto_signatures.pop(ep_name, None)

    by_news = auto_news_messages.get(channel_key, {})
    if isinstance(by_news, dict):
        for _, message_id in by_news.items():
            if await remove_tracked_message(channel_id, int(message_id)):
                deleted += 1

    by_assoc = auto_association_messages.get(channel_key, {})
    if isinstance(by_assoc, dict):
        for _, message_id in by_assoc.items():
            if await remove_tracked_message(channel_id, int(message_id)):
                deleted += 1

    auto_news_messages.pop(channel_key, None)
    auto_news_signatures.pop(channel_key, None)
    auto_association_messages.pop(channel_key, None)
    auto_association_signatures.pop(channel_key, None)

    save_reconciled_config(
        {
            "auto_messages": auto_messages,
            "auto_signatures": auto_signatures,
            "auto_news_messages": auto_news_messages,
            "auto_news_signatures": auto_news_signatures,
            "auto_association_messages": auto_association_messages,
            "auto_association_signatures": auto_association_signatures,
        }
    )
    await ctx.send(
        f"Clear termine pour <#{channel_id}>: {deleted} message(s) supprime(s). "
        "Seules les donnees liees a ces messages ont ete nettoyees."
    )


@bot.command(name="reboot")
@commands.has_permissions(administrator=True)
async def reboot(ctx: commands.Context) -> None:
    global REBOOT_REQUESTED
    REBOOT_REQUESTED = True
    await ctx.send("Redemarrage demande: mise a jour puis redemarrage en cours...")
    await bot.close()


def get_autostart_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), AUTOSTART_SCRIPT_RELATIVE_PATH)


def get_update_delay_file_path() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    return os.path.join(base_dir, UPDATE_DELAY_FILE_NAME)


def read_update_delay_minutes_from_file() -> Optional[int]:
    path = get_update_delay_file_path()
    if not os.path.isfile(path):
        return None
    try:
        raw = open(path, "r", encoding="utf-8").read().strip()
        value = int(raw)
        if value >= 1:
            return value
    except Exception:  # noqa: BLE001
        return None
    return None


def get_background_mode_file_path() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    return os.path.join(base_dir, BACKGROUND_MODE_FILE_NAME)


def read_background_mode() -> str:
    path = get_background_mode_file_path()
    if not os.path.isfile(path):
        return "on"
    try:
        raw = open(path, "r", encoding="utf-8").read().strip().lower()
    except Exception:  # noqa: BLE001
        return "on"
    if raw in {"on", "off"}:
        return raw
    return "on"


@bot.command(name="autostart")
@commands.has_permissions(administrator=True)
async def autostart(ctx: commands.Context, mode: str = "status") -> None:
    action = mode.strip().lower()
    if action not in {"on", "off", "status"}:
        await ctx.send("Usage: `!autostart <on|off|status>`")
        return

    script_path = get_autostart_script_path()
    if not os.path.isfile(script_path):
        await ctx.send(f"Script introuvable: `{script_path}`")
        return

    try:
        result = subprocess.run(
            ["bash", script_path, action],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        await ctx.send(f"Erreur execution autostart: {exc}")
        return

    output = (result.stdout or "").strip()
    error_output = (result.stderr or "").strip()

    if result.returncode != 0:
        details = error_output or output or f"code={result.returncode}"
        await ctx.send(f"Echec autostart `{action}`: {details}")
        return

    detected_state: Optional[bool] = None
    if "enabled" in output.lower():
        detected_state = True
    elif "disabled" in output.lower():
        detected_state = False

    if action == "on":
        detected_state = True
    elif action == "off":
        detected_state = False

    if detected_state is not None:
        save_reconciled_config({"boot_autostart_enabled": detected_state})

    final_state = "inconnu"
    if detected_state is True:
        final_state = "actif"
    elif detected_state is False:
        final_state = "inactif"

    message = output if output else f"Commande autostart `{action}` executee."
    await ctx.send(f"{message}\nEtat demarrage auto: `{final_state}`")


@bot.command(name="background_mode")
@commands.has_permissions(administrator=True)
async def background_mode(ctx: commands.Context, mode: str = "status") -> None:
    action = mode.strip().lower()
    if action not in {"on", "off", "status"}:
        await ctx.send("Usage: `!background_mode <on|off|status>`")
        return

    file_path = get_background_mode_file_path()
    if action in {"on", "off"}:
        try:
            with open(file_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(f"{action}\n")
        except Exception as exc:
            await ctx.send(f"Echec sauvegarde mode arriere-plan: {exc}")
            return
        save_reconciled_config({"background_mode_enabled": action == "on"})

    current = read_background_mode()
    state = "actif" if current == "on" else "inactif"
    await ctx.send(f"Mode arriere-plan: `{state}`")


@bot.command(name="set_update_delay")
@commands.has_permissions(administrator=True)
async def set_update_delay(ctx: commands.Context, minutes: int) -> None:
    if minutes < 1 or minutes > 1440:
        await ctx.send("Valeur invalide. Utilise un nombre de minutes entre 1 et 1440.")
        return

    delay_path = get_update_delay_file_path()
    try:
        with open(delay_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(f"{minutes}\n")
    except Exception as exc:
        await ctx.send(f"Echec sauvegarde delai update: {exc}")
        return

    save_reconciled_config({"update_check_delay_minutes": minutes})
    await ctx.send(f"Delai de verification des updates defini a `{minutes}` minute(s).")


@bot.command(name="show_logs")
@commands.has_permissions(administrator=True)
async def show_logs(ctx: commands.Context, lines: int = 40) -> None:
    safe_lines = max(5, min(lines, 200))
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.log")
    if not os.path.isfile(log_path):
        await ctx.send(f"Log introuvable: `{log_path}`")
        return

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as file_obj:
            content_lines = file_obj.readlines()
    except Exception as exc:
        await ctx.send(f"Echec lecture log: {exc}")
        return

    tail_lines = content_lines[-safe_lines:]
    if not tail_lines:
        await ctx.send("Le log est vide.")
        return

    payload = "".join(tail_lines).strip()
    if len(payload) > 1800:
        payload = payload[-1800:]
        payload = "(tronque)\n" + payload

    await ctx.send(f"```text\n{payload}\n```")


@bot.command(name="backup_config")
@commands.has_permissions(administrator=True)
async def backup_config(ctx: commands.Context) -> None:
    try:
        backup_path = create_config_backup(prefix="bot_config_backup")
    except Exception as exc:
        await ctx.send(f"Echec backup config: {exc}")
        return
    await ctx.send(f"Backup creee: `{backup_path.name}`")


@bot.command(name="restore_config")
@commands.has_permissions(administrator=True)
async def restore_config(ctx: commands.Context, backup_filename: Optional[str] = None) -> None:
    backups = list_config_backups()
    if not backups:
        await ctx.send("Aucune backup disponible. Utilise `!backup_config` d'abord.")
        return

    selected: Optional[Path] = None
    if backup_filename:
        safe_name = os.path.basename(backup_filename.strip())
        for item in backups:
            if item.name == safe_name:
                selected = item
                break
        if selected is None:
            await ctx.send(f"Backup introuvable: `{safe_name}`")
            return
    else:
        selected = backups[0]

    try:
        rollback_path = create_config_backup(prefix="bot_config_pre_restore")
        shutil.copy2(selected, config_file_path())
        save_reconciled_config()
    except Exception as exc:
        await ctx.send(f"Echec restoration config: {exc}")
        return

    await ctx.send(
        f"Config restauree depuis `{selected.name}`.\n"
        f"Backup temporaire rollback creee: `{rollback_path.name}`"
    )


@bot.command(name="show_config")
async def show_config(ctx: commands.Context) -> None:
    cfg = bot.config.load()
    api_key = os.getenv("ALPINN_API_KEY", "").strip()
    base_url = cfg.get("base_url", "(vide)")
    key_state = "definie" if api_key else "absente"
    autostart_enabled = bool(cfg.get("boot_autostart_enabled", False))
    autostart_state = "actif" if autostart_enabled else "inactif"
    background_state = "actif" if read_background_mode() == "on" else "inactif"
    delay_minutes = read_update_delay_minutes_from_file()
    if delay_minutes is None:
        raw_delay = cfg.get("update_check_delay_minutes", 1)
        try:
            delay_minutes = max(1, int(raw_delay))
        except (TypeError, ValueError):
            delay_minutes = 1
    await ctx.send(
        f"base_url=`{base_url}`\n"
        f"api_key=`{key_state}` (lecture locale, non modifiable via commande)\n"
        f"autostart_linux=`{autostart_state}`\n"
        f"background_mode=`{background_state}`\n"
        f"update_check_delay_minutes=`{delay_minutes}`"
    )


@bot.command(name="endpoints")
async def endpoints(ctx: commands.Context) -> None:
    base_url = bot.config.load().get("base_url") or "http://localhost/alpinn.ch_dynamic/public"
    lines = [
        f"- `association` -> {base_url}/api/v1/association.php",
        f"- `news` -> {base_url}/api/v1/news.php",
        f"- `statuts` -> {base_url}/api/v1/statuts.php",
        f"- `staff` -> {base_url}/api/v1/staff.php",
        f"- `activities` -> {base_url}/api/v1/activities.php",
        f"- `events` -> {base_url}/api/v1/events.php",
    ]
    await ctx.send("Catalogue endpoints:\n" + "\n".join(lines))


@bot.command(name="list_endpoints")
async def list_endpoints(ctx: commands.Context) -> None:
    await endpoints(ctx)


@bot.command(name="fetch")
async def fetch(ctx: commands.Context, endpoint: str, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, endpoint.lower(), params)


@bot.command(name="association")
async def association(ctx: commands.Context) -> None:
    await call_and_send(ctx, "association", {})


@bot.command(name="news")
async def news(ctx: commands.Context, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, "news", params)


@bot.command(name="statuts")
async def statuts(ctx: commands.Context, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, "statuts", params)


@bot.command(name="staff")
async def staff(ctx: commands.Context, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, "staff", params)


@bot.command(name="activities")
async def activities(ctx: commands.Context, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, "activities", params)


@bot.command(name="events")
async def events(ctx: commands.Context, *, query: str = "") -> None:
    try:
        params = extract_query_pairs(query)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await call_and_send(ctx, "events", params)


@set_channel.error
@unset_channel.error
@enable_endpoint.error
@disable_endpoint.error
@enable_news.error
@enable_all_endpoints.error
@refresh_all_now.error
@disable_news.error
@clear.error
@reboot.error
@autostart.error
@background_mode.error
@set_update_delay.error
@show_logs.error
@backup_config.error
@restore_config.error
@set_base_url.error
async def admin_only_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Commande reservee aux administrateurs.")
        return
    await ctx.send(f"Erreur: {error}")


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Variable DISCORD_BOT_TOKEN manquante dans .env")
    bot.run(token)
    if REBOOT_REQUESTED:
        raise SystemExit(42)


if __name__ == "__main__":
    main()
