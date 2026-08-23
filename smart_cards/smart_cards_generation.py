# =============================================================================
# SMART CARD GENERATOR - Google Colab / standalone
# 86 x 54 mm landscape cards, 3x3 on A4 LANDSCAPE, 9 cards/page, 600 DPI
# =============================================================================
# SECTION 1 - INSTALL DEPENDENCIES (run once in Colab)
# !pip install -q pymupdf pillow opencv-python-headless reportlab pandas openpyxl requests
# !apt-get install -y -qq fonts-liberation >/dev/null 2>&1

# SECTION 2 - IMPORTS
import os, io, json, math, hashlib, unicodedata
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
import cv2
import pandas as pd
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm as RL_MM
from reportlab.lib.utils import ImageReader

try:
    from google.colab import files as colab_files  # type: ignore
    IN_COLAB = True
except Exception:
    IN_COLAB = False

# SECTION 3 - CONFIGURATION
CARD_WIDTH_MM   = 86
CARD_HEIGHT_MM  = 54
PAGE_WIDTH_MM   = 297          # A4 landscape
PAGE_HEIGHT_MM  = 210
COLS, ROWS      = 3, 3
CARDS_PER_PAGE  = COLS * ROWS
HORIZONTAL_GAP_MM = 10.0       # 3*86 + 2*10 = 278 -> side margins (297-278)/2 = 9.5 mm
VERTICAL_GAP_MM   = 14.0       # 3*54 + 2*14 = 190 -> top/bottom margins (210-190)/2 = 10 mm
DPI = 600
PHOTO_CROP_POSITION = "center"
FIELD_PADDING_X_PX = 18        # at 600 dpi
FIELD_PADDING_Y_PX = 6
PHOTO_CACHE_DIR = "photo_cache"
CONFIG_PATH     = "template_config.json"
OUTPUT_PDF      = "smart_cards_output.pdf"
TEST_MODE = True               # False for real Excel data
DEBUG     = True

REQUIRED_COLUMNS = ["NAME","LEVEL","SEC","ADDRESS","MOBILE_FATHER","MOBILE_MOTHER",
                    "BLOOD_GROUP","SHIFT","MODE_OF_TRANSPORT","BUS_NUMBER","PHOTO_URL"]
EXCEL_TO_FIELD = {"NAME":"NAME","LEVEL":"LEVEL","SEC":"SEC","ADDRESS":"ADDRESS",
    "MOBILE_FATHER":"MOBILE_FATHER","MOBILE_MOTHER":"MOBILE_MOTHER",
    "BLOOD_GROUP":"BLOOD_GROUP","SHIFT":"SHIFT",
    "MODE_OF_TRANSPORT":"MODE_OF_TRANSPORT","BUS_NUMBER":"BUS_NUMBER"}
FIELD_ORDER = ["NAME","LEVEL","SEC","ADDRESS","MOBILE_FATHER","MOBILE_MOTHER",
               "BLOOD_GROUP","SHIFT","MODE_OF_TRANSPORT","BUS_NUMBER"]

# SECTION 21 - UNIT CONVERSION
def mm_to_points(mm):  return mm * 72.0 / 25.4
def mm_to_pixels(mm, dpi=DPI): return int(round(mm / 25.4 * dpi))
def pixels_to_mm(px, dpi=DPI): return px * 25.4 / dpi

CARD_W_PX = mm_to_pixels(CARD_WIDTH_MM)    # 2032
CARD_H_PX = mm_to_pixels(CARD_HEIGHT_MM)   # 1276

# SECTION 6 - FONTS
def find_font():
    candidates = [
        "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf",
        "/usr/share/fonts/truetype/latex-xft-fonts/Times New Roman.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

FONT_PATH = find_font()
def get_font(size_px):
    if FONT_PATH:
        return ImageFont.truetype(FONT_PATH, size_px)
    return ImageFont.load_default()

# SECTION 7/8/9 - TEMPLATE ANALYSIS: FIELD + PHOTO BOX DETECTION
def render_template(template_path, dpi=DPI):
    """Render PDF template (or load image template) and return an RGB image
    at exactly CARD_W_PX x CARD_H_PX (86x54 mm landscape)."""
    if template_path.lower().endswith(".pdf"):
        doc = fitz.open(template_path)
        page = doc[0]
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
    else:
        img = Image.open(template_path).convert("RGB")
    # Orientation: final card is landscape 86x54. Rotate portrait template 90 deg.
    if img.height > img.width:
        img = img.rotate(90, expand=True)
    # Fit to exact physical pixel size WITHOUT distorting aspect:
    # template is 85.68x53.97 mm (aspect 1.5872) == 86/54 (1.5926) within 0.4%.
    img = ImageOps.fit(img, (CARD_W_PX, CARD_H_PX), Image.LANCZOS)
    return img

def _detect_white_boxes(img):
    """Detect near-white rectangular input areas (OpenCV threshold + contours)."""
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    _, th = cv2.threshold(arr, 232, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > CARD_W_PX*0.20 and CARD_H_PX*0.03 < h < CARD_H_PX*0.14 and x > CARD_W_PX*0.20:
            boxes.append((x, y, x+w, y+h))
    # de-duplicate nested boxes
    boxes = sorted(set(boxes), key=lambda b: (b[1], b[0]))
    return boxes

def _label_end_x(gray, box, dark_thresh=130, gap_px=28):
    """Inside a white field box, find where the printed label (e.g. 'NAME :')
    ends by scanning for dark text columns from the left edge."""
    x1, y1, x2, y2 = box
    band = gray[y1+4:y2-4, x1:x1 + (x2-x1)//2]
    if band.size == 0:
        return x1
    dark = (band < dark_thresh).any(axis=0)
    last, run = 0, 0
    for i, d in enumerate(dark):
        if d:
            last, run = i, 0
        else:
            run += 1
            if run > gap_px and last > 0:
                break
    return x1 + last + 14

def detect_template_geometry(img):
    """Map detected white rectangles to the 10 named fields + photo box."""
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    boxes = _detect_white_boxes(img)
    # group into rows by y
    rows = []
    for b in boxes:
        placed = False
        for r in rows:
            if abs(r[0][1] - b[1]) < 20:
                r.append(b); placed = True; break
        if not placed:
            rows.append([b])
    rows = [sorted(r, key=lambda b: b[0]) for r in sorted(rows, key=lambda r: r[0][1])]

    fields = {}
    ok = len(rows) >= 8
    if ok:
        try:
            fields["NAME"]    = rows[0][0]
            fields["LEVEL"]   = rows[1][0]
            fields["SEC"]     = rows[1][1]
            fields["ADDRESS"] = rows[2][0]
            rest = [rows[i][0] for i in range(3, 9)]
            for name, b in zip(["MOBILE_FATHER","MOBILE_MOTHER","BLOOD_GROUP",
                                "SHIFT","MODE_OF_TRANSPORT","BUS_NUMBER"], rest):
                fields[name] = b
        except IndexError:
            ok = False
    if not ok:
        # Fallback: calibrated fractions of card W/H for the Podar Prep template
        f = {"NAME":(.259,.030,1,.088),"LEVEL":(.259,.104,.656,.162),"SEC":(.666,.104,1,.162),
             "ADDRESS":(.259,.177,1,.282),"MOBILE_FATHER":(.259,.296,1,.355),
             "MOBILE_MOTHER":(.259,.370,1,.428),"BLOOD_GROUP":(.259,.445,1,.502),
             "SHIFT":(.259,.518,1,.576),"MODE_OF_TRANSPORT":(.259,.592,1,.649),
             "BUS_NUMBER":(.259,.666,1,.724)}
        fields = {k:(int(x1*CARD_W_PX),int(y1*CARD_H_PX),int(x2*CARD_W_PX),int(y2*CARD_H_PX))
                  for k,(x1,y1,x2,y2) in f.items()}
        print("WARNING: automatic field detection uncertain - using calibrated fallback boxes.")

    # value areas = white box minus printed label
    value_boxes = {}
    for k, b in fields.items():
        lx = _label_end_x(gray, b)
        value_boxes[k] = (lx, b[1], b[2], b[3])

    # Photo box: largest non-background block in the left column, middle band
    arr = np.array(img)
    bg = np.median(arr.reshape(-1,3), axis=0)
    diff = np.abs(arr.astype(int) - bg).sum(axis=2)
    mask = (diff > 60).astype(np.uint8)*255
    mask[:int(CARD_H_PX*0.42), :] = 0
    mask[int(CARD_H_PX*0.86):, :] = 0
    mask[:, int(CARD_W_PX*0.25):] = 0
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    photo = None
    if contours:
        c = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(c)
        if w > CARD_W_PX*0.08 and h > CARD_H_PX*0.20:
            photo = (x, y, x+w, y+h)
    if photo is None:  # calibrated fallback
        photo = (int(.046*CARD_W_PX), int(.465*CARD_H_PX), int(.209*CARD_W_PX), int(.803*CARD_H_PX))
        print("WARNING: photo box auto-detection failed - using calibrated fallback.")
    return value_boxes, photo

def calibrate_template(template_path, save=True):
    """One-time calibration. Detects boxes, saves template_config.json.
    To adjust manually in Colab: display the debug overlay (DEBUG=True),
    edit the printed FIELD_BOXES dict, and re-save."""
    img = render_template(template_path)
    fields, photo = detect_template_geometry(img)
    cfg = {"CARD_W_PX": CARD_W_PX, "CARD_H_PX": CARD_H_PX, "DPI": DPI,
           "FIELD_BOXES": {k: list(v) for k, v in fields.items()},
           "PHOTO_BOX": list(photo)}
    if save:
        with open(CONFIG_PATH, "w") as fp:
            json.dump(cfg, fp, indent=2)
        print(f"Calibration saved -> {CONFIG_PATH}")
    return img, fields, photo

def load_calibration(template_path):
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fp:
            cfg = json.load(fp)
        img = render_template(template_path)
        fields = {k: tuple(v) for k, v in cfg["FIELD_BOXES"].items()}
        return img, fields, tuple(cfg["PHOTO_BOX"])
    return calibrate_template(template_path)

# SECTION 10 - EXCEL VALIDATION
def read_and_validate_excel(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"ERROR: Excel file not found: {path}")
    df = pd.read_excel(path, dtype=str)          # keep phone numbers as strings
    df.columns = [str(c).strip().upper() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"ERROR: Excel column(s) missing: {', '.join(missing)}")
    df = df[REQUIRED_COLUMNS]
    for c in df.columns:
        df[c] = df[c].apply(lambda v: "" if pd.isna(v) else unicodedata.normalize("NFC", str(v)).strip())
        # strip trailing .0 that Excel adds to numeric-looking cells
        df[c] = df[c].str.replace(r"^(\d+)\.0$", r"\1", regex=True)
    df = df.dropna(how="all")
    print(f"Excel OK: {len(df)} students, all {len(REQUIRED_COLUMNS)} required columns present.")
    return df

# SECTION 11/12 - PHOTO DOWNLOAD, CACHE, PROCESSING
os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
PLACEHOLDER = None

def _placeholder_photo(w, h):
    global PLACEHOLDER
    if PLACEHOLDER is None:
        im = Image.new("RGB", (400, 500), (225, 228, 235))
        d = ImageDraw.Draw(im)
        d.ellipse([130, 90, 270, 250], fill=(190, 196, 208))     # head
        d.ellipse([70, 270, 330, 520], fill=(190, 196, 208))     # shoulders
        PLACEHOLDER = im
    return PLACEHOLDER.resize((w, h), Image.LANCZOS)

def fetch_photo(url, student_name):
    """Download (or read local) photo; validate it is a real image. Never reuse
    another student's photo; on failure return None."""
    if not url:
        print(f"WARNING: no PHOTO_URL for {student_name}")
        return None
    key = hashlib.md5(url.encode()).hexdigest()
    for ext in (".jpg", ".png", ".jpeg"):
        cached = os.path.join(PHOTO_CACHE_DIR, key + ext)
        if os.path.exists(cached):
            try:
                im = Image.open(cached); im.verify()
                return Image.open(cached).convert("RGB")
            except Exception:
                os.remove(cached)
    try:
        if os.path.exists(url):                      # local path support (testing)
            im = Image.open(url); im.verify()
            im = Image.open(url).convert("RGB")
        else:
            r = requests.get(url, timeout=30, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 smart-card-bot"})
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content)); im.verify()
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
        ext = ".png" if im.format == "PNG" else ".jpg"
        im.save(os.path.join(PHOTO_CACHE_DIR, key + ext))
        return im
    except Exception as e:
        print(f"WARNING: Photo download failed for {student_name} ({e})")
        return None

def crop_photo_to_box(im, box_w, box_h, position=PHOTO_CROP_POSITION):
    """Aspect-ratio-preserving resize + center crop -> exact box size. No distortion."""
    centering = {"center": (0.5, 0.5), "top": (0.5, 0.0), "bottom": (0.5, 1.0)}.get(position, (0.5, 0.5))
    return ImageOps.fit(im.convert("RGB"), (box_w, box_h), Image.LANCZOS, centering=centering)

# SECTION 13 - TEXT FITTING
# SECTION 13 - TEXT FITTING (Updated)
# SECTION 13 - TEXT FITTING (Updated)
# SECTION 13 - TEXT FITTING (Updated)
# SECTION 13 - TEXT FITTING (Updated)
# SECTION 13 - TEXT FITTING (Updated)
# SECTION 13 - TEXT FITTING (UPDATED)
def draw_text_fitted(draw, text, box, max_size, min_size=14, wrap=False,
                     pad_x=FIELD_PADDING_X_PX, pad_y=FIELD_PADDING_Y_PX, align="left"):
    """Fit text inside box: shrink font as needed, optional 2-line wrap (ADDRESS),
    vertical alignment tuned for single-line vs multi-line fields."""
    if not text:
        return
    x1, y1, x2, y2 = box
    avail_w = (x2 - x1) - 2 * pad_x
    avail_h = (y2 - y1) - 2 * pad_y

    def wrap_lines(font, s):
        words, lines, cur = s.split(), [], ""
        for w_ in words:
            trial = (cur + " " + w_).strip()
            if draw.textlength(trial, font=font) <= avail_w:
                cur = trial
            else:
                if cur: lines.append(cur)
                cur = w_
        if cur: lines.append(cur)
        return lines

    size = max_size
    while size >= min_size:
        font = get_font(size)
        lines = wrap_lines(font, text) if wrap else [text]
        asc, desc = font.getmetrics()
        lh = asc + desc
        total_h = lh * len(lines) + (len(lines) - 1) * int(lh * 0.15)
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        max_lines = 3 if wrap else 1
        if widest <= avail_w and total_h <= avail_h and len(lines) <= max_lines:
            break
        if not wrap:
            if widest <= avail_w and total_h <= avail_h:
                break
        size -= 2

    font = get_font(max(size, min_size))
    lines = wrap_lines(font, text) if wrap else [text]
    asc, desc = font.getmetrics()
    lh = asc + desc
    total_h = lh * len(lines) + (len(lines) - 1) * int(lh * 0.15)

    # Adjust vertical starting position based on field type and line count
    if wrap:
        if len(lines) == 1:
            # Move single-line address slightly downward to match the 'ADDRESS :' baseline
            ty = y1 + pad_y + int(lh * 0.25)
        else:
            # Start multi-line address directly at top padding
            ty = y1 + pad_y
    else:
        # Standard fields remain vertically centered in their boxes
        ty = y1 + pad_y + max(0, (avail_h - total_h) // 2)

    for l in lines:
        w_ = draw.textlength(l, font=font)
        tx = x1 + pad_x if align == "left" else x1 + pad_x + max(0, (avail_w - w_) // 2)
        draw.text((tx, ty), l, font=font, fill=(20, 20, 20))
        ty += lh + int(lh * 0.15)

# SECTION 14 - CARD GENERATION (fresh template per student)
def generate_card(template_img, fields, photo_box, student, debug=False):
    card = template_img.copy()                    # ALWAYS a fresh blank template
    # photo
    px1, py1, px2, py2 = [int(v) for v in photo_box]
    pw, ph = px2-px1, py2-py1
    photo = fetch_photo(student.get("PHOTO_URL", ""), student.get("NAME", "?"))
    if photo is None:
        fitted = _placeholder_photo(pw, ph)       # configurable placeholder
    else:
        fitted = crop_photo_to_box(photo, pw, ph)
    card.paste(fitted, (px1, py1))
    # text
    draw = ImageDraw.Draw(card)
    for excel_col, field in EXCEL_TO_FIELD.items():
        val = student.get(excel_col, "")
        if not val:
            if excel_col == "BLOOD_GROUP":
                print(f"WARNING: Student {student.get('NAME','?')} has no blood group.")
            continue
        box = fields[field]
        bh = box[3] - box[1]
        max_size = int(bh * 0.62) if field != "ADDRESS" else int(bh * 0.30)
        draw_text_fitted(draw, val, box, max_size=max_size, wrap=(field == "ADDRESS"))
    if debug:
        d = ImageDraw.Draw(card)
        for k, b in fields.items():
            d.rectangle(b, outline=(255, 0, 0), width=3)
        d.rectangle((px1, py1, px2, py2), outline=(0, 90, 255), width=4)
    return card

# SECTION 15/16 - 3x3 A4 LANDSCAPE LAYOUT + PDF
def grid_geometry():
    total_w = COLS*CARD_WIDTH_MM + (COLS-1)*HORIZONTAL_GAP_MM
    total_h = ROWS*CARD_HEIGHT_MM + (ROWS-1)*VERTICAL_GAP_MM
    assert total_w <= PAGE_WIDTH_MM and total_h <= PAGE_HEIGHT_MM, "grid does not fit page"
    mx = (PAGE_WIDTH_MM  - total_w)/2      # 9.5 mm
    my = (PAGE_HEIGHT_MM - total_h)/2      # 10.0 mm
    pos = []
    for r in range(ROWS):
        for c in range(COLS):
            pos.append((mx + c*(CARD_WIDTH_MM+HORIZONTAL_GAP_MM),
                        my + r*(CARD_HEIGHT_MM+VERTICAL_GAP_MM)))
    return pos, mx, my

def build_pdf(cards, out_path=OUTPUT_PDF):
    """cards: list of PIL images at 600 dpi. A4 landscape, ReportLab origin =
    bottom-left -> convert top-left grid y to PDF y."""
    page_w_pt, page_h_pt = mm_to_points(PAGE_WIDTH_MM), mm_to_points(PAGE_HEIGHT_MM)
    cw_pt, ch_pt = mm_to_points(CARD_WIDTH_MM), mm_to_points(CARD_HEIGHT_MM)
    positions, _, _ = grid_geometry()
    c = rl_canvas.Canvas(out_path, pagesize=(page_w_pt, page_h_pt))
    for i, card in enumerate(cards):
        slot = i % CARDS_PER_PAGE
        gx_mm, gy_mm = positions[slot]
        x_pt = mm_to_points(gx_mm)
        y_pt = page_h_pt - mm_to_points(gy_mm) - ch_pt     # coordinate conversion
        buf = io.BytesIO()
        card.save(buf, format="PNG", dpi=(DPI, DPI))
        buf.seek(0)
        c.drawImage(ImageReader(buf), x_pt, y_pt, width=cw_pt, height=ch_pt,
                    preserveAspectRatio=False, mask=None)  # exact 86x54 mm box
        if slot == CARDS_PER_PAGE - 1:
            c.showPage()
    if len(cards) % CARDS_PER_PAGE != 0:
        c.showPage()
    c.save()
    return out_path

# SECTION 17 - PDF VALIDATION
def validate_pdf(path, expected_cards):
    res = {}
    doc = fitz.open(path)
    res["readable"] = True
    res["page_count_ok"] = (doc.page_count == math.ceil(expected_cards/CARDS_PER_PAGE))
    exp_w, exp_h = mm_to_points(PAGE_WIDTH_MM), mm_to_points(PAGE_HEIGHT_MM)
    res["page_size_ok"] = all(abs(p.rect.width-exp_w) < 1 and abs(p.rect.height-exp_h) < 1
                              for p in doc)
    cw_pt, ch_pt = mm_to_points(CARD_WIDTH_MM), mm_to_points(CARD_HEIGHT_MM)
    cards_ok, layout_ok, overlap_ok = True, True, True
    for pi, page in enumerate(doc):
        infos = page.get_image_info()
        rects = [fitz.Rect(i["bbox"]) for i in infos]
        full = pi < doc.page_count - 1
        want = CARDS_PER_PAGE if full else expected_cards - CARDS_PER_PAGE*(doc.page_count-1)
        if len(rects) != want:
            layout_ok = False
        for r in rects:
            if abs(r.width-cw_pt) > 1 or abs(r.height-ch_pt) > 1:
                cards_ok = False
        for a in range(len(rects)):
            for b in range(a+1, len(rects)):
                inter = rects[a] & rects[b]
                if not inter.is_empty and inter.get_area() > 1:
                    overlap_ok = False
    res["card_size_ok"] = cards_ok
    res["grid_ok"] = layout_ok
    res["no_overlap_ok"] = overlap_ok
    doc.close()
    return res

# SECTION 27 - SYNTHETIC TEST DATA
def make_test_excel(path="students_test.xlsx", n=11, local_photos=True):
    os.makedirs("test_photos", exist_ok=True)
    rows = []
    names = ["Rahul Kumar","Ananya Sharma","Vihaan Patil","Diya Reddy","Arjun Gowda",
             "Meera Nair","Kabir Singh","Sara Khan","Aditya Rao","Isha Verma","Rohan Desai"]
    for i in range(n):
        # synthetic portrait photo (offline-safe)
        im = Image.new("RGB", (600, 750), (200+((i*13)%40), 170, 210))
        d = ImageDraw.Draw(im)
        d.ellipse([220, 130, 380, 310], fill=(240, 205, 175))         # head
        d.ellipse([255, 195, 275, 215], fill=0); d.ellipse([325, 195, 345, 215], fill=0)
        d.arc([270, 235, 330, 275], 0, 180, fill=(120, 60, 60), width=4)
        d.rectangle([180, 330, 420, 750], fill=(60+((i*29)%120), 90, 140))  # body
        p = os.path.abspath(f"test_photos/s{i}.jpg")
        im.save(p, quality=92)
        rows.append({"NAME": names[i % len(names)], "LEVEL": str(1+i % 8),
            "SEC": "ABC"[i % 3],
            "ADDRESS": ["Venkateshwaranagara, Kadur",
                        "Behind Unilet, Venkateshwaranagara, Kadur - 577548, Chikkamagaluru, Karnataka"][i % 2],
            "MOBILE_FATHER": f"09876543{i:02d}", "MOBILE_MOTHER": f"09988776{i:02d}",
            "BLOOD_GROUP": ["B+","O+","A-","AB+"][i % 4],
            "SHIFT": ["Morning","Afternoon"][i % 2],
            "MODE_OF_TRANSPORT": ["Bus","Van","Self"][i % 3],
            "BUS_NUMBER": str(1+i % 15), "PHOTO_URL": p if local_photos else ""})
    pd.DataFrame(rows)[REQUIRED_COLUMNS].to_excel(path, index=False)
    return path

# MAIN PIPELINE
def run(template_path, excel_path, out_pdf=OUTPUT_PDF, debug=DEBUG):
    template_img, fields, photo_box = load_calibration(template_path)
    df = read_and_validate_excel(excel_path)
    cards, photo_fail = [], 0
    for idx, row in df.iterrows():
        student = {c: row[c] for c in REQUIRED_COLUMNS}
        if not student.get("BLOOD_GROUP"):
            print(f"WARNING: Student row {idx+2} has no blood group.")
        card = generate_card(template_img, fields, photo_box, student,
                             debug=(debug and idx == 0))
        if debug and idx == 0:
            card.save("debug_first_card.png")
            plain = generate_card(template_img, fields, photo_box, student, debug=False)
            plain.save("preview_first_card.png")
        cards.append(card if not debug else generate_card(template_img, fields, photo_box, student, debug=False))
    build_pdf(cards, out_pdf)
    val = validate_pdf(out_pdf, len(cards))
    # preview page 1
    doc = fitz.open(out_pdf)
    doc[0].get_pixmap(dpi=110).save("preview_page1.png")
    n_pages = doc.page_count
    doc.close()
    p = lambda b: "PASS" if b else "FAIL"
    print("="*40)
    print("SMART CARD GENERATION REPORT")
    print("="*40)
    print(f"Students found: {len(df)}")
    print(f"Cards generated: {len(cards)}")
    print(f"Pages generated: {n_pages}")
    print(f"Cards per full page: {CARDS_PER_PAGE}")
    print(f"Cards on final page: {len(cards) - CARDS_PER_PAGE*(n_pages-1)}")
    print(f"Card size: {CARD_WIDTH_MM} x {CARD_HEIGHT_MM} mm")
    print("Page: A4 Landscape\nGrid: 3 x 3")
    print(f"Target quality: {DPI} DPI")
    print(f"Font selected: {os.path.basename(FONT_PATH) if FONT_PATH else 'default'}")
    print(f"Photos cached in: {PHOTO_CACHE_DIR}/")
    print(f"PDF readable: {p(val['readable'])}")
    print(f"Page dimensions: {p(val['page_size_ok'])}")
    print(f"Page count: {p(val['page_count_ok'])}")
    print(f"Card dimensions: {p(val['card_size_ok'])}")
    print(f"Grid layout: {p(val['grid_ok'])}")
    print(f"Card overlap: {p(val['no_overlap_ok'])}")
    print("="*40)
    if IN_COLAB:
        colab_files.download(out_pdf)
    return val

if __name__ == "__main__":
    if TEST_MODE:
        # SECTION 19 - AUTOMATED SELF-TEST (11 synthetic students -> 2 pages)
        img, fields, photo_box = calibrate_template("template.pdf")
        print("Detected FIELD_BOXES:")
        for k, v in fields.items():
            print(f"  {k:18s} {v}")
        print("  PHOTO_BOX         ", photo_box)
        xlsx = make_test_excel()
        result = run("template.pdf", xlsx)
        assert all(result.values()), f"SELF-TEST FAILED: {result}"
        print("SELF-TEST: ALL CHECKS PASSED (11 students -> 9 + 2 cards on 2 pages)")
    else:
        # Real run (Colab): upload template + Excel when prompted
        if IN_COLAB:
            print("Upload the BLANK smart-card template (PDF or image):")
            up = colab_files.upload(); template = next(iter(up))
            print("Upload the student Excel file:")
            up = colab_files.upload(); excel = next(iter(up))
        else:
            template, excel = "template.pdf", "students.xlsx"
        run(template, excel)
