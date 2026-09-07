"""Fetch Steam artwork and prepare small, local assets for a timer window."""

import colorsys
from io import BytesIO
import re
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
import requests

from . import config
from .net import fetch_json
from .errors import NetworkError

BACKGROUND = "#11151b"
DEFAULT_ACCENT = "#aac9b5"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def steam_id(value) -> int | None:
    text = str(value or "")
    return int(text) if text.isascii() and text.isdigit() and 0 < int(text) < 2**32 else None


def _asset_url(appid: int, relative: str) -> str | None:
    # Steam metadata can contain hashed subdirectories, but never arbitrary URLs.
    if not isinstance(relative, str) or not re.fullmatch(r"[\w./-]+", relative, re.ASCII):
        return None
    if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    return f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/{quote(relative)}"


def _download_image(url: str) -> Image.Image | None:
    try:
        with requests.get(url, timeout=(3, 5), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                return None
            chunks = bytearray()
            for chunk in response.iter_content(65536):
                chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    return None
        with Image.open(BytesIO(chunks)) as image:
            if image.width * image.height > 16_000_000:
                return None
            return image.convert("RGB")
    except (requests.RequestException, OSError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError):
        return None


def _accent(image: Image.Image) -> str:
    palette = image.resize((64, 32)).quantize(colors=8).convert("RGB")
    candidates = palette.getcolors(2048) or []
    best = None
    for count, rgb in candidates:
        hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
        if saturation > 0.15 and value > 0.15:
            score = count * saturation
            if best is None or score > best[0]:
                best = (score, hue, saturation)
    if best is None:
        return DEFAULT_ACCENT
    rgb = colorsys.hsv_to_rgb(best[1], min(0.5, best[2]), 0.9)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def prepare_artwork(name: str, appid=None) -> tuple[dict, dict[str, bytes]]:
    """Return data-only presentation settings and optional PNGs; never write files."""
    theme = {"name": str(name)[:160], "accent": DEFAULT_ACCENT}
    assets = {}
    appid = steam_id(appid)
    if appid is None:
        return theme, assets
    theme["steam_appid"] = appid
    common = {}
    try:
        data = fetch_json(f"{config.STEAMCMD_API_URL}/{appid}", timeout=5)
        common = data.get("data", {}).get(str(appid), {}).get("common", {})
        if not isinstance(common, dict):
            common = {}
    except (NetworkError, AttributeError, TypeError):
        pass

    try:
        hero_path = common["library_assets_full"]["library_hero"]["image"]["english"]
    except (KeyError, TypeError):
        hero_path = "library_hero.jpg"
    hero_url = _asset_url(appid, hero_path)
    hero = _download_image(hero_url) if hero_url else None
    if hero is None:
        hero = _download_image(_asset_url(appid, "header.jpg"))
    if hero is not None:
        theme["accent"] = _accent(hero)
        hero = ImageOps.fit(hero, (820, 320), method=Image.Resampling.LANCZOS)
        # Fade the artwork into the panel; text contrast does not depend on the image.
        overlay = Image.new("RGBA", hero.size)
        draw = ImageDraw.Draw(overlay)
        for y in range(hero.height):
            alpha = round(40 + 215 * (y / (hero.height - 1)) ** 2)
            draw.line((0, y, hero.width, y), fill=(17, 21, 27, alpha))
        assets["hero"] = _png(Image.alpha_composite(hero.convert("RGBA"), overlay).convert("RGB"))

    icon_hash = common.get("icon", "")
    if isinstance(icon_hash, str) and re.fullmatch(r"[a-fA-F0-9]{40}", icon_hash):
        icon = _download_image(f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg")
        if icon is not None:
            assets["icon"] = _png(ImageOps.fit(icon, (64, 64), method=Image.Resampling.LANCZOS))
    return theme, assets
