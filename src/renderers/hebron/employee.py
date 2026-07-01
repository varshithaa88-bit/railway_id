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

def _render_hebron_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    # Colours straight from the standalone hebron_emp script
    COL_RED_BAND   = (170/255,  15/255,  15/255)
    COL_WHITE      = (1.0, 1.0, 1.0)
    COL_BLACK      = (0.0, 0.0, 0.0)
    COL_VALIDITY_R = (170/255,  16/255,  16/255)
    COL_ORANGE     = (255/255, 117/255,  31/255)

    # Erase placeholders
    erase_zones = [
        (8.0,  133.0, 112.0, 146.0, COL_RED_BAND),
        (50.0, 145.5,  73.0, 154.5, COL_RED_BAND),
        (53.5, 161.0,  72.0, 169.6, COL_WHITE),
        (53.5, 169.0,  72.0, 177.2, COL_WHITE),
        (53.5, 176.5,  72.0, 184.6, COL_WHITE),
        (53.5, 190.5,  72.0, 198.8, COL_WHITE),
        (112.0, 111.2, 142.0, 124.0, COL_WHITE),
    ]
    for x0, y0, x1, y1, col in erase_zones:
        page.draw_rect(fitz.Rect(x0, y0, x1, y1),
                       color=col, fill=col, width=0, overlay=True)

    # Photo box
    PHOTO_BOX = (52.44, 74.28, 99.57, 128.23)
    border_w  = 1.5
    page.draw_rect(fitz.Rect(*PHOTO_BOX),
                   color=COL_ORANGE, fill=COL_ORANGE, width=0, overlay=True)
    inner = fitz.Rect(
        PHOTO_BOX[0] + border_w, PHOTO_BOX[1] + border_w,
        PHOTO_BOX[2] - border_w, PHOTO_BOX[3] - border_w,
    )
    page.draw_rect(inner, color=(240/255, 240/255, 240/255),
                   fill=(240/255, 240/255, 240/255), width=0, overlay=True)

    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes:
        prepared = prepare_photo_for_rect_cover(
            photo_bytes,
            (inner.x0, inner.y0, inner.x1, inner.y1),
            scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        )
        insert_image_safe(page, inner, prepared or photo_bytes)
    else:
        insert_image_safe(page, inner, photo_bytes)

    # Field placement helper
    def _put_field(text, rect_pt, color, *, size_pt=6.0, min_pt=3.8,
                   max_pt=None, align="left", upper=False):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        if upper:
            val = val.upper()
        if max_pt is None:
            max_pt = size_pt
        rect = fitz.Rect(*rect_pt)
        fs = _fit_size(bold_obj, val, rect.width, max_pt, min_pt)
        val = _ellipsize_to_width(bold_obj, val, rect.width, fs)
        tw = bold_obj.text_length(val, fontsize=fs)
        if align == "center":
            x = rect.x0 + (rect.width - tw) / 2.0
        elif align == "right":
            x = rect.x1 - tw
        else:
            x = rect.x0
        baseline = _centered_baseline_for_box(bold_obj, rect.y0, rect.y1, fs)
        page.insert_text((x, baseline), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=color, overlay=True)

    # Draw colons
    colon_size = 5.5
    for y_pt in (161.5, 169.0, 176.5, 190.5):
        page.insert_text((54.7, y_pt + 6.5), ":",
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=colon_size, color=COL_BLACK, overlay=True)

    # Write employee values
    name = _emp_value(student, "employee_name", "student_name", upper=True)
    _put_field(name, (8.0, 134.0, 112.0, 145.6), COL_WHITE,
               size_pt=7.5, min_pt=5.0, max_pt=7.5, align="center")

    desig = _emp_value(student, "designation", upper=True)
    if desig:
        desig_val = clean_card_value(desig)
        fs = _fit_size(bold_obj, desig_val, 59.5, 5.0, 3.8)
        desig_val = _ellipsize_to_width(bold_obj, desig_val, 59.5, fs)
        page.insert_text((50.5, 151.8), " " + desig_val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=COL_WHITE, overlay=True)

    validity = _emp_value(student, "validity")
    if not validity:
        validity = "2026-27"
    _put_field(validity, (112.5, 111.6, 142.0, 122.6), COL_VALIDITY_R,
               size_pt=7.5, min_pt=5.5, max_pt=8.0, align="center")

    # Uniform size for detail rows
    _fh_v     = clean_card_value(_emp_value(student, "father_name", "fh_name") or "")
    _dob_v    = clean_card_value(_emp_value(student, "dob") or "")
    _addr_v   = clean_card_value(_emp_value(student, "address") or "")
    _mobile_v = clean_card_value(_emp_value(student, "mobile", "contact_no") or "")
    _addr_longest = max(_addr_v.split(), key=lambda w: bold_obj.text_length(w, fontsize=5.5), default=_addr_v) if _addr_v else ""
    _DET_BASE = 5.5
    _DET_MIN = 3.8
    _DET_W = 88.5
    _det_fs = _DET_BASE
    for _tv in (_fh_v, _dob_v, _mobile_v, _addr_longest):
        if _tv:
            _det_fs = min(_det_fs, _fit_size(bold_obj, _tv, _DET_W, _DET_BASE, _DET_MIN))

    def _put_det(val, rect_y0, rect_y1):
        if not val:
            return
        baseline = _centered_baseline_for_box(bold_obj, rect_y0, rect_y1, _det_fs)
        page.insert_text((61.5, baseline), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=_det_fs, color=COL_BLACK, overlay=True)

    _put_det(_fh_v,     161.5, 169.8)
    _put_det(_dob_v,    169.0, 177.2)

    # Address wrapping
    if _addr_v:
        _addr_lines, _ = wrap_and_shrink_text(bold_obj, _addr_v, _DET_W, 2, base_size=_det_fs)
        _asc = getattr(bold_obj, "ascender", 0.9)
        _baseline = 176.5 + _det_fs * _asc + 0.5
        for _aline in _addr_lines:
            page.insert_text((61.5, _baseline), _aline,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=_det_fs, color=COL_BLACK, overlay=True)
            _baseline += _det_fs * 1.15

    _put_det(_mobile_v, 190.5, 198.8)

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
