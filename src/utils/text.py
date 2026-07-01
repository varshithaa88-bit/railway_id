import os
import re
import logging
import threading
import fitz

from src.config import (
    ANTON_FONT, ARIAL_BOLD, _FALLBACK_FONT, DEFAULT_SESSION,
    ADDR_MAX_LINES, ADDR_LINE_GAP,
    REDEEMER_NAME_FONT_SIZE, REDEEMER_NAME_MIN_SIZE,
    REDEEMER_CLASS_FONT_SIZE, REDEEMER_VALUE_FONT_SIZE,
    REDEEMER_ADDRESS_MAX_LINES, REDEEMER_ADDRESS_LINE_GAP,
    REDEEMER_SESSION_FONT_SIZE, REDEEMER_SESSION_VALUE_RECT,
    TEARDROP_ITEMS
)

log = logging.getLogger("idcard.text")

CLASS_ORDER = {
    "NURSERY": 0, "LKG": 1, "UKG": 2,
    "1ST": 3, "2ND": 4, "3RD": 5, "4TH": 6,
    "5TH": 7, "6TH": 8, "7TH": 9, "8TH": 10,
}

def class_sort_key(cls_str):
    return CLASS_ORDER.get(str(cls_str).strip().upper(), 99)


# Font caching singletons
_anton_font_obj = None
_bold_font_obj  = None
_font_init_done = False
_font_lock      = threading.Lock()


def ensure_fonts():
    global _anton_font_obj, _bold_font_obj, _font_init_done
    if _font_init_done:
        return (
            _anton_font_obj, _bold_font_obj,
            str(ANTON_FONT) if ANTON_FONT.exists() else _FALLBACK_FONT,
            str(ARIAL_BOLD) if ARIAL_BOLD.exists() else _FALLBACK_FONT,
            "anton"   if ANTON_FONT.exists() else "libsans",
            "arialbd" if ARIAL_BOLD.exists() else "libsans",
        )
    with _font_lock:
        if _font_init_done:
            return (
                _anton_font_obj, _bold_font_obj,
                str(ANTON_FONT) if ANTON_FONT.exists() else _FALLBACK_FONT,
                str(ARIAL_BOLD) if ARIAL_BOLD.exists() else _FALLBACK_FONT,
                "anton"   if ANTON_FONT.exists() else "libsans",
                "arialbd" if ARIAL_BOLD.exists() else "libsans",
            )
        try:
            _anton_font_obj = fitz.Font(fontfile=str(ANTON_FONT)) if ANTON_FONT.exists() else (
                fitz.Font(fontfile=_FALLBACK_FONT) if _FALLBACK_FONT else fitz.Font("helv")
            )
            _bold_font_obj  = fitz.Font(fontfile=str(ARIAL_BOLD)) if ARIAL_BOLD.exists() else (
                fitz.Font(fontfile=_FALLBACK_FONT) if _FALLBACK_FONT else fitz.Font("helv")
            )
        except Exception as e:
            log.warning("Font load failed, falling back: %s", e)
            _anton_font_obj = fitz.Font(fontfile=_FALLBACK_FONT) if _FALLBACK_FONT else fitz.Font("helv")
            _bold_font_obj  = fitz.Font(fontfile=_FALLBACK_FONT) if _FALLBACK_FONT else fitz.Font("helv")
        _font_init_done = True
        return (
            _anton_font_obj, _bold_font_obj,
            str(ANTON_FONT) if ANTON_FONT.exists() else _FALLBACK_FONT,
            str(ARIAL_BOLD) if ARIAL_BOLD.exists() else _FALLBACK_FONT,
            "anton"   if ANTON_FONT.exists() else "libsans",
            "arialbd" if ARIAL_BOLD.exists() else "libsans",
        )


def _fit_size(font, text, max_width, base, min_size=4.0):
    s = base
    while s >= min_size:
        if font.text_length(text, fontsize=s) <= max_width:
            return s
        s -= 0.1
    return min_size


def _put_single(page, rect, text, fontfile, fontname, size, color, font_obj):
    if not text: return
    baseline_y = rect.y0 + size * font_obj.ascender
    page.insert_text(
        (rect.x0, baseline_y), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def draw_text_vertically_centered(page, rect, text, fontfile, fontname, font_obj, base_size, color):
    if not text: return
    size   = _fit_size(font_obj, text, rect.width, base_size, 4.0)
    text_h = size * (font_obj.ascender - font_obj.descender)
    baseline = rect.y0 + (rect.height + text_h) / 2.0 - size * abs(font_obj.descender)
    page.insert_text(
        (rect.x0, baseline), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def draw_text_centered_hv(page, rect, text, fontfile, fontname, font_obj, size, color):
    if not text: return
    size = _fit_size(font_obj, text, rect.width, size, 3.5)
    tw   = font_obj.text_length(text, fontsize=size)
    gh   = size * (font_obj.ascender - font_obj.descender)
    x    = rect.x0 + (rect.width - tw) / 2.0
    y    = rect.y0 + (rect.height + gh) / 2.0 - size * abs(font_obj.descender)
    page.insert_text(
        (x, y), text,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def clean_visible_text(text):
    if text is None:
        return ""
    text = str(text)
    text = (text
            .replace("\xa0", " ")
            .replace("\u200b", "")
            .replace("\u200c", "")
            .replace("\u200d", "")
            .replace("\ufeff", "")
            .replace("\ufffd", " "))
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return " ".join(text.split()).strip()


def has_html(text) -> bool:
    if not text:
        return False
    return bool(re.search(r"<[^>]+>", str(text)))


def clean_card_value(text):
    text = clean_visible_text(text)
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"nan", "none", "null", "nil", "0000-00-00", "00-00-0000", "0000/00/00"}:
        return ""
    if has_html(text):
        return ""
    digits_only = re.sub(r"[0\-/:\. ]", "", text)
    if not digits_only and re.search(r"[1-9]", text) is None:
        return ""
    return text


def insert_tracked_text(page, x, baseline_y, text, fontfile, fontname, font_obj, size, color, tracking=0.0):
    text = clean_visible_text(text)
    if not text:
        return
    if tracking <= 0 or len(text) <= 1:
        page.insert_text(
            (x, baseline_y), text,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=size, color=color, overlay=True,
        )
        return
    cursor = x
    for ch in text:
        page.insert_text(
            (cursor, baseline_y), ch,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=size, color=color, overlay=True,
        )
        cursor += font_obj.text_length(ch, fontsize=size) + tracking


def draw_redeemer_banner_text(page, text, center_x, baseline_y, max_width, fontfile, fontname, font_obj, base_size, color, tracking=0.0, min_size=4.0):
    text = clean_visible_text(text).upper()
    if not text:
        return
    size, adjusted_tracking = _fit_tracked_text(font_obj, text, max_width, base_size, tracking, min_size=min_size)
    text = _ellipsize_tracked_to_width(font_obj, text, max_width, size, adjusted_tracking)
    if not text:
        return
    total_width = _tracked_text_width(font_obj, text, size, adjusted_tracking)
    if total_width > max_width:
        adjusted_tracking = 0.0
        text = _ellipsize_tracked_to_width(font_obj, text, max_width, size, adjusted_tracking)
        total_width = _tracked_text_width(font_obj, text, size, adjusted_tracking)
    insert_tracked_text(
        page,
        center_x - total_width / 2.0,
        baseline_y,
        text,
        fontfile, fontname, font_obj, size, color, adjusted_tracking,
    )


def draw_redeemer_value(page, text, x, baseline_y, max_width, fontfile, fontname, font_obj, base_size, color, min_size=5.0):
    value = clean_card_value(text)
    if not value:
        return
    size = _fit_size(font_obj, value, max_width, base_size, min_size)
    value = _ellipsize_to_width(font_obj, value, max_width, size)
    if not value:
        return
    page.insert_text(
        (x, baseline_y), value,
        fontname=fontname, fontfile=str(fontfile) if fontfile else None,
        fontsize=size, color=color, overlay=True,
    )


def render_redeemer_address(page, addr, x, baseline_y, max_width, fontfile, fontname, font_obj, color, base_size=6.8, min_size=4.8, max_lines=2, line_gap=1.03):
    lines, target_fs = wrap_and_shrink_text(font_obj, addr, max_width, max_lines, base_size=base_size)
    if not lines:
        return
    step = target_fs * line_gap
    for idx, line in enumerate(lines):
        bl = baseline_y + idx * step
        page.insert_text(
            (x, bl), line,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=target_fs, color=color, overlay=True,
        )


def _tracked_text_width(font_obj, text, fontsize, tracking=0.0):
    if not text:
        return 0.0
    base = font_obj.text_length(text, fontsize=fontsize)
    if tracking <= 0 or len(text) <= 1:
        return base
    return base + tracking * (len(text) - 1)


def _fit_tracked_text(font_obj, text, max_width, base_size, tracking=0.0, min_size=4.0):
    size = base_size
    adjusted_tracking = tracking
    while size >= min_size:
        if _tracked_text_width(font_obj, text, size, adjusted_tracking) <= max_width:
            return size, adjusted_tracking
        size -= 0.1
        adjusted_tracking = tracking * (size / base_size) if base_size else tracking
    return min_size, 0.0


def _ellipsize_to_width(font_obj, text, max_width, fontsize):
    text = clean_visible_text(text)
    if not text:
        return ""
    if font_obj.text_length(text, fontsize=fontsize) <= max_width:
        return text
    ellipsis = "…"
    if font_obj.text_length(ellipsis, fontsize=fontsize) > max_width:
        return ""
    trimmed = text.rstrip()
    while trimmed and font_obj.text_length(trimmed + ellipsis, fontsize=fontsize) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ellipsis) if trimmed else ellipsis


def _ellipsize_tracked_to_width(font_obj, text, max_width, fontsize, tracking=0.0):
    text = clean_visible_text(text)
    if not text:
        return ""
    if _tracked_text_width(font_obj, text, fontsize, tracking) <= max_width:
        return text
    ellipsis = "…"
    if _tracked_text_width(font_obj, ellipsis, fontsize, 0.0) > max_width:
        return ""
    trimmed = text.rstrip()
    while trimmed and _tracked_text_width(font_obj, trimmed + ellipsis, fontsize, tracking) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ellipsis) if trimmed else ellipsis


def _addr_wrap_at_size(font_obj, words, max_width, fs):
    lines = []; cur = ""
    for w in words:
        if font_obj.text_length(w, fontsize=fs) > max_width:
            if cur: lines.append(cur); cur = ""
            trunc = ""; ellipsis = "…"
            for ch in w:
                if font_obj.text_length(trunc + ch + ellipsis, fontsize=fs) <= max_width:
                    trunc += ch
                else:
                    break
            lines.append(trunc + ellipsis); continue
        trial = (cur + " " + w).strip() if cur else w
        if font_obj.text_length(trial, fontsize=fs) <= max_width:
            cur = trial
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


def wrap_and_shrink_text(font_obj, text, max_width, max_lines, base_size=6.0):
    val = clean_card_value(text)
    if not val:
        return [], base_size
    
    def wrap_at_size(fs):
        words = val.split()
        lines = []
        cur_line = ""
        for w in words:
            trial = (cur_line + " " + w).strip() if cur_line else w
            if font_obj.text_length(trial, fontsize=fs) <= max_width:
                cur_line = trial
            else:
                if cur_line:
                    lines.append(cur_line)
                cur_line = w
        if cur_line:
            lines.append(cur_line)
        return lines

    fs = base_size
    while fs >= 1.0:
        lines = wrap_at_size(fs)
        all_words_fit = True
        for line in lines:
            if font_obj.text_length(line, fontsize=fs) > max_width:
                all_words_fit = False
                break
        if len(lines) <= max_lines and all_words_fit:
            return lines, fs
        fs -= 0.1

    return wrap_at_size(1.0), 1.0


def _centered_baseline_for_box(font_obj, y0, y1, fontsize):
    asc = getattr(font_obj, "ascender", 0.9)
    desc = getattr(font_obj, "descender", -0.2)
    text_h = fontsize * (asc - desc)
    return y0 + max(0.0, ((y1 - y0) - text_h) / 2.0) + fontsize * asc


def _wrap_fixed_text(font_obj, text, max_width, fontsize, max_lines=2):
    value = clean_card_value(text)
    if not value:
        return []
    lines = _addr_wrap_at_size(font_obj, value.split(), max_width, fontsize)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            last = lines[-1]
            ellipsis = "…"
            while last and font_obj.text_length(last + ellipsis, fontsize=fontsize) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + ellipsis) if last else ellipsis
    return lines


def render_address(page, rect, addr, fontfile, fontname, font_obj, color, max_x=None):
    addr = clean_card_value(addr)
    if not addr: return
    
    max_w = (max_x if max_x is not None else rect.x1) - rect.x0
    
    lines, target_fs = wrap_and_shrink_text(font_obj, addr, max_w, ADDR_MAX_LINES, base_size=5.5)
    if not lines: return
    
    line_step = target_fs * ADDR_LINE_GAP
    baseline0 = rect.y0 + target_fs * font_obj.ascender
    for i, line in enumerate(lines):
        baseline = baseline0 + i * line_step
        if baseline - target_fs * abs(font_obj.descender) > rect.y1: break
        page.insert_text(
            (rect.x0, baseline), line,
            fontname=fontname, fontfile=str(fontfile) if fontfile else None,
            fontsize=target_fs, color=color, overlay=True,
        )


def redraw_blood_teardrop(page, fill_color):
    shape = page.new_shape()
    p = lambda t: fitz.Point(*t)
    shape.draw_line(p(TEARDROP_ITEMS[0][1]), p(TEARDROP_ITEMS[0][2]))
    shape.draw_line(p(TEARDROP_ITEMS[1][1]), p(TEARDROP_ITEMS[1][2]))
    shape.draw_bezier(p(TEARDROP_ITEMS[2][1]), p(TEARDROP_ITEMS[2][2]),
                      p(TEARDROP_ITEMS[2][3]), p(TEARDROP_ITEMS[2][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[3][1]), p(TEARDROP_ITEMS[3][2]),
                      p(TEARDROP_ITEMS[3][3]), p(TEARDROP_ITEMS[3][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[4][1]), p(TEARDROP_ITEMS[4][2]),
                      p(TEARDROP_ITEMS[4][3]), p(TEARDROP_ITEMS[4][4]))
    shape.finish(color=fill_color, fill=fill_color, width=0, closePath=True)
    shape.commit(overlay=True)


def _make_card_transform(source_rect, target_rect):
    sx = target_rect.width / source_rect.width
    sy = target_rect.height / source_rect.height
    return {"src": source_rect, "dst": target_rect, "sx": sx, "sy": sy}


def _tr_point(tr, x, y):
    return fitz.Point(
        tr["dst"].x0 + (x - tr["src"].x0) * tr["sx"],
        tr["dst"].y0 + (y - tr["src"].y0) * tr["sy"],
    )


def _tr_rect(tr, coords):
    x0, y0, x1, y1 = coords
    p0 = _tr_point(tr, x0, y0)
    p1 = _tr_point(tr, x1, y1)
    return fitz.Rect(p0.x, p0.y, p1.x, p1.y)


def _tr_font_size(tr, size):
    return size * min(tr["sx"], tr["sy"])


def redraw_blood_teardrop_transformed(page, tr, fill_color):
    shape = page.new_shape()
    p = lambda t: _tr_point(tr, *t)
    shape.draw_line(p(TEARDROP_ITEMS[0][1]), p(TEARDROP_ITEMS[0][2]))
    shape.draw_line(p(TEARDROP_ITEMS[1][1]), p(TEARDROP_ITEMS[1][2]))
    shape.draw_bezier(p(TEARDROP_ITEMS[2][1]), p(TEARDROP_ITEMS[2][2]),
                      p(TEARDROP_ITEMS[2][3]), p(TEARDROP_ITEMS[2][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[3][1]), p(TEARDROP_ITEMS[3][2]),
                      p(TEARDROP_ITEMS[3][3]), p(TEARDROP_ITEMS[3][4]))
    shape.draw_bezier(p(TEARDROP_ITEMS[4][1]), p(TEARDROP_ITEMS[4][2]),
                      p(TEARDROP_ITEMS[4][3]), p(TEARDROP_ITEMS[4][4]))
    shape.finish(color=fill_color, fill=fill_color, width=0, closePath=True)
    shape.commit(overlay=True)


def _emp_value(student: dict, *keys, upper: bool = False) -> str:
    for k in keys:
        v = student.get(k, "")
        if v is None:
            continue
        s = clean_card_value(str(v))
        if s:
            return s.upper() if upper else s
    return ""

