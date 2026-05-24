"""
=====================================================================
  A. B. ASCENT PUBLIC SCHOOL — Employee ID Card Generator
  (Excel-driven • 500 DPI • 10 cards / A4 landscape page • Multi-page PDF)
=====================================================================
Author : Genspark AI

 ** PRINTER-SAFE BUILD **
 ----------------------------
 Fixes applied so the PDF prints reliably from any office printer
 (no more 'File Error' on the printer LCD):
   FIX 1  -  Sheet pages are saved as JPEG (DCT) instead of PNG.
   FIX 2  -  The multi-page PDF is built from those JPEG pages with
             save_all + resolution metadata + flat-RGB conversion,
             producing a small, well-formed PDF that every print
             driver can flatten without errors.
   FIX 3  -  Images are forced to plain RGB before PDF embedding
             (no alpha, no palette) - avoids spooler crashes.
   FIX 4  -  Temp files are flushed and closed deterministically so
             the spooler never sees a half-written PDF.

USAGE (Colab):
    !pip install PyMuPDF Pillow pandas openpyxl requests
    !apt-get install -y fonts-liberation fonts-dejavu

    from id_card_generator import generate
    generate(
        excel_path   = "employees.xlsx",
        template_pdf = "Card 4 (1)_full_card.pdf",
        out_pdf      = "ID_Cards_A4.pdf",
    )
=====================================================================
"""

import os
import io
import gc
import math
import tempfile
import requests
import pandas as pd
import fitz                                   # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

# =====================================================================
# 0. DOB NORMALISATION HELPER
# =====================================================================
def _format_dob(text: str) -> str:
    """Normalise any DOB string to DD-MM-YYYY, stripping timestamps."""
    if not text:
        return ""
    text = str(text).strip()
    low = text.lower()
    if low in {"nan", "none", "null", "", "0000-00-00", "00-00-0000",
               "0000/00/00", "00/00/0000"}:
        return ""

    if " " in text:
        text = text.split(" ")[0].strip()
    if "T" in text:
        text = text.split("T")[0].strip()

    fmt_candidates = [
        "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y",
        "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
        "%d.%m.%Y", "%Y.%m.%d",
        "%d-%b-%Y", "%d %b %Y", "%d %B %Y",
    ]
    for fmt in fmt_candidates:
        try:
            dt = datetime.strptime(text, fmt)
            return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
        except ValueError:
            continue

    try:
        dt = pd.to_datetime(text, dayfirst=True, errors="raise")
        return f"{dt.day:02d}-{dt.month:02d}-{dt.year:04d}"
    except Exception:
        return text


# =====================================================================
# 1. CONSTANTS — physical dimensions & A4 layout
# =====================================================================
DPI              = 500
MM_PER_INCH      = 25.4
PT_PER_INCH      = 72.0

# JPEG quality for printer-safe page export.  92 = visually lossless
# while keeping each A4 sheet at ~2-3 MB instead of ~80 MB (PNG).
JPEG_QUALITY     = 92

CARD_W_MM, CARD_H_MM = 55, 86
A4_W_MM,  A4_H_MM    = 297, 210

def mm_to_px(mm, dpi=DPI):
    return int(round(mm * dpi / MM_PER_INCH))

def pt_to_px(pt, dpi=DPI):
    return int(round(pt * dpi / PT_PER_INCH))

CARD_W_PX = mm_to_px(CARD_W_MM)
CARD_H_PX = mm_to_px(CARD_H_MM)
A4_W_PX   = mm_to_px(A4_W_MM)
A4_H_PX   = mm_to_px(A4_H_MM)

COLS, ROWS              = 5, 2
CARDS_PER_PAGE          = COLS * ROWS

GUTTER_X_PX = (A4_W_PX - COLS * CARD_W_PX) // (COLS + 1)
GUTTER_Y_PX = (A4_H_PX - ROWS * CARD_H_PX) // (ROWS + 1)

# =====================================================================
# 2. COLOR PALETTE
# =====================================================================
COLOR_ROYAL_BLUE = (0x1E, 0x40, 0xAF)
COLOR_NAME_RED   = (0xE8, 0x3A, 0x2F)
COLOR_YELLOW_BG  = (0xFF, 0xD9, 0x11)
COLOR_WHITE      = (255, 255, 255)

# =====================================================================
# 3. FONT RESOLUTION
# =====================================================================
def _find_font(candidates):
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None

FONT_BOLD = _find_font([
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
])
FONT_REG = _find_font([
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
])

FONT_ROBOTO_BOLD = _find_font([
    "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",
    "/usr/share/fonts/truetype/roboto/static/Roboto-Bold.ttf",
]) or FONT_BOLD

FONT_ANTON = _find_font([
    "/usr/share/fonts/truetype/anton/Anton-Regular.ttf",
]) or FONT_BOLD

assert FONT_BOLD and FONT_REG, (
    "Missing fonts. In Colab run: "
    "!apt-get install -y fonts-liberation fonts-dejavu"
)

# =====================================================================
# 4. PLACEHOLDER MAP — reference canvas: 638 x 1013 px
# =====================================================================
REF_W, REF_H = 638, 1013
SX = CARD_W_PX / REF_W
SY = CARD_H_PX / REF_H

def _scale(box):
    x1, y1, x2, y2 = box
    return (int(x1 * SX), int(y1 * SY),
            int(x2 * SX), int(y2 * SY))

PLACEHOLDERS = {
    "validity":    _scale((470, 422, 590, 458)),
    "photo":       _scale((224, 279, 414, 498)),
    "name":        _scale(( 60, 545, 470, 590)),
    "designation": _scale((210, 587, 480, 623)),
    "dob":         _scale((248, 657, 604, 690)),
    "fh_name":     _scale((248, 686, 604, 719)),
    "address":     _scale((248, 719, 604, 800)),
    "mobile":      _scale((248, 781, 430, 812)),
}

MOBILE_MASK_BOX = PLACEHOLDERS["mobile"]

# =====================================================================
# 5. FIXED FONT SIZES
# =====================================================================
PT_NAME        = 9.0
PT_DESIGNATION = 6.0
PT_VALUE       = 6.0
PT_SESSION     = 7.0

PX_NAME        = pt_to_px(PT_NAME)
PX_DESIGNATION = pt_to_px(PT_DESIGNATION)
PX_VALUE       = pt_to_px(PT_VALUE)
PX_SESSION     = pt_to_px(PT_SESSION)

PX_VALUE_MIN   = max(20, int(PX_VALUE * 0.55))
PX_NAME_MIN    = max(24, int(PX_NAME  * 0.45))

# =====================================================================
# 6. TEXT DRAWING HELPERS
# =====================================================================
def font_at(path, px):
    return ImageFont.truetype(path, px)

def shrink_to_fit(text, box, font_path, px_max, px_min):
    w_box = box[2] - box[0]
    h_box = box[3] - box[1]
    for px in range(int(px_max), int(px_min) - 1, -1):
        f = font_at(font_path, px)
        l, t, r, b = f.getbbox(text)
        if (r - l) <= w_box and (b - t) <= h_box:
            return f
    return font_at(font_path, int(px_min))

def draw_aligned(draw, text, box, font, color, align="left"):
    l, t, r, b = font.getbbox(text)
    tw, th = r - l, b - t
    x1, y1, x2, y2 = box
    if align == "center":
        x = x1 + (x2 - x1 - tw) / 2 - l
    elif align == "right":
        x = x2 - tw - l
    else:
        x = x1 - l
    y = y1 + (y2 - y1 - th) / 2 - t
    draw.text((x, y), text, font=font, fill=color)

def draw_wrapped_fixed(draw, text, box, font_path, color, px_size, px_min):
    w_box = box[2] - box[0]
    h_box = box[3] - box[1]
    for px in range(int(px_size), int(px_min) - 1, -1):
        f = font_at(font_path, px)
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if f.getbbox(test)[2] <= w_box:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        line_h = f.getbbox("Ay")[3] + 4
        total_h = len(lines) * line_h
        if total_h <= h_box and all(f.getbbox(ln)[2] <= w_box for ln in lines):
            row_h = line_h
            l, t, r, b = f.getbbox("Ay")
            th = b - t
            y = box[1] + (row_h - th) // 2 - t
            for ln in lines:
                draw.text((box[0], y), ln, font=f, fill=color)
                y += line_h
            return
    f = font_at(font_path, int(px_min))
    draw.text((box[0], box[1]), text, font=f, fill=color)

# =====================================================================
# 7. IMAGE HELPERS
# =====================================================================
def load_photo(url_or_path):
    try:
        if isinstance(url_or_path, str) and url_or_path.lower().startswith(
                ("http://", "https://")):
            r = requests.get(url_or_path, timeout=20)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        return Image.open(url_or_path).convert("RGB")
    except Exception as e:
        print(f"  ! photo load failed ({e}) — using blank.")
        ph = Image.new("RGB", (400, 520), (235, 235, 235))
        d = ImageDraw.Draw(ph)
        d.text((80, 240), "NO PHOTO", fill=(120, 120, 120),
               font=font_at(FONT_BOLD, 36))
        return ph

def cover_resize(im, w, h):
    """
    v2.9 FULL-PHOTO FIT — was: cover (center-crop), which cut off the
    legs of full-length photos.  Now: contain (letterbox on white) so
    the whole employee photo (head-to-toe) is preserved on the card.
    """
    iw, ih = im.size
    ratio = min(w / iw, h / ih)  # scale-to-FIT (was max() = cover)
    nw, nh = max(1, int(iw * ratio)), max(1, int(ih * ratio))
    fitted = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(fitted, ((w - nw) // 2, (h - nh) // 2))
    return canvas

# =====================================================================
# 8. TEMPLATE RASTERIZATION
# =====================================================================
def rasterize_template(template_pdf):
    doc = fitz.open(template_pdf)
    page = doc.load_page(0)
    pix = page.get_pixmap(dpi=DPI, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    if img.size != (CARD_W_PX, CARD_H_PX):
        img = img.resize((CARD_W_PX, CARD_H_PX), Image.LANCZOS)
    return img

# =====================================================================
# 9. RENDER ONE CARD
# =====================================================================
def render_card(template_img, row):
    card = template_img.copy()
    draw = ImageDraw.Draw(card)

    px1, py1, px2, py2 = PLACEHOLDERS["photo"]
    pw, ph = px2 - px1, py2 - py1
    photo = load_photo(row.get("image_url", ""))
    photo = cover_resize(photo, pw, ph)
    card.paste(photo, (px1, py1))

    masks = {
        "name":        COLOR_YELLOW_BG,
        "designation": COLOR_YELLOW_BG,
        "dob":         COLOR_WHITE,
        "fh_name":     COLOR_WHITE,
        "address":     COLOR_WHITE,
        "mobile":      COLOR_WHITE,
        "validity":    COLOR_WHITE,
    }
    for key, fill in masks.items():
        draw.rectangle(list(PLACEHOLDERS[key]), fill=fill)

    name = (str(row.get("name") or "")).upper().strip() or "NAME"
    f = shrink_to_fit(name, PLACEHOLDERS["name"], FONT_BOLD,
                      PX_NAME, PX_NAME_MIN)
    draw_aligned(draw, name, PLACEHOLDERS["name"], f, COLOR_NAME_RED,
                 align="center")

    desig = str(row.get("designation") or "").strip()
    f = font_at(FONT_ROBOTO_BOLD, PX_DESIGNATION)
    draw_aligned(draw, desig, PLACEHOLDERS["designation"], f,
                 COLOR_ROYAL_BLUE, align="left")

    f_val = font_at(FONT_BOLD, PX_VALUE)
    dob_val = str(row.get("dob") or "").strip()
    draw_aligned(draw, dob_val, PLACEHOLDERS["dob"], f_val,
                 COLOR_ROYAL_BLUE, align="left")

    for key in ("fh_name", "mobile"):
        v = str(row.get(key) or "").strip()
        draw_aligned(draw, v, PLACEHOLDERS[key], f_val, COLOR_ROYAL_BLUE,
                     align="left")

    addr = str(row.get("address") or "").strip()
    draw_wrapped_fixed(draw, addr, PLACEHOLDERS["address"], FONT_BOLD,
                       COLOR_ROYAL_BLUE, PX_VALUE, PX_VALUE)

    val = str(row.get("validity") or "2026-27").strip()
    f = shrink_to_fit(val, PLACEHOLDERS["validity"], FONT_ANTON,
                      PX_SESSION, max(20, int(PX_SESSION * 0.5)))
    draw_aligned(draw, val, PLACEHOLDERS["validity"], f,
                 COLOR_ROYAL_BLUE, align="center")

    return card

# =====================================================================
# 10. A4 SHEET COMPOSITION
# =====================================================================
def build_a4_page(card_images):
    sheet = Image.new("RGB", (A4_W_PX, A4_H_PX), COLOR_WHITE)
    for idx, card in enumerate(card_images):
        if idx >= CARDS_PER_PAGE:
            break
        r = idx // COLS
        c = idx % COLS
        x = GUTTER_X_PX + c * (CARD_W_PX + GUTTER_X_PX)
        y = GUTTER_Y_PX + r * (CARD_H_PX + GUTTER_Y_PX)
        sheet.paste(card, (x, y))
    return sheet

# =====================================================================
# 10b. PRINTER-SAFE IMAGE HELPERS
# =====================================================================
def _to_printer_safe_rgb(img: Image.Image) -> Image.Image:
    """
    Force the image to flat RGB.  Print spoolers can fail on RGBA /
    palette images so we always flatten before saving.
    """
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _save_sheet_jpeg(sheet: Image.Image, path: str):
    """FIX 1: write the A4 sheet as JPEG, not PNG."""
    _to_printer_safe_rgb(sheet).save(
        path, "JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
        progressive=False,      # baseline JPEG = best printer compat
        dpi=(DPI, DPI),
        subsampling=1,          # 4:2:2 - high quality but compact
    )


def _save_pdf_from_jpegs(jpeg_paths, out_pdf: str):
    """
    FIX 2: build the final multi-page PDF from compressed JPEG sheets
    using Pillow's PDF writer.  This produces a small, valid PDF whose
    every page is a single DCT/JPEG stream - the format every printer
    RIP understands.
    """
    if not jpeg_paths:
        return

    first = _to_printer_safe_rgb(Image.open(jpeg_paths[0]))
    rest  = [_to_printer_safe_rgb(Image.open(p)) for p in jpeg_paths[1:]]

    first.save(
        out_pdf,
        "PDF",
        resolution=float(DPI),
        save_all=True,
        append_images=rest,
        producer="ABAscent-IDCardGenerator",
        title="Employee ID Cards",
    )

    first.close()
    for r in rest:
        r.close()

# =====================================================================
# 11. PUBLIC ENTRY POINT
# =====================================================================
RECOMMENDED_COLUMNS = [
    "name", "designation", "emp_id", "fh_name",
    "dob", "address", "mobile", "validity", "image_url",
]
REQUIRED_COLUMNS = ["name", "designation"]


def generate_employee_pdf(records, template_pdf, out_pdf):
    """
    Entry point called by app.py.

    Renders all employees, writes one JPEG per A4 sheet, then
    assembles a printer-safe compressed multi-page PDF.
    """
    print(f"-> {len(records)} employees loaded")

    for row in records:
        raw_dob = str(row.get("dob") or row.get("DOB") or "").strip()
        row["dob"] = _format_dob(raw_dob)

    template_img = rasterize_template(template_pdf)
    print(f"-> Template rasterized at {DPI} DPI "
          f"({template_img.size[0]} x {template_img.size[1]} px)")

    n_pages = math.ceil(len(records) / CARDS_PER_PAGE) if records else 0
    print(f"-> Composing {n_pages} A4 landscape page(s) ...")

    tmp_dir = tempfile.mkdtemp(prefix="idcards_ab_")
    page_files = []

    try:
        for p in range(n_pages):
            chunk = records[p * CARDS_PER_PAGE : (p + 1) * CARDS_PER_PAGE]
            cards = []
            for j, row in enumerate(chunk, 1):
                idx = p * CARDS_PER_PAGE + j
                cards.append(render_card(template_img, row))
                print(f"   * card {idx:03d} : {row.get('name')}")
            sheet = build_a4_page(cards)
            for c in cards:
                c.close()
            del cards
            gc.collect()

            # FIX 1: save sheet as JPEG (was PNG)
            page_path = os.path.join(tmp_dir, f"page_{p+1:03d}.jpg")
            _save_sheet_jpeg(sheet, page_path)
            sheet.close()
            del sheet
            gc.collect()

            page_files.append(page_path)
            print(f"   ~ page {p+1}/{n_pages} written "
                  f"({os.path.getsize(page_path)//1024} KB)")

        # FIX 2: compressed PDF from JPEG pages
        _save_pdf_from_jpegs(page_files, out_pdf)
        if os.path.exists(out_pdf):
            print(f"   ~ PDF compressed: "
                  f"{os.path.getsize(out_pdf)//1024} KB total")

    finally:
        for p in page_files:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    print(f"DONE  ->  {out_pdf}")
    return out_pdf


def generate(excel_path, template_pdf, out_pdf="ID_Cards_A4.pdf",
             sheet_name=0):
    """Convenience wrapper for CLI / Colab usage."""
    if str(excel_path).lower().endswith(".csv"):
        df = pd.read_csv(excel_path)
    else:
        df = pd.read_excel(excel_path, sheet_name=sheet_name,
                           engine="openpyxl")
    df = df.fillna("")

    df.columns = [
        col.strip().lower().replace(" ", "_").replace("-", "_")
        for col in df.columns
    ]

    rows = df.to_dict(orient="records")
    return generate_employee_pdf(rows, template_pdf, out_pdf)


# =====================================================================
# 12. CLI
# =====================================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel",    default="employees.xlsx")
    ap.add_argument("--template", default="Card 4 (1)_full_card.pdf")
    ap.add_argument("--out",      default="ID_Cards_A4.pdf")
    args = ap.parse_args()
    generate(args.excel, args.template, args.out)
