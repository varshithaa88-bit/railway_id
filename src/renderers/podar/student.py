import io
import fitz
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps
from src.config import PHOTO_EMBED_SCALE, DEFAULT_SESSION, TEMPLATE_CONFIGS
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    wrap_and_shrink_text, _centered_baseline_for_box
)
from src.utils.photo import (
    fetch_photo_bytes, prepare_photo_for_rect_cover, insert_image_safe
)
from src.utils.pdf import _PDF_SAVE_OPTS

# Podar card dimensions (86x54 mm landscape at 600 DPI - matching standalone script)
CARD_WIDTH_MM = 86
CARD_HEIGHT_MM = 54
DPI = 600

def mm_to_pixels(mm, dpi=DPI):
    return int(round(mm / 25.4 * dpi))

CARD_W_PX = mm_to_pixels(CARD_WIDTH_MM)    # 2032
CARD_H_PX = mm_to_pixels(CARD_HEIGHT_MM)   # 1276

# Calibrated field boxes for Podar templates (fractions of card dimensions - matching standalone)
PODAR_FIELD_BOXES = {
    "NAME": (0.259, 0.030, 1.0, 0.088),
    "LEVEL": (0.259, 0.104, 0.656, 0.162),
    "SEC": (0.666, 0.104, 1.0, 0.162),
    "ADDRESS": (0.259, 0.177, 1.0, 0.282),
    "MOBILE_FATHER": (0.259, 0.296, 1.0, 0.355),
    "MOBILE_MOTHER": (0.259, 0.370, 1.0, 0.428),
    "BLOOD_GROUP": (0.259, 0.445, 1.0, 0.502),
    "SHIFT": (0.259, 0.518, 1.0, 0.576),
    "MODE_OF_TRANSPORT": (0.259, 0.592, 1.0, 0.649),
    "BUS_NUMBER": (0.259, 0.666, 1.0, 0.724),
}

# Calibrated photo box for Podar templates (matching standalone)
PODAR_PHOTO_BOX = (0.046, 0.465, 0.209, 0.803)

def _get_podar_field_boxes():
    """Get calibrated field boxes in pixel coordinates."""
    return {
        k: (int(x1 * CARD_W_PX), int(y1 * CARD_H_PX), int(x2 * CARD_W_PX), int(y2 * CARD_H_PX))
        for k, (x1, y1, x2, y2) in PODAR_FIELD_BOXES.items()
    }

def _get_podar_photo_box():
    """Get calibrated photo box in pixel coordinates."""
    x1, y1, x2, y2 = PODAR_PHOTO_BOX
    return (int(x1 * CARD_W_PX), int(y1 * CARD_H_PX), int(x2 * CARD_W_PX), int(y2 * CARD_H_PX))

# Template cache to avoid re-detection
_template_cache = {}

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
    return value_boxes, photo

def get_template_geometry(template_path):
    """Get template geometry with caching to avoid re-detection."""
    if template_path in _template_cache:
        return _template_cache[template_path]
    
    img = _render_template_to_image(template_path)
    fields, photo = detect_template_geometry(img)
    _template_cache[template_path] = (fields, photo)
    return fields, photo

def _normalize_podar_class(class_name):
    """Normalize class name for template matching."""
    if not class_name:
        return "Play Group"  # Default
    
    normalized = str(class_name).strip().upper()
    
    variations = {
        "PLAY GROUP": "Play Group",
        "PLAYGROUP": "Play Group",
        "NURSERY": "Nursery",
        "JUNIOR KG": "Junior KG",
        "JUNIOR K.G.": "Junior KG",
        "JUNIOR": "Junior KG",
        "SENIOR KG": "Senior KG",
        "SENIOR K.G.": "Senior KG",
        "SENIOR": "Senior KG"
    }
    
    return variations.get(normalized, class_name)

def _get_podar_template_bytes(class_name):
    """Get the appropriate Podar template bytes based on class name."""
    normalized_class = _normalize_podar_class(class_name)
    podar_config = TEMPLATE_CONFIGS.get("podar", {})
    class_templates = podar_config.get("class_templates", {})
    
    template_path = class_templates.get(normalized_class)
    if not template_path:
        # Default to Play Group if class not found
        template_path = class_templates.get("Play Group")
    
    if template_path and template_path.exists():
        with open(template_path, "rb") as f:
            return f.read()
    
    return None

def _render_template_to_image(template_path, dpi=DPI):
    """Render PDF template to RGB image at exact card dimensions (matching standalone)."""
    doc = fitz.open(template_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    
    # Rotate portrait template to landscape if needed
    if img.height > img.width:
        img = img.rotate(90, expand=True)
    
    # Fit to exact physical pixel size
    img = ImageOps.fit(img, (CARD_W_PX, CARD_H_PX), Image.LANCZOS)
    return img

def _placeholder_photo(w, h):
    """Generate a placeholder photo (matching standalone)."""
    im = Image.new("RGB", (400, 500), (225, 228, 235))
    d = ImageDraw.Draw(im)
    d.ellipse([130, 90, 270, 250], fill=(190, 196, 208))     # head
    d.ellipse([70, 270, 330, 520], fill=(190, 196, 208))     # shoulders
    return im.resize((w, h), Image.LANCZOS)

def _draw_text_fitted(draw, text, box, max_size, min_size=14, wrap=False,
                     pad_x=18, pad_y=6, align="left"):
    """Fit text inside box with font shrinking and optional wrapping (matching standalone)."""
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
    
    # For ADDRESS field with single line, move it down slightly for better alignment
    # This ensures it starts at the same level as the label
    if wrap and len(lines) == 1:
        ty = y1 + pad_y + int(lh * 0.3)  # Move down by 30% of line height
    else:
        ty = y1 + pad_y + max(0, (avail_h - total_h) // 2)
    
    for l in lines:
        w_ = draw.textlength(l, font=font)
        tx = x1 + pad_x if align == "left" else x1 + pad_x + max(0, (avail_w - w_) // 2)
        draw.text((tx, ty), l, font=font, fill=(20, 20, 20))
        ty += lh + int(lh * 0.15)

def get_font(size_px):
    """Get font at specified size (matching standalone - Times New Roman)."""
    import os
    
    # Try to find Times New Roman on Windows (matching standalone script)
    candidates = [
        "C:/Windows/Fonts/times.ttf",
        "C:/Windows/Fonts/timesbd.ttf",  # Times New Roman Bold
        "C:/Windows/Fonts/timesi.ttf",   # Times New Roman Italic
        "C:/Windows/Fonts/timesbi.ttf",  # Times New Roman Bold Italic
        "C:/Windows/Fonts/Times New Roman.ttf",
        "C:/Windows/Fonts/Times New Roman Bold.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf",
        "/usr/share/fonts/truetype/latex-xft-fonts/Times New Roman.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size_px)
    
    return ImageFont.load_default()

def _render_podar_card_bytes(student: dict, tmpl_bytes: bytes, template_path=None):
    """Render a single Podar student card using PIL-based approach with OpenCV detection (matching standalone)."""
    
    # Get template geometry using OpenCV detection
    if template_path:
        fields, photo_box = get_template_geometry(template_path)
    else:
        # Fallback to calibrated boxes if no template path provided
        fields = _get_podar_field_boxes()
        photo_box = _get_podar_photo_box()
    
    # Map API fields to template fields (matching standalone)
    field_mapping = {
        "student_name": "NAME",
        "class": "LEVEL",
        "section": "SEC",
        "address": "ADDRESS",
        "mobile": "MOBILE_FATHER",  # API maps father_contact to mobile
        "father_contact": "MOBILE_FATHER",  # Also support direct father_contact
        "blood_group": "BLOOD_GROUP",
        "mode_of_transport": "MODE_OF_TRANSPORT",
    }
    
    # Load template PDF and render to image
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]
    pix = page.get_pixmap(dpi=DPI)
    template_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    
    # Rotate portrait template to landscape if needed
    if template_img.height > template_img.width:
        template_img = template_img.rotate(90, expand=True)
    
    # Fit to exact physical pixel size
    template_img = ImageOps.fit(template_img, (CARD_W_PX, CARD_H_PX), Image.LANCZOS)
    
    # Create card copy
    card = template_img.copy()
    
    # Photo placement
    px1, py1, px2, py2 = photo_box
    pw, ph = px2 - px1, py2 - py1
    photo_url = clean_card_value(student.get("photo_url", ""))
    
    if photo_url:
        try:
            photo_bytes = fetch_photo_bytes(photo_url)
            if photo_bytes:
                photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
                # Crop photo to fit box (matching standalone)
                photo = ImageOps.fit(photo, (pw, ph), Image.LANCZOS, centering=(0.5, 0.5))
                card.paste(photo, (px1, py1))
        except Exception as e:
            # Use placeholder on failure
            placeholder = _placeholder_photo(pw, ph)
            card.paste(placeholder, (px1, py1))
    else:
        placeholder = _placeholder_photo(pw, ph)
        card.paste(placeholder, (px1, py1))
    
    # Text overlay (matching standalone)
    draw = ImageDraw.Draw(card)
    for api_field, template_field in field_mapping.items():
        val = clean_card_value(student.get(api_field, ""))
        if not val:
            continue
        
        box = fields.get(template_field)
        if not box:
            continue
        
        bh = box[3] - box[1]
        max_size = int(bh * 0.62) if template_field != "ADDRESS" else int(bh * 0.30)
        _draw_text_fitted(draw, val, box, max_size=max_size, wrap=(template_field == "ADDRESS"))
    
    # Convert back to PDF (matching standalone approach)
    output = io.BytesIO()
    card.save(output, format="PDF", resolution=DPI)
    
    # Convert to proper PDF using fitz
    img_pdf = fitz.open("pdf", output.getvalue())
    final_output = io.BytesIO()
    img_pdf.save(final_output, **_PDF_SAVE_OPTS)
    img_pdf.close()
    
    return final_output.getvalue()

def render_podar_student_card(student: dict, class_name: str = None):
    """
    Render a Podar student card with class-specific template.
    
    Args:
        student: Student data dictionary
        class_name: Student's class (Play Group, Nursery, Junior KG, Senior KG)
                   If not provided, will use student['class']
    
    Returns:
        PDF bytes for the rendered card
    """
    if class_name is None:
        class_name = student.get("class", "Play Group")
    
    normalized_class = _normalize_podar_class(class_name)
    podar_config = TEMPLATE_CONFIGS.get("podar", {})
    class_templates = podar_config.get("class_templates", {})
    
    template_path = class_templates.get(normalized_class)
    if not template_path:
        # Default to Play Group if class not found
        template_path = class_templates.get("Play Group")
    
    if not template_path or not template_path.exists():
        return None
    
    tmpl_bytes = _get_podar_template_bytes(class_name)
    if not tmpl_bytes:
        return None
    
    return _render_podar_card_bytes(student, tmpl_bytes, template_path=str(template_path))
