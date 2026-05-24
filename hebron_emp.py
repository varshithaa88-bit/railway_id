"""
====================================================================
 HEBRON MISSION SCHOOL  -  ID CARD OVERLAY AUTOMATION (A4 LAYOUT)
 (Run by Love India Brethren Education and Welfare Trust)
====================================================================
Fixed-alignment version. Generates pixel-perfect ID cards by
overlaying employee data onto the original card template, with each
value text neatly aligned to its colon on the same baseline.

Input Excel columns supported:
    id, employee_name, dob, husband_father_name, contact_no,
    address, designation, employee_photo
Also tolerates the older column names:
    name, fh_name, mobile, image_url, validity

Output:
    out/all_id_cards_A4.pdf        - multi-page A4 landscape PDF (JPEG-in-PDF, compressed)
    out/sheet_XX.jpg               - one JPEG per A4 page (10 cards)        [Fix 1]
    out/single/<name>.jpg          - individual high-res cards as JPEG       [Fix 1]

Fixes applied vs original:
    Fix 1  – JPEG (quality=92, 4:2:2) replaces PNG everywhere.
    Fix 2  – PDF assembled from JPEG sheets via Pillow PDF writer with
             correct resolution metadata; deflate/garbage-collect pass
             via PyMuPDF on the final file.
    Fix 3  – _to_printer_safe_rgb() strips alpha/palette/LA before any
             save → no spooler transparency crashes.
    Fix 4  – doc.close() after every fitz rasterise; finally-blocks
             for temp file cleanup; extras list closed after PDF write.

USAGE (Windows / Mac / Linux – VS Code terminal):
    pip install PyMuPDF Pillow pandas openpyxl requests
    python id_card_generator.py --csv employees.xlsx \\
            --template card_template.pdf --out out
"""

import os, io, argparse, requests, sys, subprocess
from pathlib import Path

import fitz                                 # PyMuPDF
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

# ====================================================================
#  0. FONT BOOTSTRAP
# ====================================================================
_FONT_URLS = {
    "DejaVuSans-Bold.ttf": [
        "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans-Bold.ttf",
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/dejavu-fonts/dejavu-fonts@master/ttf/DejaVuSans-Bold.ttf",
    ],
    "DejaVuSans.ttf": [
        "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf",
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
        "https://cdn.jsdelivr.net/gh/dejavu-fonts/dejavu-fonts@master/ttf/DejaVuSans.ttf",
    ],
}

_SYS_FONT_PATHS = {
    "DejaVuSans-Bold.ttf": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "DejaVuSans.ttf": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}

def _resolve_existing(filename):
    for p in _SYS_FONT_PATHS.get(filename, []):
        if Path(p).exists():
            return p
    return None

def download_fonts(dest_dir="."):
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    if sys.platform.startswith("linux"):
        try:
            subprocess.run(
                ["apt-get", "install", "-y", "fonts-dejavu-core"],
                capture_output=True, text=True, timeout=60
            )
        except Exception:
            pass

    for filename, urls in _FONT_URLS.items():
        target = dest / filename
        if target.exists():
            continue
        if _resolve_existing(filename):
            continue
        for url in urls:
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if len(r.content) < 1000:
                    raise ValueError("Response too small")
                target.write_bytes(r.content)
                print(f"  [font] downloaded {filename}")
                break
            except Exception:
                continue

download_fonts(".")

# ====================================================================
#  1. CONSTANTS / PATHS / LAYOUT
# ====================================================================
DPI       = 500
PT2PX     = DPI / 72.0
MM2PX     = DPI / 25.4

CARD_W_PT = 153.0
CARD_H_PT = 243.0
CARD_W_PX = int(round(CARD_W_PT * PT2PX))
CARD_H_PX = int(round(CARD_H_PT * PT2PX))

# ----- Exact printed card size (ISO CR80 / standard ID) -----
# Width = 55 mm, Height = 86 mm — FIXED, not scaled to fit.
# Verify it fits: 5×55 + 4×2 + 2×5 = 293 mm < 297 mm (A4 width)  ✓
#                 2×86 + 1×3 + 2×5 = 185 mm < 210 mm (A4 height)  ✓
FINAL_CARD_W_MM = 55.0
FINAL_CARD_H_MM = 86.0

A4_W_MM, A4_H_MM = 297.0, 210.0
A4_W_PX = int(round(A4_W_MM * MM2PX))
A4_H_PX = int(round(A4_H_MM * MM2PX))

COLS, ROWS     = 5, 2
CARDS_PER_PAGE = COLS * ROWS

MARGIN_MM, H_GAP_MM, V_GAP_MM = 5.0, 2.0, 3.0

FINAL_CARD_W_PX = int(round(FINAL_CARD_W_MM * MM2PX))
FINAL_CARD_H_PX = int(round(FINAL_CARD_H_MM * MM2PX))

# Sanity-check: warn if cards don't actually fit on the sheet
_used_w = COLS*FINAL_CARD_W_MM + (COLS-1)*H_GAP_MM + 2*MARGIN_MM
_used_h = ROWS*FINAL_CARD_H_MM + (ROWS-1)*V_GAP_MM + 2*MARGIN_MM
if _used_w > A4_W_MM or _used_h > A4_H_MM:
    print(f"[WARN] Cards overflow A4! "
          f"Need {_used_w:.1f}×{_used_h:.1f} mm, sheet is {A4_W_MM}×{A4_H_MM} mm")

print(f"[layout] A4 landscape: {A4_W_MM} x {A4_H_MM} mm @ {DPI} DPI "
      f"= {A4_W_PX} x {A4_H_PX} px")
print(f"[layout] Card size (exact): {FINAL_CARD_W_MM} x {FINAL_CARD_H_MM} mm "
      f"= {FINAL_CARD_W_PX} x {FINAL_CARD_H_PX} px  |  "
      f"grid uses {_used_w:.1f} x {_used_h:.1f} mm of A4")

# ====================================================================
#  2. COLOURS
# ====================================================================
COL_RED_BAND   = (170,  15,  15)
COL_WHITE      = (255, 255, 255)
COL_BLACK      = (  0,   0,   0)
COL_LABEL_RED  = (218,  16,  16)
COL_VALIDITY_R = (170,  16,  16)
COL_ORANGE     = (255, 117,  31)

# ====================================================================
#  3. PLACEHOLDER ERASE RECTS
# ====================================================================
ERASE = [
    (8.0,  133.0, 112.0, 146.0, COL_RED_BAND),
    (50.0, 145.5,  73.0, 154.5, COL_RED_BAND),
    (53.5, 161.0,  72.0, 169.6, COL_WHITE),
    (53.5, 169.0,  72.0, 177.2, COL_WHITE),
    (53.5, 176.5,  72.0, 184.6, COL_WHITE),
    (53.5, 190.5,  72.0, 198.8, COL_WHITE),
    (112.0, 111.2, 142.0, 124.0, COL_WHITE),
]

# ====================================================================
#  4. FIELD COORDINATES & STYLES
# ====================================================================
VALUE_X_START = 61.5
VALUE_X_END   = 150.0

FIELDS = {
    "name": {
        "rect_pt"  : (8.0, 134.0, 112.0, 145.6),
        "color"    : COL_WHITE,
        "weight"   : "bold",
        "size_pt"  : 8.5,
        "align"    : "center",
        "max_size" : 9.0,
        "min_size" : 5.5,
        "uppercase": True,
    },
    "designation": {
        "rect_pt"  : (50.5, 147.5, 110.0, 155.0),
        "color"    : COL_WHITE,
        "weight"   : "bold",
        "size_pt"  : 5.2,
        "align"    : "left",
        "max_size" : 5.5,
        "min_size" : 3.8,
        "uppercase": True,
        "prefix"   : " ",
    },
    "validity": {
        "rect_pt"  : (112.5, 111.6, 142.0, 122.6),
        "color"    : COL_VALIDITY_R,
        "weight"   : "bold",
        "size_pt"  : 7.5,
        "align"    : "center",
        "max_size" : 8.0,
        "min_size" : 5.5,
        "uppercase": False,
    },
    "fh_name": {
        "rect_pt"  : (VALUE_X_START, 161.5, VALUE_X_END, 169.8),
        "color"    : COL_BLACK,
        "weight"   : "bold",
        "size_pt"  : 5.5,
        "align"    : "left",
        "max_size" : 6.0,
        "min_size" : 3.8,
        "uppercase": False,
        "prefix"   : "",
    },
    "dob": {
        "rect_pt"  : (VALUE_X_START, 169.0, VALUE_X_END, 177.2),
        "color"    : COL_BLACK,
        "weight"   : "bold",
        "size_pt"  : 5.5,
        "align"    : "left",
        "max_size" : 6.0,
        "min_size" : 3.8,
        "uppercase": False,
        "prefix"   : "",
    },
    "address": {
        "rect_pt"   : (VALUE_X_START, 176.5, VALUE_X_END, 190.4),
        "color"     : COL_BLACK,
        "weight"    : "bold",
        "size_pt"   : 5.0,
        "align"     : "left",
        "valign"    : "top",
        "max_size"  : 5.5,
        "min_size"  : 3.4,
        "uppercase" : False,
        "wrap_lines": 2,
        "prefix"    : "",
        "hang_indent": False,
    },
    "mobile": {
        "rect_pt"  : (VALUE_X_START, 190.5, VALUE_X_END, 198.8),
        "color"    : COL_BLACK,
        "weight"   : "bold",
        "size_pt"  : 5.5,
        "align"    : "left",
        "max_size" : 6.0,
        "min_size" : 3.8,
        "uppercase": False,
        "prefix"   : "",
    },
}

PHOTO_BOX_PT    = (52.44, 74.28, 99.57, 128.23)
PHOTO_BORDER_PX = max(4, int(round(2 * PT2PX)))

# ====================================================================
#  5. FONTS
# ====================================================================
FONT_CANDIDATES = {
    "bold":    [
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "arialbd.ttf",
    ],
    "regular": [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "arial.ttf",
    ],
}

def _find_font(weight: str) -> str:
    for p in FONT_CANDIDATES.get(weight, []):
        if Path(p).exists():
            return p
    return FONT_CANDIDATES["bold"][0]

# ====================================================================
#  6. HELPER FUNCTIONS
# ====================================================================

# ---- FIX 3: printer-safe RGB flattener ----------------------------
def _to_printer_safe_rgb(img: Image.Image) -> Image.Image:
    """
    Convert any Pillow image to flat RGB so printer spoolers never
    choke on transparency, palette, or unexpected colour modes.
    Handles: RGBA, LA, P (palette+alpha), and any other non-RGB mode.
    """
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.split()[-1]          # last channel is alpha for both modes
        if img.mode == "LA":
            img = img.convert("RGBA")
        bg.paste(img, mask=alpha)
        return bg
    if img.mode == "P":
        # Palette images may carry transparency; convert via RGBA to be safe
        img_rgba = img.convert("RGBA")
        bg = Image.new("RGB", img_rgba.size, (255, 255, 255))
        bg.paste(img_rgba, mask=img_rgba.split()[3])
        return bg
    # Catch-all: CMYK, L, HSV, …
    return img.convert("RGB")
# -------------------------------------------------------------------

def pt_rect_to_px(rect_pt):
    x0, y0, x1, y1 = rect_pt
    return (int(round(x0*PT2PX)), int(round(y0*PT2PX)),
            int(round(x1*PT2PX)), int(round(y1*PT2PX)))

def clean_text(value) -> str:
    if value is None: return ""
    s = str(value)
    if s.lower() in ("nan", "none"): return ""
    return " ".join(s.split()).strip()

# ---- FIX 4: close fitz doc deterministically ----------------------
def load_template_pdf(template_pdf: str) -> Image.Image:
    doc = fitz.open(template_pdf)
    try:
        mat = fitz.Matrix(PT2PX, PT2PX)
        pix = doc[0].get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).copy()
    finally:
        doc.close()                      # always release the fitz document

    draw = ImageDraw.Draw(img)
    for (x0, y0, x1, y1, col) in ERASE:
        draw.rectangle(pt_rect_to_px((x0, y0, x1, y1)), fill=col)

    pbx   = pt_rect_to_px(PHOTO_BOX_PT)
    inner = (pbx[0]+PHOTO_BORDER_PX, pbx[1]+PHOTO_BORDER_PX,
             pbx[2]-PHOTO_BORDER_PX, pbx[3]-PHOTO_BORDER_PX)
    draw.rectangle(pbx,   fill=COL_ORANGE)
    draw.rectangle(inner, fill=(240, 240, 240))
    return img
# -------------------------------------------------------------------

def fit_font(text: str, font_path: str, max_w_px: int, max_h_px: int,
             start_pt: float, min_pt: float, max_pt: float):
    size_px = int(start_pt * PT2PX)
    min_px  = int(min_pt   * PT2PX)
    max_px  = int(max_pt   * PT2PX)
    size_px = min(max(size_px, min_px), max_px)

    while size_px >= min_px:
        font = ImageFont.truetype(font_path, size_px)
        bbox = font.getbbox(text)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_w_px and h <= max_h_px:
            return font
        size_px -= max(1, int(size_px * 0.05))
    return ImageFont.truetype(font_path, min_px)

def wrap_text(text: str, font, max_w_px: int, max_lines: int = 1):
    words = text.split()
    lines, current = [], ""
    for w in words:
        trial = (current + " " + w).strip() if current else w
        if font.getbbox(trial)[2] <= max_w_px:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
            if len(lines) == max_lines:
                while font.getbbox(current + "…")[2] > max_w_px and len(current) > 1:
                    current = current[:-1]
                current += "…"
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines or [""]

def draw_field(draw: ImageDraw.ImageDraw, text: str, spec: dict):
    if not text:
        return
    if spec.get("uppercase"):
        text = text.upper()
    if spec.get("prefix"):
        text = spec["prefix"] + text

    rect_px = pt_rect_to_px(spec["rect_pt"])
    x0, y0, x1, y1 = rect_px
    max_w, max_h = x1-x0, y1-y0
    font_path = _find_font(spec.get("weight", "bold"))
    wrap_lines = spec.get("wrap_lines", 1)

    if wrap_lines > 1:
        size_px = int(spec["max_size"] * PT2PX)
        min_px  = int(spec["min_size"] * PT2PX)
        font    = ImageFont.truetype(font_path, size_px)
        lines   = wrap_text(text, font, max_w, wrap_lines)
        while True:
            line_h  = int(font.size * 1.15)
            total_h = line_h * len(lines)
            if total_h <= max_h or size_px <= min_px:
                break
            size_px -= 1
            font  = ImageFont.truetype(font_path, size_px)
            lines = wrap_text(text, font, max_w, wrap_lines)
    else:
        font  = fit_font(text, font_path, max_w, max_h,
                         spec["size_pt"], spec["min_size"], spec["max_size"])
        lines = [text]

    valign  = spec.get("valign", "middle")
    line_h  = int(font.size * 1.15)
    total_h = line_h * len(lines)
    if valign == "top":
        y_cursor = y0
    elif valign == "bottom":
        y_cursor = y1 - total_h
    else:
        y_cursor = y0 + max(0, (max_h - total_h) // 2)

    hang_offset = 0
    if spec.get("hang_indent") and spec.get("prefix") and len(lines) > 1:
        pref = spec["prefix"]
        hang_offset = font.getbbox(pref)[2] - font.getbbox(pref)[0]

    for idx, line in enumerate(lines):
        bbox   = font.getbbox(line)
        line_w = bbox[2] - bbox[0]
        if spec.get("align") == "center":
            x = x0 + (max_w - line_w) // 2 - bbox[0]
        else:
            x = x0 - bbox[0]
            if idx > 0 and hang_offset:
                x += hang_offset
        draw.text((x, y_cursor - bbox[1]), line, font=font, fill=spec["color"])
        y_cursor += line_h

def load_photo(src) -> "Image.Image | None":
    if not src:
        return None
    src = str(src).strip()
    if not src or src.lower() in ("nan", "none"):
        return None
    try:
        if src.lower().startswith(("http://", "https://")):
            r = requests.get(src, timeout=25,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content))
            return _to_printer_safe_rgb(img)   # Fix 3
        p = Path(src)
        if p.exists():
            img = Image.open(p)
            return _to_printer_safe_rgb(img)   # Fix 3
    except Exception as e:
        print(f"  [warn] could not load photo '{src}': {e}")
    return None

def paste_photo(card: Image.Image, photo: "Image.Image | None"):
    pbx   = pt_rect_to_px(PHOTO_BOX_PT)
    inner = (pbx[0]+PHOTO_BORDER_PX, pbx[1]+PHOTO_BORDER_PX,
             pbx[2]-PHOTO_BORDER_PX, pbx[3]-PHOTO_BORDER_PX)
    iw, ih = inner[2]-inner[0], inner[3]-inner[1]

    if photo is None:
        draw = ImageDraw.Draw(card)
        draw.rectangle(inner, fill=(225, 225, 225))
        try:
            f = ImageFont.truetype(_find_font("bold"), int(6*PT2PX))
        except Exception:
            f = ImageFont.load_default()
        msg = "PHOTO"
        b   = f.getbbox(msg)
        draw.text((inner[0] + (iw - (b[2]-b[0]))//2,
                   inner[1] + (ih - (b[3]-b[1]))//2),
                  msg, font=f, fill=(140,140,140))
        return

    # v2.9 FULL-PHOTO FIT — was: center-crop to box ratio (cut legs off
    # full-length photos).  Now: scale-to-fit (contain) on a white
    # canvas so the whole employee photo is visible on the card.
    pw, ph = photo.size
    ratio  = min(iw / max(1, pw), ih / max(1, ph))
    nw, nh = max(1, int(pw * ratio)), max(1, int(ph * ratio))
    fitted = photo.resize((nw, nh), Image.LANCZOS)
    bg     = Image.new("RGB", (iw, ih), (255, 255, 255))
    bg.paste(fitted, ((iw - nw) // 2, (ih - nh) // 2))
    card.paste(bg, (inner[0], inner[1]))

# ====================================================================
#  7. COLUMN NAME NORMALISATION
# ====================================================================
COLUMN_ALIASES = {
    "name"       : ["name", "employee_name", "emp_name", "full_name"],
    "designation": ["designation", "post", "role", "title"],
    "validity"   : ["validity", "valid_till", "valid"],
    "fh_name"    : ["fh_name", "husband_father_name",
                    "father_husband_name", "father_name",
                    "fathers_name", "guardian"],
    "dob"        : ["dob", "date_of_birth", "birth_date"],
    "address"    : ["address", "addr", "residence"],
    "mobile"     : ["mobile", "contact_no", "phone", "phone_no",
                    "contact", "mobile_no"],
    "image_url"  : ["image_url", "employee_photo", "photo",
                    "photo_url", "image"],
    "id"         : ["id", "emp_id", "employee_id", "sno", "s_no"],
}

def normalise_row(row: dict) -> dict:
    norm     = {}
    lower_map = {str(k).strip().lower(): v for k, v in row.items()}
    for key, aliases in COLUMN_ALIASES.items():
        val = ""
        for a in aliases:
            if a in lower_map and clean_text(lower_map[a]):
                val = clean_text(lower_map[a])
                break
        norm[key] = val

    # Normalise DOB: strip timestamp, flip YYYY-MM-DD → DD-MM-YYYY
    if norm.get("dob"):
        date_only = norm["dob"].split(" ")[0]
        if "-" in date_only:
            parts = date_only.split("-")
            if len(parts[0]) == 4:
                norm["dob"] = f"{parts[2]}-{parts[1]}-{parts[0]}"
            else:
                norm["dob"] = date_only

    if not norm.get("validity"):
        norm["validity"] = "2026-27"
    return norm

# ====================================================================
#  8. RENDER A SINGLE CARD (native template resolution)
# ====================================================================
def render_card(row: dict, template_img: Image.Image) -> Image.Image:
    card = template_img.copy()
    paste_photo(card, load_photo(row.get("image_url")))

    draw       = ImageDraw.Draw(card)
    colon_font = ImageFont.truetype(_find_font("bold"), int(5.5 * PT2PX))
    for key in ["fh_name", "dob", "address", "mobile"]:
        y_pt = FIELDS[key]["rect_pt"][1]
        draw.text((int(54.7 * PT2PX), int(y_pt * PT2PX)),
                  ":", font=colon_font, fill=COL_BLACK)

    for key in ("name", "designation", "validity",
                "fh_name", "dob", "address", "mobile"):
        draw_field(draw, row.get(key, ""), FIELDS[key])

    return card

def safe_filename(s: str) -> str:
    keep = "-_.() "
    return "".join(c if c.isalnum() or c in keep else "_" for c in s).strip() or "card"

# ====================================================================
#  9. PREPARE CARD FOR A4 LAYOUT
# ====================================================================
def prepare_card_for_a4(card_img: Image.Image) -> Image.Image:
    resized = card_img.resize((FINAL_CARD_W_PX, FINAL_CARD_H_PX), Image.LANCZOS)
    return _to_printer_safe_rgb(resized)   # Fix 3: guarantee flat RGB

# ====================================================================
# 10. COMPOSE A4 LANDSCAPE SHEETS
# ====================================================================
def compose_a4_pages_iter(cards_resized):
    h_gap_px = int(round(H_GAP_MM * MM2PX))
    v_gap_px = int(round(V_GAP_MM * MM2PX))

    grid_w  = COLS*FINAL_CARD_W_PX + (COLS-1)*h_gap_px
    grid_h  = ROWS*FINAL_CARD_H_PX + (ROWS-1)*v_gap_px
    start_x = (A4_W_PX - grid_w) // 2
    start_y = (A4_H_PX - grid_h) // 2
    tick    = max(4, int(round(2 * MM2PX)))

    for page_idx in range(0, len(cards_resized), CARDS_PER_PAGE):
        page  = Image.new("RGB", (A4_W_PX, A4_H_PX), COL_WHITE)
        draw  = ImageDraw.Draw(page)
        chunk = cards_resized[page_idx:page_idx + CARDS_PER_PAGE]

        for i, c in enumerate(chunk):
            col = i % COLS
            row = i // COLS
            x   = start_x + col*(FINAL_CARD_W_PX + h_gap_px)
            y   = start_y + row*(FINAL_CARD_H_PX + v_gap_px)
            page.paste(c, (x, y))
            for cx, cy in [(x, y), (x+FINAL_CARD_W_PX, y),
                           (x, y+FINAL_CARD_H_PX),
                           (x+FINAL_CARD_W_PX, y+FINAL_CARD_H_PX)]:
                draw.line([(cx-tick, cy), (cx+tick, cy)],
                          fill=(200,200,200), width=1)
                draw.line([(cx, cy-tick), (cx, cy+tick)],
                          fill=(200,200,200), width=1)
        yield page

# ====================================================================
# 11. BATCH RUNNER
# ====================================================================

# Fix 1/2 JPEG constants
_JPEG_QUALITY     = 92
_JPEG_SUBSAMPLING = 1        # 1 = 4:2:2 (Pillow's subsampling kwarg)

def run(csv_path: str, template_pdf: str, out_dir: str,
        save_singles: bool = True):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    singles_dir = out / "single"
    if save_singles:
        singles_dir.mkdir(parents=True, exist_ok=True)

    template_img = load_template_pdf(template_pdf)   # Fix 4: doc closed inside

    if csv_path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(csv_path, dtype=str).fillna("")
    else:
        df = pd.read_csv(csv_path, dtype=str).fillna("")

    df.columns = [str(c).strip().lower() for c in df.columns]
    print(f"Loaded {len(df)} rows from {csv_path}")
    print(f"Columns: {list(df.columns)}")

    resized_cards = []
    for i, raw in df.iterrows():
        row  = normalise_row(raw.to_dict())
        name = row.get("name") or f"row{i+1}"
        card = render_card(row, template_img)

        # --- Fix 1: save singles as JPEG ---
        if save_singles:
            jpg = singles_dir / f"{i+1:03d}_{safe_filename(name)}.jpg"
            safe_card = _to_printer_safe_rgb(card)          # Fix 3
            safe_card.save(
                jpg, "JPEG",
                quality=_JPEG_QUALITY,
                subsampling=_JPEG_SUBSAMPLING,
                dpi=(DPI, DPI),
            )

        resized_cards.append(prepare_card_for_a4(card))
        print(f"  [{i+1}/{len(df)}] rendered: {name}")

    n_total  = len(resized_cards)
    n_pages  = (n_total + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    print(f"Composing {n_pages} A4 landscape page(s) "
          f"({CARDS_PER_PAGE} cards/page, {n_total} total)")

    # --- Fix 1/2: save sheets as JPEG, collect paths for PDF --------
    jpg_paths = []
    for idx, page in enumerate(compose_a4_pages_iter(resized_cards), start=1):
        safe_page = _to_printer_safe_rgb(page)               # Fix 3
        jpg_path  = out / f"sheet_{idx:02d}.jpg"
        safe_page.save(
            jpg_path, "JPEG",
            quality=_JPEG_QUALITY,
            subsampling=_JPEG_SUBSAMPLING,
            dpi=(DPI, DPI),
        )
        jpg_paths.append(jpg_path)
        print(f"  page {idx}/{n_pages} -> {jpg_path.name}")
        del page, safe_page

    # --- Fix 2: assemble PDF from JPEG sheets -----------------------
    pdf_path = out / "all_id_cards_A4.pdf"
    if jpg_paths:
        first  = Image.open(jpg_paths[0]).convert("RGB")
        extras = []
        try:
            for p in jpg_paths[1:]:
                extras.append(Image.open(p).convert("RGB"))

            # Pillow writes a JPEG-in-PDF stream with correct resolution tag
            first.save(
                pdf_path, "PDF",
                resolution=DPI,
                save_all=True,
                append_images=extras,
            )
            print(f"\n  PDF assembled (Pillow JPEG-in-PDF) -> {pdf_path}")
        finally:
            # Fix 4: close all image handles regardless of errors
            first.close()
            for img in extras:
                img.close()

        # --- Fix 2 (PyMuPDF pass): deflate + garbage-collect --------
        try:
            doc = fitz.open(str(pdf_path))
            try:
                doc.save(
                    str(pdf_path),
                    deflate=True,
                    deflate_images=True,
                    deflate_fonts=True,
                    garbage=4,
                    clean=True,
                    incremental=False,
                )
                print(f"  PDF optimised (deflate/garbage=4) -> {pdf_path}")
            finally:
                doc.close()                                # Fix 4
        except Exception as e:
            print(f"  [warn] PyMuPDF optimise pass skipped: {e}")
            print(f"  (Pillow JPEG-in-PDF is still valid at {pdf_path})")

    print("\nDone.")

# ====================================================================
# 12. CLI
# ====================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",       default="employees.xlsx",
                    help="Path to employees Excel or CSV file")
    ap.add_argument("--template",  default="card_template.pdf",
                    help="Path to the blank card template PDF")
    ap.add_argument("--out",       default="out",
                    help="Output directory")
    ap.add_argument("--no-singles", action="store_true",
                    help="Do not export individual high-res JPEGs.")
    args = ap.parse_args()
    run(args.csv, args.template, args.out,
        save_singles=not args.no_singles)