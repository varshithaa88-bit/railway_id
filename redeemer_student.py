# =====================================================================
#  MY REDEEMER MISSION SCHOOL  –  ID-CARD AUTOMATION  (Google Colab)
#  ------------------------------------------------------------------
#  FIXED VERSION (v3.1)  –  overlay-blend + correct coordinates
#                           + PRINTER-SAFE PDF EXPORT
#  ------------------------------------------------------------------
#   PRINTER FIXES (v3.1):
#     FIX 1  -  Card images are now embedded into the PDF as JPEG
#               streams (via PyMuPDF's `stream=jpeg_bytes`) instead
#               of raw PNG.  PNG-in-PDF is the #1 cause of
#               "File Error" on inexpensive office printers.
#     FIX 2  -  The final PDF is saved with `deflate=True`,
#               `deflate_images=True`, `deflate_fonts=True`,
#               `garbage=4`, and `clean=True` -> small, compact,
#               printer-friendly PDF.
#     FIX 3  -  Every PIL image is forced to flat RGB (no alpha,
#               no palette) before being handed to PyMuPDF, which
#               avoids transparency-flattening errors in the print
#               spooler.
#     FIX 4  -  Also writes ID_Cards_A4_safe.pdf using Pillow's
#               JPEG-in-PDF writer as an extra fall-back the user
#               can print if the PyMuPDF PDF still misbehaves on a
#               very old driver.
#
#   Original problems already fixed (v3):
#     1. DOB / F-H Name / Address values were sitting ON TOP of the
#        label colon ":" and a hard white rectangle was repainted
#        behind them.  Now we erase ONLY the placeholder letter
#        area (right of the ":") and rebuild the soft light-blue
#        gradient so the text blends seamlessly into the card.
#     2. NAME is now centred across the FULL blue banner.
#     3. All text coordinates re-extracted from the actual template.
#     4. Exact template colours used.
# =====================================================================
#
#  USAGE (Colab):
#    !pip -q install pymupdf pillow pandas openpyxl requests
#    !python ID_Card_Automation_Colab_v3.py
#
#  OUTPUT  (./output/) :
#    • ID_Cards_A4.pdf            – production A4 sheet (PyMuPDF JPEG-in-PDF)
#    • ID_Cards_A4_safe.pdf       – fallback PDF (Pillow JPEG-in-PDF)
#    • single_card_preview.jpg    – one finished card (visual check)
#    • single_card_debug.jpg      – same + red placeholder guides
#    • sample_employees.xlsx      – auto-generated demo data (if none)
# =====================================================================

import os, io, math, requests, random
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pandas as pd

# =====================================================================
#                      CONFIGURATION
# =====================================================================
TEMPLATE_PDF   = "template.pdf"
EMPLOYEES_XLSX = "sample_employees.xlsx"
OUT_DIR        = "output"
os.makedirs(OUT_DIR, exist_ok=True)

CARD_DPI       = 300
CARD_W_PT, CARD_H_PT = 153.0, 243.0
CARD_W_MM, CARD_H_MM = 54.0, 85.7

# JPEG quality used everywhere card images are exported.  92 keeps
# text crisp while keeping each card ~150-300 KB instead of 2-3 MB.
JPEG_QUALITY   = 92

# ---- A4 sheet layout ------------------------------------------------
A4_W_MM, A4_H_MM = 297.0, 210.0
COLS, ROWS       = 5, 2
GAP_X_MM, GAP_Y_MM = 4.0, 6.0
PAGE_MARGIN_MM   = 6.0

# ---------------------------------------------------------------------
#  PLACEHOLDER BOXES  (PDF points, from top-left of the page)
# ---------------------------------------------------------------------
NAME_BANNER    = (7.0, 146.8, 113.0, 158.3)
NAME_TEXT_COL  = (255, 255, 255)
NAME_SIZE_PT   = 8.5

DESIG_BOX      = (48.3, 158.8, 110.0, 165.2)
DESIG_COL      = (255, 255, 255)
DESIG_SIZE_PT  = 4.66

EMPID_BOX      = (111.6, 108.5, 138.0, 117.5)
EMPID_COL      = (31, 72, 255)
EMPID_SIZE_PT  = 7.0

VAL_COL        = (0, 0, 0)
VAL_SIZE_PT    = 7.0
DOB_BOX        = (54.0, 171.5, 145.0, 181.0)
FNAME_BOX      = (54.0, 181.0, 145.0, 190.5)
ADDR_BOX       = (54.0, 190.0, 145.0, 200.0)

PHOTO_BOX      = (54.6, 81.6, 98.6, 136.5)

BANNER_BLUE    = (35, 64, 200)
GRADIENT_LEFT  = (233, 249, 255)
GRADIENT_RIGHT = (246, 253, 254)


# =====================================================================
#                      HELPERS
# =====================================================================
def _find_font(bold=True, size=12):
    cand = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in cand:
        if os.path.isfile(p):
            try:    return ImageFont.truetype(p, size=size)
            except: pass
    return ImageFont.load_default()


def _load_image(src):
    if src is None or (isinstance(src, float) and math.isnan(src)): return None
    s = str(src).strip()
    if not s: return None
    try:
        if s.startswith(("http://","https://")):
            r = requests.get(s, timeout=20); r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        if os.path.isfile(s):
            return Image.open(s).convert("RGB")
    except Exception as e:
        print("  ! image load failed:", s, "->", e)
    return None


def _render_template(dpi=CARD_DPI):
    doc = fitz.open(TEMPLATE_PDF)
    pix = doc[0].get_pixmap(dpi=dpi, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()
    return img


def _pt_to_px(box_pt, dpi):
    s = dpi / 72.0
    x0,y0,x1,y1 = box_pt
    return (int(round(x0*s)), int(round(y0*s)),
            int(round(x1*s)), int(round(y1*s)))


def _sample_row_gradient(img, y_px, x0_px, x1_px, n_samples=5):
    W, H = img.size
    y_px = max(0, min(H-1, y_px))
    xs = [int(x0_px + (x1_px-x0_px)*i/(n_samples-1)) for i in range(n_samples)]
    xs = [max(0, min(W-1, x)) for x in xs]
    return [img.getpixel((x, y_px)) for x in xs]


def _paint_gradient(img, box_px, samples):
    x0,y0,x1,y1 = box_px
    w, h = x1-x0, y1-y0
    if w <= 0 or h <= 0: return
    grad = Image.new("RGB", (w, 1))
    n = len(samples)
    for x in range(w):
        t = x / max(1, w-1) * (n-1)
        i = int(t); f = t - i
        if i >= n-1:
            c = samples[-1]
        else:
            a, b = samples[i], samples[i+1]
            c = (int(a[0]+(b[0]-a[0])*f),
                 int(a[1]+(b[1]-a[1])*f),
                 int(a[2]+(b[2]-a[2])*f))
        grad.putpixel((x, 0), c)
    grad = grad.resize((w, h), Image.BILINEAR)

    mask = Image.new("L", (w, h), 255)
    md = ImageDraw.Draw(mask)
    md.rectangle([0,0,w-1,h-1], outline=0, width=1)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=2.5))
    img.paste(grad, (x0, y0), mask)


# ---------------------------------------------------------------------
#  Printer-safe image helpers
# ---------------------------------------------------------------------
def _to_printer_safe_rgb(img: Image.Image) -> Image.Image:
    """Flat RGB only - no alpha, no palette.  Cures spooler crashes."""
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _pil_to_jpeg_bytes(img: Image.Image, quality=JPEG_QUALITY) -> bytes:
    """
    FIX 1: convert a PIL image to a baseline JPEG byte string so that
    PyMuPDF embeds a DCT/JPEG stream into the PDF instead of a PNG/Flate
    one.  Baseline (non-progressive) JPEG is the most universally
    supported image format on printer RIPs.
    """
    safe = _to_printer_safe_rgb(img)
    buf = io.BytesIO()
    safe.save(
        buf, "JPEG",
        quality=quality,
        optimize=True,
        progressive=False,
        subsampling=1,
    )
    buf.seek(0)
    return buf.getvalue()


# =====================================================================
#                      RENDER ONE CARD
# =====================================================================
def render_card(person, dpi=CARD_DPI, debug=False, base_img=None):
    img  = (base_img or _render_template(dpi)).copy()
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # ---------- 1. Photo --------------------------------------------
    # v2.9 FULL-PHOTO FIT: scale-to-fit (contain) on a white pad so the
    # entire photo (head-to-toe) is visible — do NOT stretch or crop.
    px_photo = _pt_to_px(PHOTO_BOX, dpi)
    photo = _load_image(person.get("photo"))
    if photo is not None:
        pw = px_photo[2]-px_photo[0]
        ph = px_photo[3]-px_photo[1]
        iw, ih = photo.size
        ratio  = min(pw / max(1, iw), ph / max(1, ih))
        nw, nh = max(1, int(iw * ratio)), max(1, int(ih * ratio))
        fitted = photo.resize((nw, nh), Image.LANCZOS)
        pad    = Image.new("RGB", (pw, ph), (255, 255, 255))
        pad.paste(fitted, ((pw - nw) // 2, (ph - nh) // 2))
        img.paste(pad, (px_photo[0], px_photo[1]))

    # ---------- 2. Erase placeholders --------------------------------
    nb = _pt_to_px(NAME_BANNER, dpi)
    draw.rectangle(nb, fill=BANNER_BLUE)

    db = _pt_to_px(DESIG_BOX, dpi)
    draw.rectangle(db, fill=BANNER_BLUE)

    eb = _pt_to_px(EMPID_BOX, dpi)
    s_emp = img.getpixel((min(W-1, eb[2]+4), (eb[1]+eb[3])//2))
    draw.rectangle(eb, fill=s_emp)

    for box_pt in (DOB_BOX, FNAME_BOX, ADDR_BOX):
        box_px = _pt_to_px(box_pt, dpi)
        sample_y = (box_px[1] + box_px[3]) // 2
        samples = _sample_row_gradient(img, sample_y,
                                        box_px[0], box_px[2], n_samples=10)
        _paint_gradient(img, box_px, samples)

    # ---------- 3. Draw text ----------------------------------------
    def _draw_centered(text, box_px, size_pt, color):
        x0,y0,x1,y1 = box_px
        bw, bh = x1-x0, y1-y0
        size_px = max(6, int(round(size_pt * dpi / 72.0)))
        font = _find_font(bold=True, size=size_px)
        while size_px > 6:
            tw = draw.textlength(text, font=font)
            if tw <= bw - 2: break
            size_px -= 1
            font = _find_font(bold=True, size=size_px)
        tw = draw.textlength(text, font=font)
        asc, desc = font.getmetrics()
        th = asc + desc
        tx = x0 + (bw - tw)//2
        ty = y0 + (bh - th)//2
        draw.text((tx, ty), text, fill=color, font=font)

    def _draw_left(text, box_px, size_pt, color, pad_left=2):
        x0,y0,x1,y1 = box_px
        bw, bh = x1-x0, y1-y0
        size_px = max(6, int(round(size_pt * dpi / 72.0)))
        font = _find_font(bold=True, size=size_px)
        while size_px > 6:
            tw = draw.textlength(text, font=font)
            if tw <= bw - 2: break
            size_px -= 1
            font = _find_font(bold=True, size=size_px)
        asc, desc = font.getmetrics()
        th = asc + desc
        tx = x0 + pad_left
        ty = y0 + (bh - th)//2
        draw.text((tx, ty), text, fill=color, font=font)

    name = str(person.get("name","")).strip().upper()
    if name:
        _draw_centered(name, _pt_to_px(NAME_BANNER, dpi),
                       NAME_SIZE_PT, NAME_TEXT_COL)

    desig = str(person.get("designation","")).strip().upper()
    if desig:
        _draw_left(desig, _pt_to_px(DESIG_BOX, dpi),
                   DESIG_SIZE_PT, DESIG_COL, pad_left=1)

    emp = str(person.get("emp_id","")).strip()
    if emp:
        _draw_left(emp, _pt_to_px(EMPID_BOX, dpi),
                   EMPID_SIZE_PT, EMPID_COL, pad_left=2)

    _draw_left(str(person.get("dob","")),     _pt_to_px(DOB_BOX,   dpi),
               VAL_SIZE_PT, VAL_COL, pad_left=6)
    _draw_left(str(person.get("fname","")),   _pt_to_px(FNAME_BOX, dpi),
               VAL_SIZE_PT, VAL_COL, pad_left=6)
    _draw_left(str(person.get("address","")), _pt_to_px(ADDR_BOX,  dpi),
               VAL_SIZE_PT, VAL_COL, pad_left=6)

    # ---------- 4. Debug overlay -----------------------------------
    if debug:
        overlays = [
            ("NAME",    NAME_BANNER, (255,0,0)),
            ("DESIG",   DESIG_BOX,   (255,128,0)),
            ("EMPID",   EMPID_BOX,   (0,0,255)),
            ("DOB",     DOB_BOX,     (200,0,0)),
            ("FNAME",   FNAME_BOX,   (0,150,0)),
            ("ADDR",    ADDR_BOX,    (150,0,150)),
            ("PHOTO",   PHOTO_BOX,   (0,200,0)),
        ]
        for label, box_pt, col in overlays:
            b = _pt_to_px(box_pt, dpi)
            draw.rectangle(b, outline=col, width=2)
            draw.text((b[0]+2, b[1]-10), label, fill=col,
                      font=_find_font(bold=True, size=12))
    return img


# =====================================================================
#                      A4 SHEET COMPOSER
# =====================================================================
def _fit_card_size(pw, ph, cols, rows, gx, gy, margin,
                   aw=CARD_W_MM, ah=CARD_H_MM):
    avail_w = pw - 2*margin - (cols-1)*gx
    avail_h = ph - 2*margin - (rows-1)*gy
    cw_by_w = avail_w / cols
    ch_by_h = avail_h / rows
    if cw_by_w >= aw and ch_by_h >= ah: return aw, ah
    aspect = aw / ah
    cw = min(cw_by_w, ch_by_h * aspect)
    return cw, cw / aspect


def build_sheets(people, out_pdf, debug=False,
                 page_w_mm=A4_W_MM, page_h_mm=A4_H_MM,
                 cols=COLS, rows=ROWS):
    """
    Build the A4 production PDF.

    Each card image is embedded as a JPEG stream (FIX 1) and the PDF
    itself is saved with full deflate/garbage-collect/clean options
    (FIX 2) so that the resulting file is small AND printer-safe.

    Additionally writes a Pillow JPEG-in-PDF fallback at
    <out_pdf>_safe.pdf for stubborn printer drivers (FIX 4).
    """
    mm2pt = lambda v: v * 72.0 / 25.4
    pageW, pageH = mm2pt(page_w_mm), mm2pt(page_h_mm)

    cwmm, chmm = _fit_card_size(page_w_mm, page_h_mm, cols, rows,
                                GAP_X_MM, GAP_Y_MM, PAGE_MARGIN_MM)
    print(f"  card on sheet: {cwmm:.1f} × {chmm:.1f} mm  "
          f"(layout {cols}×{rows} on {page_w_mm}×{page_h_mm} mm)")

    cardW, cardH = mm2pt(cwmm), mm2pt(chmm)
    gapX, gapY   = mm2pt(GAP_X_MM), mm2pt(GAP_Y_MM)
    block_w = cols*cardW + (cols-1)*gapX
    block_h = rows*cardH + (rows-1)*gapY
    mx = (pageW - block_w)/2
    my = (pageH - block_h)/2

    base = _render_template(dpi=CARD_DPI)
    out  = fitz.open()
    per  = cols*rows
    npgs = max(1, math.ceil(len(people)/per))

    # We also keep a list of compiled JPEG page images so we can build
    # the Pillow fallback PDF (FIX 4) at the end without re-rendering.
    page_jpeg_imgs = []

    for p in range(npgs):
        page = out.new_page(width=pageW, height=pageH)
        page.draw_rect(page.rect, color=(1,1,1), fill=(1,1,1), width=0)

        # Compose a single full-page raster too (for the Pillow fallback)
        page_raster_dpi = CARD_DPI
        a4_w_px = int(round(page_w_mm * page_raster_dpi / 25.4))
        a4_h_px = int(round(page_h_mm * page_raster_dpi / 25.4))
        sheet_img = Image.new("RGB", (a4_w_px, a4_h_px), (255, 255, 255))

        for i in range(per):
            idx = p*per + i
            if idx >= len(people): break
            person = people[idx]
            r, c = divmod(i, cols)
            x = mx + c*(cardW+gapX)
            y = my + r*(cardH+gapY)
            card = render_card(person, dpi=CARD_DPI, debug=debug,
                               base_img=base)

            # ---- FIX 1: JPEG-encoded image stream into the PDF ----
            jpg_bytes = _pil_to_jpeg_bytes(card, quality=JPEG_QUALITY)
            page.insert_image(fitz.Rect(x, y, x+cardW, y+cardH),
                              stream=jpg_bytes,
                              keep_proportion=False)
            page.draw_rect(fitz.Rect(x, y, x+cardW, y+cardH),
                           color=(0.85,0.85,0.85), width=0.3)

            # ---- Also paste into the fallback raster sheet ----
            card_w_px = int(round(cwmm * page_raster_dpi / 25.4))
            card_h_px = int(round(chmm * page_raster_dpi / 25.4))
            x_px = int(round((x / pageW) * a4_w_px))
            y_px = int(round((y / pageH) * a4_h_px))
            card_resized = card.resize((card_w_px, card_h_px), Image.LANCZOS)
            sheet_img.paste(card_resized, (x_px, y_px))
            card.close()

        page_jpeg_imgs.append(sheet_img)

    # ---- FIX 2: save PyMuPDF PDF with full compression ------------
    out.save(
        out_pdf,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        garbage=4,
        clean=True,
    )
    out.close()
    print(f"  saved → {out_pdf}  ({os.path.getsize(out_pdf)//1024} KB)")

    # ---- FIX 4: also save a Pillow JPEG-in-PDF fallback -----------
    safe_pdf = os.path.splitext(out_pdf)[0] + "_safe.pdf"
    if page_jpeg_imgs:
        first = _to_printer_safe_rgb(page_jpeg_imgs[0])
        rest  = [_to_printer_safe_rgb(p) for p in page_jpeg_imgs[1:]]
        first.save(
            safe_pdf,
            "PDF",
            resolution=float(CARD_DPI),
            save_all=True,
            append_images=rest,
            producer="MyRedeemerMissionSchool-IDCardGenerator-v3.1",
            title="Employee ID Cards (printer-safe fallback)",
        )
        first.close()
        for r in rest:
            r.close()
        print(f"  saved → {safe_pdf}  "
              f"({os.path.getsize(safe_pdf)//1024} KB, printer-safe fallback)")


# =====================================================================
#                      SAMPLE EXCEL GENERATOR
# =====================================================================
def make_sample_excel(path=EMPLOYEES_XLSX, n=6):
    photos_m = [f"https://randomuser.me/api/portraits/men/{i}.jpg"
                for i in (11,33,55,15,27,41,63,77,82,9)]
    photos_w = [f"https://randomuser.me/api/portraits/women/{i}.jpg"
                for i in (22,44,66,8,16,28,32,46,54,60)]
    names_m  = ["Ravi Kumar","Amit Singh","Suresh Roy","Manoj Jha","Vivek Mishra"]
    names_w  = ["Anjali Mishra","Priya Sharma","Sneha Verma","Kavita Roy","Pooja Singh"]
    desigs   = ["PRINCIPAL","MATHS TEACHER","SCIENCE TEACHER","ENGLISH TEACHER",
                "HINDI TEACHER","SPORTS COACH","LIBRARIAN"]
    addrs    = ["Purnia, Bihar","Patna, Bihar","Banka, Bihar","Bhagalpur, Bihar",
                "Munger, Bihar","Shambhuganj, Banka, Bihar"]
    random.seed(7)
    rows = []
    for i in range(n):
        male = random.random() < 0.5
        nm = (names_m if male else names_w)[i % len(names_m if male else names_w)]
        rows.append(dict(
            emp_id      = 2027+i,
            name        = nm,
            designation = random.choice(desigs),
            dob         = f"{random.randint(1,28):02d}-"
                          f"{random.choice(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])}-"
                          f"{random.randint(1980,1998)}",
            fname       = f"Mr. {random.choice(['Suresh','Rakesh','Anil','Mohan','Dinesh'])} {nm.split()[-1]}",
            address     = random.choice(addrs),
            photo       = (photos_m if male else photos_w)[i % 10],
        ))
    pd.DataFrame(rows).to_excel(path, index=False)
    print("  sample Excel saved →", path, " rows =", n)
    return path


# =====================================================================
#                      MAIN
# =====================================================================
def main(n_people=6):
    if not os.path.isfile(EMPLOYEES_XLSX):
        make_sample_excel(EMPLOYEES_XLSX, n=n_people)
    df = pd.read_excel(EMPLOYEES_XLSX)
    people = df.to_dict(orient="records")
    print(f"Loaded {len(people)} employees from {EMPLOYEES_XLSX}")

    print("Rendering single-card previews ...")
    base = _render_template(dpi=CARD_DPI)

    # FIX 1: save previews as JPEG too (printer-friendly)
    prev = render_card(people[0], dpi=CARD_DPI, debug=False, base_img=base)
    _to_printer_safe_rgb(prev).save(
        f"{OUT_DIR}/single_card_preview.jpg",
        "JPEG", quality=JPEG_QUALITY, optimize=True,
        progressive=False, subsampling=1,
    )
    prev.close()

    dbg = render_card(people[0], dpi=CARD_DPI, debug=True, base_img=base)
    _to_printer_safe_rgb(dbg).save(
        f"{OUT_DIR}/single_card_debug.jpg",
        "JPEG", quality=JPEG_QUALITY, optimize=True,
        progressive=False, subsampling=1,
    )
    dbg.close()

    print("Building production A4 sheet ...")
    build_sheets(people, f"{OUT_DIR}/ID_Cards_A4.pdf", debug=False)

    print("\nDONE.  Files in", OUT_DIR)
    for f in sorted(os.listdir(OUT_DIR)):
        print(" ", f, os.path.getsize(os.path.join(OUT_DIR, f)), "bytes")


if __name__ == "__main__":
    main(n_people=6)
