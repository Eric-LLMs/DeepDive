"""Contextual image scraping (Google Images → Bing fallback).

Downloads the top image for each of several query variants (term alone, term+definition,
term+context), caches them under the image cache dir, and returns URL paths.
"""
import asyncio
import hashlib
import re
import time
import urllib.parse
import urllib.request

from core.config import settings

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class ImageScraper:
    def __init__(self) -> None:
        self.output_dir = settings.image_cache_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def fetch(
        self, word: str, definition: str = "", context: str = "", regenerate: bool = False
    ) -> list[str]:
        return await asyncio.to_thread(self._fetch_sync, word, definition, context, regenerate)

    def _fetch_sync(self, word: str, definition: str, context: str, regenerate: bool) -> list[str]:
        fetched: set[str] = set()
        selected: list[str] = []

        queries = [word]
        if definition:
            queries.append(f"{word} {_clean(definition)[:30]}")
        else:
            queries.append(f"{word} concept")
        if context:
            queries.append(f"{word} {_clean(context)[:30]}")
        else:
            queries.append(f"{word} illustration")

        for q in queries:
            urls = _get_image_urls(q, count=8, exclude=fetched)
            if urls:
                chosen = urls[0]
                if regenerate and len(urls) > 1:
                    import random

                    chosen = random.choice(urls)
                selected.append(chosen)
                fetched.add(chosen)

        if len(selected) < 3:
            more = _get_image_urls(word, count=10, exclude=fetched)
            if more:
                if regenerate:
                    import random

                    random.shuffle(more)
                selected.extend(more[: 3 - len(selected)])

        saved: list[str] = []
        stamp = f"{_slug(word)}_{int(time.time())}"
        for idx, url in enumerate(selected[:3]):
            ext = "png" if ".png" in url.lower() else ("jpeg" if ".jpeg" in url.lower() else "jpg")
            filename = f"{stamp}_{idx}.{ext}"
            path = self.output_dir / filename
            if _download(url, path):
                saved.append(f"/images/{filename}")
        return saved


def _clean(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text)


def _slug(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def _get_image_urls(query: str, count: int = 8, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    encoded = urllib.parse.quote(query)
    urls: list[str] = []

    # Google Images
    try:
        req = urllib.request.Request(
            f"https://www.google.com/search?tbm=isch&q={encoded}", headers=_HEADERS
        )
        html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", errors="ignore")
        for m in re.findall(r'\["(http[^"]+?\.(?:jpg|jpeg|png))"', html):
            if m not in exclude and m not in urls:
                urls.append(m)
                if len(urls) >= count:
                    return urls
    except Exception:
        pass

    # Bing Images fallback
    if len(urls) < count:
        try:
            req = urllib.request.Request(
                f"https://www.bing.com/images/search?q={encoded}", headers=_HEADERS
            )
            html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", errors="ignore")
            for m in re.findall(r'murl&quot;:&quot;(http[^&]+?)&quot;', html):
                if m not in exclude and m not in urls:
                    urls.append(m)
                    if len(urls) >= count:
                        return urls
        except Exception:
            pass

    return urls


def _download(url: str, save_path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp, open(save_path, "wb") as out:
            out.write(resp.read())
        return True
    except Exception:
        return False
