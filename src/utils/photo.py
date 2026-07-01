import io
import re
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from PIL import Image, ImageOps, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from src.config import (
    PHOTO_PX, PHOTO_JPEG_QUALITY, PHOTO_TIMEOUT, MAX_PHOTO_BYTES,
    MAX_CACHED_PHOTOS, PREFETCH_WORKERS
)

log = logging.getLogger("idcard.photo")

# Global HTTP Session setup for connection pooling
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; IDCardGen/2.7)",
    "Accept": "image/*,*/*;q=0.8",
    "Connection": "keep-alive",
})
_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504],
               allowed_methods=["GET"], raise_on_status=False)
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=_retry)
_HTTP.mount("http://",  _adapter)
_HTTP.mount("https://", _adapter)


class _BoundedPhotoCache:
    def __init__(self, maxsize: int = 600):
        self._cache = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key not in self._cache:
                return False, None
            self._cache.move_to_end(key)
            return True, self._cache[key]

    def set(self, key: str, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __len__(self):
        with self._lock:
            return len(self._cache)


_photo_cache = _BoundedPhotoCache(maxsize=MAX_CACHED_PHOTOS)


def _compress_photo(pil_img) -> bytes:
    if not HAS_PIL:
        return b""
    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass

    rgb = pil_img.convert("RGB")
    src_w, src_h = rgb.size
    src_min = min(src_w, src_h)

    if src_min < 150:
        pre_w = max(1, int(src_w * 2))
        pre_h = max(1, int(src_h * 2))
        rgb = rgb.resize((pre_w, pre_h), Image.Resampling.BICUBIC)
        rgb = rgb.filter(ImageFilter.SMOOTH_MORE)
        src_w, src_h = rgb.size
    elif src_min < 280:
        rgb = rgb.filter(ImageFilter.SMOOTH)

    ratio = min(PHOTO_PX / max(1, src_w), PHOTO_PX / max(1, src_h))
    new_w = max(1, int(round(src_w * ratio)))
    new_h = max(1, int(round(src_h * ratio)))
    resized = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if rgb is not pil_img:
        rgb.close()

    resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=4))

    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                 optimize=True, progressive=False, subsampling=1)
    resized.close()
    return buf.getvalue()


def _clean_photo_url(url: str) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    return url


def fetch_photo_bytes(url: str):
    if not HAS_PIL:
        return None

    cache_key = _clean_photo_url((url or "").strip())
    if not cache_key:
        return None

    found, cached = _photo_cache.get(cache_key)
    if found:
        return cached

    def _do_fetch(verify_ssl=True):
        resp = _HTTP.get(cache_key, timeout=PHOTO_TIMEOUT, stream=True,
                         allow_redirects=True, verify=verify_ssl)
        resp.raise_for_status()
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ct:
            return None
        chunks = []
        total  = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PHOTO_BYTES:
                raise ValueError("photo too large")
            chunks.append(chunk)
        resp.close()
        return b"".join(chunks)

    try:
        raw = _do_fetch(verify_ssl=True)
        if raw is None:
            _photo_cache.set(cache_key, None)
            return None
    except Exception:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            raw = _do_fetch(verify_ssl=False)
            if raw is None:
                _photo_cache.set(cache_key, None)
                return None
        except Exception:
            _photo_cache.set(cache_key, None)
            return None

    try:
        with Image.open(io.BytesIO(raw)) as img:
            compressed = _compress_photo(img)
        _photo_cache.set(cache_key, compressed)
        return compressed
    except Exception:
        _photo_cache.set(cache_key, None)
        return None


def clear_photo_cache():
    _photo_cache.clear()


def prefetch_photos(students: list) -> None:
    urls = list({
        s.get("photo_url","").strip()
        for s in students
        if s.get("photo_url","").strip()
    })
    if not urls:
        return
    workers = min(PREFETCH_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_photo_bytes, url) for url in urls]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass


def prepare_photo_for_rect_cover(photo_bytes, rect_coords, scale=6, output_format="JPEG", is_redeemer=False):
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round(abs(x1 - x0) * scale)))
    target_h = max(1, int(round(abs(y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size

            ratio = max(target_w / max(1, src_w), target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)

            crop_x = (new_w - target_w) // 2
            overflow_h = max(0, new_h - target_h)
            crop_y = max(0, overflow_h // 2 - 25)  # shift 12px up = photo moves down slightly

            canvas = fitted.crop((crop_x, crop_y, crop_x + target_w, crop_y + target_h))
            fitted.close()
            if rgb is not img:
                rgb.close()

            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                canvas.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                             optimize=True, progressive=False, subsampling=1)
            else:
                canvas.save(buf, format="PNG")
            canvas.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def prepare_photo_for_rect_contain(photo_bytes, rect_coords, scale=6, output_format="JPEG"):
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round((x1 - x0) * scale)))
    target_h = max(1, int(round((y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size
            ratio = min(target_w / max(1, src_w),
                        target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
            fx = (target_w - new_w) // 2
            fy = (target_h - new_h) // 2
            canvas.paste(fitted, (fx, fy))
            fitted.close()
            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                canvas.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                             optimize=True, progressive=False, subsampling=1)
            else:
                canvas.save(buf, format="PNG")
            canvas.close()
            if rgb is not img:
                rgb.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def prepare_photo_for_rect(photo_bytes, rect_coords, scale=6, output_format="JPEG"):
    if not HAS_PIL or not photo_bytes:
        return photo_bytes
    x0, y0, x1, y1 = rect_coords
    target_w = max(1, int(round(abs(x1 - x0) * scale)))
    target_h = max(1, int(round(abs(y1 - y0) * scale)))
    try:
        with Image.open(io.BytesIO(photo_bytes)) as img:
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            rgb = img.convert("RGB")
            src_w, src_h = rgb.size

            ratio = max(target_w / max(1, src_w), target_h / max(1, src_h))
            new_w = max(1, int(round(src_w * ratio)))
            new_h = max(1, int(round(src_h * ratio)))
            fitted = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)

            crop_x = (new_w - target_w) // 2
            overflow_h = max(0, new_h - target_h)
            crop_y = max(0, overflow_h // 2 - 12)  # shift 12px up = photo moves down slightly
            canvas = fitted.crop((crop_x, crop_y, crop_x + target_w, crop_y + target_h))
            fitted.close()
            if rgb is not img:
                rgb.close()

            buf = io.BytesIO()
            save_fmt = (output_format or "JPEG").upper()
            if save_fmt == "JPEG":
                canvas.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY,
                             optimize=True, progressive=False, subsampling=1)
            else:
                canvas.save(buf, format="PNG")
            canvas.close()
            return buf.getvalue()
    except Exception:
        return photo_bytes


def insert_image_safe(page, rect, photo_bytes):
    if not photo_bytes:
        return
    page.insert_image(rect, stream=photo_bytes, overlay=True, keep_proportion=False)

