import io
import fitz
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    _centered_baseline_for_box, wrap_and_shrink_text, _emp_value
)
from src.utils.photo import (
    fetch_photo_bytes, prepare_photo_for_rect_cover, insert_image_safe
)
from src.utils.pdf import _PDF_SAVE_OPTS

def _render_redeemer_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    BANNER_BLUE = (35/255, 64/255, 200/255)
    WHITE_C     = (1.0, 1.0, 1.0)
    BLACK_C     = (0.0, 0.0, 0.0)
    EMPID_BLUE  = (31/255, 72/255, 255/255)

    NAME_BANNER = (7.0,   146.8, 113.0, 158.3)
    DESIG_BOX   = (48.3,  158.8, 110.0, 165.2)
    EMPID_BOX   = (111.6, 108.5, 138.0, 117.5)
    DOB_BOX     = (54.0,  171.5, 145.0, 181.0)
    FNAME_BOX   = (54.0,  181.0, 145.0, 190.5)
    ADDR_BOX    = (54.0,  190.0, 145.0, 200.0)
    PHOTO_BOX   = (54.6,   81.6,  98.6, 136.5)

    page.draw_rect(fitz.Rect(*NAME_BANNER), color=BANNER_BLUE, fill=BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*DESIG_BOX), color=BANNER_BLUE, fill=BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*EMPID_BOX), color=WHITE_C, fill=WHITE_C, width=0, overlay=True)

    DATA_BG = (233/255, 249/255, 255/255)
    page.draw_rect(
        fitz.Rect(DOB_BOX[0], DOB_BOX[1], ADDR_BOX[2], ADDR_BOX[3]),
        color=DATA_BG, fill=DATA_BG, width=0, overlay=True,
    )

    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes:
        prepared = prepare_photo_for_rect_cover(
            photo_bytes, PHOTO_BOX,
            scale=7, output_format="JPEG",
            is_redeemer=True,
        )
        insert_image_safe(page, fitz.Rect(*PHOTO_BOX), prepared or photo_bytes)
    else:
        insert_image_safe(page, fitz.Rect(*PHOTO_BOX), photo_bytes)

    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(*PHOTO_BOX))
    sh.finish(color=BLACK_C, fill=None, width=0.6, closePath=True)
    sh.commit(overlay=True)

    def _draw_centered(text, box, size_pt, color, *, upper=False,
                       font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = box
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw - 2, size_pt, max(4.0, size_pt * 0.55)) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw - 2, fs)
        tw = fo.text_length(val, fontsize=fs)
        x = x0 + (bw - tw) / 2.0
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _draw_left(text, box, size_pt, color, *, pad_left=2, upper=False,
                   font_obj=None, font_fn=None, font_name=None, shrink=True):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        x0, y0, x1, y1 = box
        bw = x1 - x0
        fo = font_obj or bold_obj
        ff = font_fn or bold_fn
        fn = font_name or fn_bold
        fs = _fit_size(fo, val, bw - pad_left - 2, size_pt, max(4.0, size_pt * 0.55)) if shrink else size_pt
        if shrink:
            val = _ellipsize_to_width(fo, val, bw - pad_left - 2, fs)
        baseline = _centered_baseline_for_box(fo, y0, y1, fs)
        page.insert_text((x0 + pad_left, baseline), val,
                         fontname=fn, fontfile=ff,
                         fontsize=fs, color=color, overlay=True)

    def _draw_wrapped_fixed(text, box, size_pt, color, *, pad_left=6, max_lines=2, line_gap=1.0):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        x0, y0, x1, y1 = box
        lines, target_fs = wrap_and_shrink_text(bold_obj, val, (x1 - x0) - pad_left - 2, max_lines, base_size=size_pt)
        if not lines:
            return
        step = target_fs * line_gap
        for idx, line in enumerate(lines):
            page.insert_text((x0 + pad_left, y0 + target_fs + idx * step), line,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=target_fs, color=color, overlay=True)

    name = _emp_value(student, "employee_name", "student_name", upper=True)
    _draw_centered(name, NAME_BANNER, 7.5, WHITE_C)

    desig = _emp_value(student, "designation", upper=True)
    _draw_left(desig, DESIG_BOX, 4.66, WHITE_C, pad_left=1, shrink=False)

    emp_id = _emp_value(student, "emp_id", "roll")
    if emp_id:
        _draw_left(emp_id, EMPID_BOX, 7.0, EMPID_BLUE, pad_left=2)

    _draw_left(_emp_value(student, "dob"), DOB_BOX, 7.0, BLACK_C, pad_left=6, shrink=False)
    _draw_left(_emp_value(student, "father_name", "fh_name", "fname"), FNAME_BOX, 7.0, BLACK_C, pad_left=6, shrink=False)
    _draw_wrapped_fixed(_emp_value(student, "address"), ADDR_BOX, 7.0, BLACK_C, pad_left=6, max_lines=2, line_gap=1.0)

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
