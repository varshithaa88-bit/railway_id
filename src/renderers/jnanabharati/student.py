import io
import os
import re
import fitz
import logging
from PIL import Image, ImageOps, ImageDraw
from src.config import BASE_DIR, PHOTO_EMBED_SCALE
from src.utils.photo import fetch_photo_bytes
from src.utils.pdf import _PDF_SAVE_OPTS

log = logging.getLogger("idcard.renderer.jnanabharati.student")

# Font file paths
LIB_SANS_TTF = str(BASE_DIR / "LiberationSans-Bold.ttf")
ARCHIVO_TTF  = str(BASE_DIR / "ArchivoBlack-Regular.ttf")

# Coords and placeholders
def _rgb(x):
    return ((x >> 16 & 0xFF) / 255.0, (x >> 8 & 0xFF) / 255.0, (x & 0xFF) / 255.0)

CLASS_COLOR = _rgb(0x4570FF)

PLACEHOLDERS = {
    "class_num":    dict(origin=(23.05516, 114.73078), size=15.488, color=CLASS_COLOR, font="archivo"),
    "class_suffix": dict(origin=(33.48307, 107.88457), size= 9.491, color=CLASS_COLOR, font="archivo"),
    "blood_group":  dict(origin=(117.89593, 113.89590), size= 5.884, color=_rgb(0xFFFFFF), font="libSans",
                         align="center", center_x=124.09),
    "name":         dict(origin=( 39.24439, 142.70671), size= 8.996, color=_rgb(0xFFFFFF), font="libSans",
                         align="center", center_x= 76.50),
    "adm_no":       dict(origin=( 47.98084, 161.29890), size= 5.997, color=_rgb(0x000000), font="libSans"),
    "father":       dict(origin=( 47.98084, 177.84126), size= 5.997, color=_rgb(0x000000), font="libSans"),
    "mother":       dict(origin=( 47.70485, 193.49648), size= 5.997, color=_rgb(0x000000), font="libSans"),
    "dob":          dict(origin=( 47.07488, 209.15173), size= 5.997, color=_rgb(0x000000), font="libSans"),
}

OLD_TEXT = {
    "3", "rd", "AB+", "ROHAN SINGH ", "SUYASH SINGH",
    "POOJA SINGH", "1234", "10-10-2018", "ROHAN SINGH"
}

PHOTO_FRAME         = fitz.Rect(56.12399, 76.06030, 96.87601, 126.52277)
ORIGINAL_PHOTO_XREF = 96
PHOTO_INNER_PAD_PT  = 1.5
PHOTO_CORNER_RADIUS_PT = 3.0
TEXT_TRACKING_RATIO = 0.081

# Class-field layout constants (LEFT of the photo)
CLASS_AREA = fitz.Rect(
    5.0,
    PHOTO_FRAME.y0 + 12.0,
    PHOTO_FRAME.x0 - 2.12,
    PHOTO_FRAME.y1 - 1.5,
)
CLASS_NUM_SIZE_REF    = 15.488
CLASS_SUFFIX_SIZE_REF = 9.491
CLASS_RIGHT_SAFETY_PT = 2.5

def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

_ORDINAL = {1: "st", 2: "nd", 3: "rd"}
def _ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return _ORDINAL.get(n % 10, "th")

def classify_class_text(raw):
    if raw is None:
        return ("empty",)
    s = str(raw).strip()
    if not s:
        return ("empty",)

    n = _norm(s)
    if n in ("PREKG", "PREKINDERGERTEN", "PREKINDERGARTEN"):
        return ("twoline", "PRE", "KG")
    if n in ("PLAYHOME", "PLAYHOUSE", "PLAYGROUP"):
        return ("twoline", "PLAY", "HOME")

    if n in {"LKG", "UKG", "KG", "KG1", "KG2", "NUR", "NURSERY"}:
        return ("single", n if n != "NURSERY" else "NUR")

    m = re.match(r"^\s*(\d{1,3})\s*(st|nd|rd|th)?\s*$", s, flags=re.IGNORECASE)
    if m:
        digits = m.group(1)
        suffix = (m.group(2) or _ordinal_suffix(int(digits))).lower()
        return ("numeric", digits, suffix)

    return ("generic", s)

def _measure(font, text, size):
    return font.text_length(text, fontsize=size)

def _cap_height(font, size):
    try:
        asc = font.ascender * size
        desc = font.descender * size
        return asc + desc
    except Exception:
        return size * 0.72

def draw_class(page, archivo_font, archivo_fontname, raw_value):
    kind = classify_class_text(raw_value)
    if kind[0] == "empty":
        return

    area = CLASS_AREA
    max_w = area.width - CLASS_RIGHT_SAFETY_PT
    max_h = area.height
    color = CLASS_COLOR
    font  = archivo_font
    fn    = archivo_fontname

    def _place_line(text, size, cx, cy):
        w = _measure(font, text, size)
        ch = _cap_height(font, size)
        x = cx - w / 2.0
        y_baseline = cy + ch / 2.0
        page.insert_text(fitz.Point(x, y_baseline), text,
                         fontsize=size, fontname=fn, color=color, overlay=True)

    # 1. CASE 1: Numeric
    if kind[0] == "numeric":
        digits, suffix = kind[1], kind[2]
        ratio = CLASS_SUFFIX_SIZE_REF / CLASS_NUM_SIZE_REF
        main_size = CLASS_NUM_SIZE_REF

        for _ in range(60):
            suf_size = main_size * ratio
            digits_w = _measure(font, digits, main_size)
            suffix_w = _measure(font, suffix, suf_size)
            gap      = main_size * 0.05
            block_w  = digits_w + gap + suffix_w
            if block_w <= max_w - 1.0:
                break
            main_size *= 0.96

        digit_ch = _cap_height(font, main_size)
        cy = (area.y0 + area.y1) / 2.0
        block_x0 = (area.x0 + area.x1 - block_w) / 2.0

        digit_baseline = cy + digit_ch / 2.0
        page.insert_text(fitz.Point(block_x0, digit_baseline), digits,
                         fontsize=main_size, fontname=fn, color=color, overlay=True)

        suf_x = block_x0 + digits_w + gap
        suf_ch = _cap_height(font, suf_size)
        suf_baseline = digit_baseline - (digit_ch - suf_ch) - suf_ch * 0.05
        suf_baseline = max(suf_baseline, area.y0 + suf_ch)
        page.insert_text(fitz.Point(suf_x, suf_baseline), suffix,
                         fontsize=suf_size, fontname=fn, color=color, overlay=True)
        return

    # 2. CASE 2: short single line (LKG, UKG)
    if kind[0] == "single":
        text = kind[1]
        size = 13.5
        for _ in range(60):
            w = _measure(font, text, size)
            if w <= max_w:
                break
            size *= 0.96
        cx = (area.x0 + area.x1) / 2.0
        cy = (area.y0 + area.y1) / 2.0
        _place_line(text, size, cx, cy)
        return

    # 3. CASE 3 / 4: forced two-line layout
    if kind[0] == "twoline":
        line1, line2 = kind[1], kind[2]
        size = 12.0
        line_gap_ratio = 0.20
        for _ in range(80):
            w1 = _measure(font, line1, size)
            w2 = _measure(font, line2, size)
            ch = _cap_height(font, size)
            block_h = 2 * ch + size * line_gap_ratio
            if max(w1, w2) <= max_w and block_h <= max_h - 2.0:
                break
            size *= 0.95
        cx = (area.x0 + area.x1) / 2.0
        cy = (area.y0 + area.y1) / 2.0
        ch = _cap_height(font, size)
        gap = size * line_gap_ratio
        cy1 = cy - (ch + gap) / 2.0
        cy2 = cy + (ch + gap) / 2.0
        _place_line(line1, size, cx, cy1)
        _place_line(line2, size, cx, cy2)
        return

    # 4. CASE 5 (fallback)
    if kind[0] == "generic":
        text = kind[1]
        size = 12.0
        for _ in range(80):
            w = _measure(font, text, size)
            if w <= max_w:
                break
            size *= 0.94
        cx = (area.x0 + area.x1) / 2.0
        cy = (area.y0 + area.y1) / 2.0
        _place_line(text, size, cx, cy)
        return

def prepare_photo(photo_bytes, frame_w_pt, frame_h_pt, corner_radius_pt=0.0, dpi=600):
    img = Image.open(io.BytesIO(photo_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    img = img.convert("RGB")

    target_ratio = frame_w_pt / frame_h_pt
    src_ratio    = img.width / img.height

    if src_ratio > target_ratio:
        new_w = int(round(img.height * target_ratio))
        x0    = (img.width - new_w) // 2
        img   = img.crop((x0, 0, x0 + new_w, img.height))
    else:
        new_h    = int(round(img.width / target_ratio))
        overflow = img.height - new_h
        y0       = max(0, int(round(overflow * 0.25)))
        img      = img.crop((0, y0, img.width, y0 + new_h))

    tgt_w = int(round(frame_w_pt / 72.0 * dpi))
    tgt_h = int(round(frame_h_pt / 72.0 * dpi))
    img   = img.resize((tgt_w, tgt_h), Image.Resampling.LANCZOS)

    if corner_radius_pt > 0:
        radius_px = int(round(corner_radius_pt / 72.0 * dpi))
        mask = Image.new("L", (tgt_w, tgt_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, tgt_w - 1, tgt_h - 1),
            radius=radius_px,
            fill=255,
        )
        img = img.convert("RGBA")
        img.putalpha(mask)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    img.close()
    return buf.getvalue()

def _render_jnanabharati_student_card_bytes(student: dict, tmpl_bytes: bytes):
    doc  = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    # 1. Redact placeholders
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                span_text = span["text"].strip()
                if span_text in OLD_TEXT or any(o in span_text for o in OLD_TEXT):
                    r = fitz.Rect(span["bbox"]) + (-0.15, -0.15, 0.15, 0.15)
                    page.add_redact_annot(r, fill=None)
    
    page.apply_redactions(
        images   = fitz.PDF_REDACT_IMAGE_NONE,
        graphics = fitz.PDF_REDACT_LINE_ART_NONE,
        text     = fitz.PDF_REDACT_TEXT_REMOVE,
    )

    # 2. Blank the original photo (xref 96)
    try:
        page.replace_image(
            ORIGINAL_PHOTO_XREF,
            pixmap=fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False),
        )
    except Exception as e:
        log.warning("Could not blank photo xref %d: %s", ORIGINAL_PHOTO_XREF, e)

    # 3. Insert new photo from url
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes:
        photo_rect = fitz.Rect(
            PHOTO_FRAME.x0 + PHOTO_INNER_PAD_PT,
            PHOTO_FRAME.y0 + PHOTO_INNER_PAD_PT,
            PHOTO_FRAME.x1 - PHOTO_INNER_PAD_PT,
            PHOTO_FRAME.y1 - PHOTO_INNER_PAD_PT,
        )
        inner_radius_pt = max(0.5, PHOTO_CORNER_RADIUS_PT - PHOTO_INNER_PAD_PT * 0.5)
        try:
            png_bytes = prepare_photo(
                photo_bytes,
                photo_rect.width,
                photo_rect.height,
                corner_radius_pt=inner_radius_pt,
                dpi=max(300, int(72.0 * PHOTO_EMBED_SCALE))
            )
            page.insert_image(photo_rect, stream=png_bytes, keep_proportion=False, overlay=True)
        except Exception as e:
            log.error("Photo crop/fit failed: %s", e)
            page.insert_image(photo_rect, stream=photo_bytes, keep_proportion=False, overlay=True)

    # 4. Load & insert fonts
    page.insert_font(fontname="LibSans", fontfile=LIB_SANS_TTF)
    page.insert_font(fontname="Archivo", fontfile=ARCHIVO_TTF)

    _FMAP = {"libSans": "LibSans", "archivo": "Archivo"}
    
    # Custom text length function with tracking
    def _tracked_width(font, text, fontsize, tracking):
        base = font.text_length(text, fontsize=fontsize)
        if len(text) <= 1:
            return base
        return base + tracking * (len(text) - 1)

    # Custom text drawer with tracking
    def _draw_tracked(x, y, text, fontsize, fontname, color, font, tracking):
        cx = x
        for ch in text:
            page.insert_text(fitz.Point(cx, y), ch,
                             fontsize=fontsize, fontname=fontname, color=color, overlay=True)
            cx += font.text_length(ch, fontsize=fontsize) + tracking

    def draw(key, text):
        val = str(text or "").strip()
        if not val:
            return
        p = PLACEHOLDERS[key]
        font = fitz.Font(fontfile=LIB_SANS_TTF if p["font"] == "libSans" else ARCHIVO_TTF)
        tracking = TEXT_TRACKING_RATIO * p["size"] if p["font"] == "libSans" else 0.0

        if p.get("align") == "center":
            tw = _tracked_width(font, val, p["size"], tracking)
            x0 = p["center_x"] - tw / 2.0
        else:
            x0 = p["origin"][0]
        y0 = p["origin"][1]

        if tracking == 0.0:
            page.insert_text(fitz.Point(x0, y0), val, fontsize=p["size"],
                             fontname=_FMAP[p["font"]], color=p["color"], overlay=True)
        else:
            _draw_tracked(x0, y0, val, p["size"], _FMAP[p["font"]],
                          p["color"], font, tracking)

    draw("blood_group",  student.get("blood_group", ""))
    draw("name",         student.get("student_name", ""))
    draw("adm_no",       student.get("adm_no", ""))
    draw("father",       student.get("father_name", ""))
    draw("mother",       student.get("mother_name", ""))
    draw("dob",          student.get("dob", ""))

    # 5. Dynamic class field engine
    archivo_font = fitz.Font(fontfile=ARCHIVO_TTF)
    draw_class(page, archivo_font, "Archivo", student.get("class", ""))

    doc.bake()
    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
