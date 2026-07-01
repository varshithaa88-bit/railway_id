import io
import fitz
from src.config import PHOTO_EMBED_SCALE, DEFAULT_SESSION
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    wrap_and_shrink_text, _centered_baseline_for_box
)
from src.utils.photo import (
    fetch_photo_bytes, prepare_photo_for_rect_cover, insert_image_safe
)
from src.utils.pdf import _PDF_SAVE_OPTS

def _render_ab_ascent_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    def h(c): return ((c>>16)&0xFF)/255, ((c>>8)&0xFF)/255, (c&0xFF)/255
    NAVY    = h(0x224499)
    RED     = h(0xC83030)
    WHITE_C = (1.0, 1.0, 1.0)
    BLACK   = (0.0, 0.0, 0.0)

    redact_zones = [
        (109.15, 107.50, 148.0,  118.50),
        ( 25.07, 107.50,  50.0,  118.50),
        ( 17.73, 126.60, 140.0,  138.50),
        ( 26.46, 137.50,  58.0,  146.30),
        ( 73.90, 137.50,  84.0,  146.30),
        (100.07, 137.50, 115.0,  146.30),
        ( 60.74, 153.50, 150.0,  162.00),
        ( 60.74, 161.00, 150.0,  169.50),
        ( 60.74, 168.30, 150.0,  176.80),
        ( 60.74, 175.60, 150.0,  184.10),
        ( 60.74, 183.60, 150.0,  192.00),
        ( 60.74, 191.10, 150.0,  199.60),
        (  8.00, 203.60,  65.0,  211.60),
        (116.03,  84.50, 125.56,  94.00),
    ]
    for x0, y0, x1, y1 in redact_zones:
        page.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    PHOTO = (52.93, 63.01, 100.07, 116.96)
    AB_BORDER_COLOR = (0.08, 0.31, 0.86)
    AB_BORDER_WIDTH = 1.5
    AB_BORDER_HALF = AB_BORDER_WIDTH / 2.0

    _ab_erase_shape = page.new_shape()
    _ab_erase_shape.draw_rect(fitz.Rect(PHOTO[0] - 1.0, PHOTO[1] - 1.0,
                                         PHOTO[2] + 1.0, PHOTO[3] + 1.0))
    _ab_erase_shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
    _ab_erase_shape.commit(overlay=True)
    page.add_redact_annot(
        fitz.Rect(PHOTO[0] - 1.0, PHOTO[1] - 1.0, PHOTO[2] + 1.0, PHOTO[3] + 1.0),
        fill=None
    )
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    photo_insert_rect = fitz.Rect(
        PHOTO[0] + AB_BORDER_HALF,
        PHOTO[1] + AB_BORDER_HALF,
        PHOTO[2] - AB_BORDER_HALF,
        PHOTO[3] - AB_BORDER_HALF,
    )
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes:
        prepared = prepare_photo_for_rect_cover(
            photo_bytes,
            (photo_insert_rect.x0, photo_insert_rect.y0,
             photo_insert_rect.x1, photo_insert_rect.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, photo_insert_rect, prepared or photo_bytes)
    else:
        insert_image_safe(page, photo_insert_rect, photo_bytes)

    _ab_shape = page.new_shape()
    _ab_shape.draw_rect(fitz.Rect(*PHOTO))
    _ab_shape.finish(color=AB_BORDER_COLOR, fill=None,
                     width=AB_BORDER_WIDTH, closePath=True)
    _ab_shape.commit(overlay=True)

    def put(text, x0, y1_bbox, color, max_x, sz=6.0, align="left"):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        available_w = max_x - x0
        fs = _fit_size(bold_obj, val, available_w, sz, 3.0)
        tw = bold_obj.text_length(val, fontsize=fs)
        if align == "center":
            x = x0 + (available_w - tw) / 2.0
        else:
            x = x0
        baseline = y1_bbox - 0.22 * sz
        page.insert_text((x, baseline), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=color, overlay=True)

    put(student.get("session", "") or DEFAULT_SESSION,
        109.15, 117.44, NAVY, 148.0, sz=7.5)
    put(student.get("adm_no", ""),
        18.5, 117.44, NAVY, 50.0, sz=7.5)
    put(student.get("student_name", "").upper(),
        17.73, 137.63, RED, 140.0, sz=9.0)
    put(student.get("class", "").upper(),
        26.46, 145.43, NAVY, 58.0, sz=6.0)
    put(student.get("section", "").upper(),
        73.90, 145.43, NAVY, 84.0, sz=6.0)
    put(student.get("roll", ""),
        100.07, 145.43, NAVY, 115.0, sz=6.0)
    put(student.get("father_name", ""),
        60.74, 161.14, NAVY, 150.0, sz=6.0)
    put(student.get("mother_name", ""),
        60.74, 168.69, NAVY, 150.0, sz=6.0)
    put(student.get("dob", ""),
        60.74, 175.95, NAVY, 150.0, sz=6.0)
    put(student.get("mobile", ""),
        60.74, 198.76, NAVY, 150.0, sz=6.0)

    blood = clean_card_value(student.get("blood_group", "")).upper()
    if blood and any(c.isalpha() for c in blood):
        put(blood, 116.03, 93.34, WHITE_C, 125.56, sz=7.0, align="center")

    bus = clean_card_value(student.get("bus_route", ""))
    if bus:
        # Extract digits from the bus route string
        import re
        m = re.search(r"(BUS|VAN|ROUTE)?\s*(\d+)", bus, re.IGNORECASE)
        if m:
            label = m.group(1) or "BUS"
            num = m.group(2)
        else:
            label = "BUS"
            num = "".join(c for c in bus if c.isdigit()) or bus

        # Print the number inside the original box position (9.0, 203.5, 17.5, 211.5)
        box_w = 17.5 - 9.0
        fs_num = _fit_size(bold_obj, num, box_w - 1.0, 6.0, 3.5)
        tw_num = bold_obj.text_length(num, fontsize=fs_num)
        bx = 9.0 + (box_w - tw_num) / 2.0
        by = _centered_baseline_for_box(bold_obj, 203.5, 211.5, fs_num)
        page.insert_text((bx, by), num,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs_num, color=NAVY, overlay=True)

        # Print the label next to it starting at x=21.0
        label_text = f"{label.upper()} {num}"
        put(label_text, 21.0, 210.70, BLACK, 65.0, sz=5.5)

    addr = clean_card_value(student.get("address", ""))
    if addr:
        if "," in addr:
            addr1, addr2 = addr.split(",", 1)
            addr1 = addr1.strip() + ","
            addr2 = addr2.strip()
        else:
            words = addr.split()
            mid = max(1, len(words) // 2)
            addr1 = " ".join(words[:mid])
            addr2 = " ".join(words[mid:])
        put(addr1, 60.74, 183.22, NAVY, 150.0, sz=6.0)
        if addr2:
            put(addr2, 60.74, 191.21, NAVY, 150.0, sz=6.0)

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
