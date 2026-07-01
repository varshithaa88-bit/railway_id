import io
import fitz
from src.config import PHOTO_EMBED_SCALE
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    _centered_baseline_for_box, wrap_and_shrink_text, _emp_value
)
from src.utils.photo import (
    fetch_photo_bytes, prepare_photo_for_rect_cover, insert_image_safe
)
from src.utils.pdf import _PDF_SAVE_OPTS

def _render_ab_ascent_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # Colours
    ROYAL_BLUE = (0x1E/255, 0x40/255, 0xAF/255)
    NAME_RED   = (0xE8/255, 0x3A/255, 0x2F/255)
    YELLOW_BG  = (0xFF/255, 0xD9/255, 0x11/255)
    WHITE_C    = (1.0, 1.0, 1.0)

    # Coords converted to points
    PLACEHOLDERS = {
        "validity":    (114.86, 101.55, 144.18, 110.21),
        "name":        ( 14.66, 131.15, 114.86, 141.98),
        "designation": ( 50.0, 141.26, 106.0, 149.92),
        "dob":         ( 60.61, 158.10, 147.61, 166.04),
        "fh_name":     ( 60.61, 165.08, 147.61, 173.02),
        "address":     ( 60.61, 172.50, 147.61, 187.00), 
        "mobile":      ( 60.61, 187.94, 105.08, 195.40),
        "photo":       ( 53.60,  66.50, 99.20, 119.50),
    }

    masks = {
        "name":        YELLOW_BG,
        "designation": YELLOW_BG,
        "dob":         WHITE_C,
        "fh_name":     WHITE_C,
        "address":     WHITE_C,
        "mobile":      WHITE_C,
        "validity":    WHITE_C,
    }
    for key, fill in masks.items():
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                       color=fill, fill=fill, width=0, overlay=True)

    PHOTO = PLACEHOLDERS["photo"]

    # Clear inner photo area
    page.draw_rect(
        fitz.Rect(PHOTO[0] + 0.5, PHOTO[1] + 0.5, PHOTO[2] - 0.5, PHOTO[3] - 0.5), 
        color=(1.0, 1.0, 1.0), 
        fill=(1.0, 1.0, 1.0), 
        width=0, 
        overlay=True
    )

    photo_inset_rect = fitz.Rect(
        PHOTO[0] + 0.5,
        PHOTO[1] + 0.5,
        PHOTO[2] - 0.5,
        PHOTO[3] - 0.5
    )
        
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes:
        prepared = prepare_photo_for_rect_cover(
            photo_bytes, 
            (photo_inset_rect.x0, photo_inset_rect.y0, photo_inset_rect.x1, photo_inset_rect.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, photo_inset_rect, prepared or photo_bytes)
    else:
        insert_image_safe(page, photo_inset_rect, photo_bytes)

    # Helper function
    def _put(text, key, color, *, size_pt=6.0, min_pt=3.5, align="left", upper=False,
             font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw, size_pt, min_pt) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw, fs)
        tw = map_w = fo.text_length(val, fontsize=fs)
        if align == "center":
            x = x0 + (bw - tw) / 2.0
        elif align == "right":
            x = x1 - tw
        else:
            x = x0
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _put_wrapped_fixed(text, key, color, *, size_pt=6.0, max_lines=2, line_gap=1.15):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        x0, y0, x1, y1 = PLACEHOLDERS[key]
        bw = x1 - x0
        lines, target_fs = wrap_and_shrink_text(bold_obj, val, bw, max_lines, base_size=size_pt)
        if not lines:
            return
        asc = getattr(bold_obj, "ascender", 0.9)
        step = target_fs * line_gap
        baseline = y0 + target_fs * asc + 1.5
        for line in lines:
            if baseline - target_fs * abs(bold_obj.descender) > y1:
                break
            page.insert_text((x0, baseline), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=color, overlay=True)
            baseline += step

    _put(_emp_value(student, "employee_name", "student_name", upper=True),
         "name", NAME_RED, size_pt=7.5, min_pt=5.0, align="center")

    _put(_emp_value(student, "designation"),
         "designation", ROYAL_BLUE,
         size_pt=6.0, min_pt=4.0, align="left")

    _put(_emp_value(student, "dob"),
         "dob", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)
    _put(_emp_value(student, "father_name", "fh_name"),
         "fh_name", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)
    _put(_emp_value(student, "mobile", "contact_no"),
         "mobile", ROYAL_BLUE, size_pt=6.0, min_pt=6.0, align="left", shrink=False)

    _put_wrapped_fixed(_emp_value(student, "address"),
                       "address", ROYAL_BLUE,
                       size_pt=6.0, max_lines=2, line_gap=1.0)

    validity = _emp_value(student, "validity") or "2026-27"
    v_obj = anton_obj or bold_obj
    v_fn  = anton_fn or bold_fn
    v_nm  = fn_anton or fn_bold
    _put(validity, "validity", ROYAL_BLUE, size_pt=7.0, min_pt=4.0,
         align="center", font_obj=v_obj, font_fn=v_fn, font_name=v_nm)

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
