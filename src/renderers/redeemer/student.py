import io
import logging
import fitz
from src.config import (
    REDEEMER_BG_COLOR, REDEEMER_WHITE, REDEEMER_BLACK, REDEEMER_PHOTO_OUTER_RECT,
    REDEEMER_PHOTO_RECT_COORDS, REDEEMER_PHOTO_BORDER_W, REDEEMER_BANNER_RECT,
    REDEEMER_NAME_BASELINE_Y, REDEEMER_CLASS_BASELINE_Y, REDEEMER_SESSION_CLEAN_COORDS,
    REDEEMER_SESSION_VALUE_RECT, REDEEMER_DATA_CLEAN_RECT, REDEEMER_VALUE_X,
    REDEEMER_COLON_X, REDEEMER_VALUE_MAX_X, REDEEMER_CLASS_VALUE_BASELINE_Y,
    REDEEMER_FATHER_BASELINE_Y, REDEEMER_MOBILE_BASELINE_Y, REDEEMER_ADDRESS_BASELINE_Y,
    REDEEMER_DOB_BASELINE_Y, REDEEMER_NAME_FONT_SIZE, REDEEMER_NAME_MIN_SIZE,
    REDEEMER_CLASS_FONT_SIZE, REDEEMER_VALUE_FONT_SIZE, REDEEMER_ADDRESS_MAX_LINES,
    REDEEMER_ADDRESS_LINE_GAP, REDEEMER_SESSION_FONT_SIZE, PHOTO_EMBED_SCALE,
    DEFAULT_SESSION, REDEEMER_BANNER_TOP_RIGHT_X, REDEEMER_BANNER_BOT_RIGHT_X,
    REDEEMER_BANNER_Y0, REDEEMER_BANNER_Y1, REDEEMER_BANNER_TEXT_LEFT,
    REDEEMER_BANNER_TEXT_RIGHT, REDEEMER_BANNER_CENTER_X
)
from src.utils.text import (
    ensure_fonts, draw_redeemer_banner_text, draw_redeemer_value,
    render_redeemer_address, clean_card_value, _fit_size,
    _ellipsize_to_width, _put_single, _tr_point, _tr_rect,
    wrap_and_shrink_text
)
from src.utils.photo import (
    prepare_photo_for_rect_cover, fetch_photo_bytes, insert_image_safe
)
from src.utils.pdf import _PDF_SAVE_OPTS

log = logging.getLogger("idcard.renderer.redeemer.student")

def _draw_horizontal_gradient_mask(page, rect, left_color, right_color, steps):
    steps = max(8, int(steps))
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    band_w = (x1 - x0) / steps
    for step in range(steps):
        t = 0.0 if steps == 1 else step / (steps - 1)
        color = (
            left_color[0] + t * (right_color[0] - left_color[0]),
            left_color[1] + t * (right_color[1] - left_color[1]),
            left_color[2] + t * (right_color[2] - left_color[2]),
        )
        rx0 = x0 + step * band_w
        rx1 = x0 + (step + 1) * band_w + 0.02
        page.draw_rect(fitz.Rect(rx0, y0, rx1, y1), color=color, fill=color, width=0, overlay=True)


def _draw_redeemer_overlay_core(page, student: dict, map_point, map_rect, scale_x=1.0, scale_y=1.0):
    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if anton_obj is None or bold_obj is None:
        return

    _MASK_BG = (0.94, 0.97, 0.99)
    _BANNER_BLUE = (35/255, 64/255, 200/255)
    _SESSION_BG  = (0.98, 0.99, 1.0)
    
    page.draw_rect(fitz.Rect(*map_rect((56.0, 163.5, 153.0, 205.0))), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*map_rect((4.0, 136.0, 124.0, 161.0))), color=_BANNER_BLUE, fill=_BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*map_rect(REDEEMER_SESSION_CLEAN_COORDS)), color=_SESSION_BG, fill=_SESSION_BG, width=0, overlay=True)

    page.draw_rect(fitz.Rect(*map_rect(REDEEMER_PHOTO_OUTER_RECT)), color=REDEEMER_WHITE, fill=REDEEMER_WHITE, width=0, overlay=True)

    page.add_redact_annot(fitz.Rect(*map_rect((61.0, 163.0, 67.0, 202.0))), fill=None)
    page.add_redact_annot(fitz.Rect(*map_rect((109.0, 105.0, 142.0, 115.0))), fill=None)
    
    page.draw_rect(fitz.Rect(*map_rect((12.0, 137.0, 112.0, 148.0))), color=_BANNER_BLUE, fill=_BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(*map_rect((12.0, 149.0, 112.0, 159.0))), color=_BANNER_BLUE, fill=_BANNER_BLUE, width=0, overlay=True)
    
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    _redeemer_photo_rect = map_rect(REDEEMER_PHOTO_RECT_COORDS)
    photo_bytes = prepare_photo_for_rect_cover(
        fetch_photo_bytes(student.get("photo_url", "")),
        (_redeemer_photo_rect.x0, _redeemer_photo_rect.y0,
         _redeemer_photo_rect.x1, _redeemer_photo_rect.y1),
        scale=PHOTO_EMBED_SCALE, output_format="JPEG",
        is_redeemer=True,
    )
    insert_image_safe(page, _redeemer_photo_rect, photo_bytes)
    page.draw_rect(
        map_rect(REDEEMER_PHOTO_OUTER_RECT),
        color=REDEEMER_BLACK, fill=None,
        width=max(0.1, REDEEMER_PHOTO_BORDER_W * ((scale_x + scale_y) / 2.0)),
        overlay=True,
    )

    banner_min_scale = max(0.5, min(scale_x, scale_y))
    banner_max_width = max(1.0, (REDEEMER_BANNER_TEXT_RIGHT - REDEEMER_BANNER_TEXT_LEFT) * scale_x)
    center_x = map_point(60.0, 0).x

    draw_redeemer_banner_text(
        page,
        student.get("student_name", ""),
        center_x,
        map_point(0, REDEEMER_NAME_BASELINE_Y).y,
        banner_max_width,
        bold_fn, fn_bold, bold_obj,
        REDEEMER_NAME_FONT_SIZE * banner_min_scale, REDEEMER_WHITE,
        tracking=0.0,
        min_size=REDEEMER_NAME_MIN_SIZE * banner_min_scale,
    )

    sec_text   = clean_card_value(student.get("section", "")).upper()
    roll_text  = clean_card_value(student.get("roll", "")).strip()
    class_text = clean_card_value(student.get("class", "")).upper()
    _banner_fs  = REDEEMER_CLASS_FONT_SIZE * banner_min_scale
    _banner_min = 4.5 * banner_min_scale
    _bl2 = map_point(0, REDEEMER_CLASS_BASELINE_Y).y

    if sec_text or roll_text:
        _row2_t = (REDEEMER_CLASS_BASELINE_Y - REDEEMER_BANNER_Y0) / max(1.0, REDEEMER_BANNER_Y1 - REDEEMER_BANNER_Y0)
        _row2_right = REDEEMER_BANNER_TOP_RIGHT_X + _row2_t * (REDEEMER_BANNER_BOT_RIGHT_X - REDEEMER_BANNER_TOP_RIGHT_X) - 2.0
        _sec_label_x  = map_point(17.47, 0).x
        _roll_label_x = map_point(78.78, 0).x
        _banner_right_x = map_point(_row2_right, 0).x
        _sec_str  = f"Sec: {sec_text}"  if sec_text  else ""
        _roll_str = f"Roll: {roll_text}" if roll_text else ""
        _sec_max_w  = max(1.0, (_roll_label_x - _sec_label_x - 2.0 * scale_x))
        _roll_max_w = max(1.0, (_banner_right_x - _roll_label_x))
        if _sec_str:
            _sz = _fit_size(bold_obj, _sec_str, _sec_max_w, _banner_fs, _banner_min)
            _sec_str = _ellipsize_to_width(bold_obj, _sec_str, _sec_max_w, _sz)
            page.insert_text(
                (_sec_label_x, _bl2), _sec_str,
                fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                fontsize=_sz, color=REDEEMER_WHITE, overlay=True,
            )
        if _roll_str:
            _sz = _fit_size(bold_obj, _roll_str, _roll_max_w, _banner_fs, _banner_min)
            _roll_str = _ellipsize_to_width(bold_obj, _roll_str, _roll_max_w, _sz)
            page.insert_text(
                (_roll_label_x, _bl2), _roll_str,
                fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                fontsize=_sz, color=REDEEMER_WHITE, overlay=True,
            )
    elif class_text:
        draw_redeemer_banner_text(
            page, f"CLASS: {class_text}", center_x, _bl2, banner_max_width,
            bold_fn, fn_bold, bold_obj, _banner_fs, REDEEMER_WHITE,
            tracking=0.0, min_size=_banner_min,
        )

    value_x        = map_point(REDEEMER_VALUE_X, 0).x
    value_max_width = max(1.0, (REDEEMER_VALUE_MAX_X - REDEEMER_VALUE_X) * scale_x)
    value_base_size = REDEEMER_VALUE_FONT_SIZE * min(scale_x, scale_y)
    value_min_size  = 4.7 * min(scale_x, scale_y)

    _colon_x = map_point(REDEEMER_COLON_X, 0).x
    for _colon_baseline_y in [
        REDEEMER_CLASS_VALUE_BASELINE_Y,
        REDEEMER_FATHER_BASELINE_Y,
        REDEEMER_MOBILE_BASELINE_Y,
        REDEEMER_ADDRESS_BASELINE_Y,
    ]:
        page.insert_text(
            (_colon_x, map_point(0, _colon_baseline_y).y),
            ":",
            fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
            fontsize=value_base_size, color=REDEEMER_BLACK, overlay=True,
        )

    class_val  = str(student.get("class") or "").strip().upper()
    father_val = str(student.get("father_name") or "").strip()
    mobile_val = str(student.get("mobile") or "").strip()
    addr_val   = str(student.get("address") or "").strip()

    if not class_val:  class_val  = "NOT FOUND"
    if not father_val: father_val = "NOT FOUND"
    if not mobile_val: mobile_val = "NOT FOUND"
    if not addr_val:   addr_val   = "NOT FOUND"

    _addr_wrap_word = max(addr_val.split(), key=lambda w: bold_obj.text_length(w, fontsize=value_base_size), default=addr_val)
    _uniform_fs = value_base_size
    for _test_val in (class_val, father_val, mobile_val, _addr_wrap_word):
        _uniform_fs = min(_uniform_fs, _fit_size(bold_obj, _test_val, value_max_width, value_base_size, value_min_size))

    page.insert_text((value_x, map_point(0, REDEEMER_CLASS_VALUE_BASELINE_Y).y), class_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=REDEEMER_BLACK, overlay=True)
    page.insert_text((value_x, map_point(0, REDEEMER_FATHER_BASELINE_Y).y), father_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=REDEEMER_BLACK, overlay=True)
    page.insert_text((value_x, map_point(0, REDEEMER_MOBILE_BASELINE_Y).y), mobile_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=REDEEMER_BLACK, overlay=True)

    _addr_lines, _ = wrap_and_shrink_text(bold_obj, addr_val, value_max_width, 2, base_size=_uniform_fs)
    for _ai, _aline in enumerate(_addr_lines):
        page.insert_text((value_x, map_point(0, REDEEMER_ADDRESS_BASELINE_Y).y + _ai * (_uniform_fs * REDEEMER_ADDRESS_LINE_GAP)),
                         _aline, fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                         fontsize=_uniform_fs, color=REDEEMER_BLACK, overlay=True)

    session_value = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    session_rect  = map_rect(REDEEMER_SESSION_VALUE_RECT)
    session_size  = _fit_size(anton_obj, session_value, session_rect.width,
                              REDEEMER_SESSION_FONT_SIZE * min(scale_x, scale_y),
                              5.6 * min(scale_x, scale_y))
    session_value = _ellipsize_to_width(anton_obj, session_value, session_rect.width, session_size)
    _put_single(page, session_rect, session_value, anton_fn, fn_anton, session_size, REDEEMER_BLACK, anton_obj)


def draw_card_overlay_redeemer(page, student: dict, tr):
    _draw_redeemer_overlay_core(
        page, student,
        lambda x, y: _tr_point(tr, x, y),
        lambda coords: _tr_rect(tr, coords),
        tr["sx"], tr["sy"],
    )


def _render_redeemer_student_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    _MASK_BG = (0.94, 0.97, 0.99)
    _BANNER_BLUE = (35/255, 64/255, 200/255)
    _SESSION_BG  = (0.98, 0.99, 1.0)
    
    page.draw_rect(fitz.Rect(57.0, 163.5, 155.0, 172.5), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 172.5, 155.0, 182.0), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 182.0, 155.0, 191.5), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 191.5, 155.0, 205.0), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)

    page.draw_rect(fitz.Rect(12.0, 137.0, 112.0, 159.0), color=_BANNER_BLUE, fill=_BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(109.0, 105.0, 142.0, 115.0), color=_SESSION_BG, fill=_SESSION_BG, width=0, overlay=True)
    
    _PHOTO_OUTER = fitz.Rect(53.55, 72.70, 99.45, 129.72)
    _PHOTO_INNER = fitz.Rect(54.58, 73.78, 98.59, 128.68)
    page.draw_rect(_PHOTO_OUTER, color=(1,1,1), fill=(1,1,1), width=0, overlay=True)
    _photo_bytes = prepare_photo_for_rect_cover(
        fetch_photo_bytes(student.get("photo_url", "")),
        (_PHOTO_INNER.x0, _PHOTO_INNER.y0, _PHOTO_INNER.x1, _PHOTO_INNER.y1),
        scale=PHOTO_EMBED_SCALE, output_format="JPEG", is_redeemer=True,
    )
    insert_image_safe(page, _PHOTO_INNER, _photo_bytes)
    page.draw_rect(_PHOTO_OUTER, color=(0,0,0), fill=None, width=1.0, overlay=True)

    _name_val = clean_card_value(student.get("student_name", ""))
    if _name_val:
        _name_max_w = 104.0
        _name_fs = _fit_size(bold_obj, _name_val, _name_max_w,
                             REDEEMER_NAME_FONT_SIZE, REDEEMER_NAME_MIN_SIZE)
        _name_val_fit = _ellipsize_to_width(bold_obj, _name_val, _name_max_w, _name_fs)
        _name_tw = bold_obj.text_length(_name_val_fit, fontsize=_name_fs)
        _name_x  = 4.0 + (_name_max_w - _name_tw) / 2.0
        page.insert_text(
            (_name_x, REDEEMER_NAME_BASELINE_Y),
            _name_val_fit,
            fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
            fontsize=_name_fs, color=(1,1,1), overlay=True,
        )

    _sec_text  = clean_card_value(student.get("section", "")).upper()
    _roll_text = clean_card_value(student.get("roll",    ""))
    _class_text = clean_card_value(student.get("class",  "")).upper()
    _banner_fs  = REDEEMER_CLASS_FONT_SIZE
    _banner_min = 4.5
    _bl2 = REDEEMER_CLASS_BASELINE_Y

    if _sec_text or _roll_text:
        if _sec_text:
            _s = f"Sec: {_sec_text}"
            _sz = _fit_size(bold_obj, _s, 55.0, _banner_fs, _banner_min)
            _s  = _ellipsize_to_width(bold_obj, _s, 55.0, _sz)
            page.insert_text((17.47, _bl2), _s,
                             fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                             fontsize=_sz, color=(1,1,1), overlay=True)
        if _roll_text:
            _r = f"Roll: {_roll_text}"
            _sz = _fit_size(bold_obj, _r, 30.0, _banner_fs, _banner_min)
            _r  = _ellipsize_to_width(bold_obj, _r, 30.0, _sz)
            page.insert_text((78.78, _bl2), _r,
                             fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                             fontsize=_sz, color=(1,1,1), overlay=True)
    elif _class_text:
        _s = f"CLASS: {_class_text}"
        _sz = _fit_size(bold_obj, _s, 104.0, _banner_fs, _banner_min)
        _tw = bold_obj.text_length(_s, fontsize=_sz)
        page.insert_text((4.0 + (104.0 - _tw) / 2.0, _bl2), _s,
                         fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                         fontsize=_sz, color=(1,1,1), overlay=True)

    _COLON_X  = 57.49
    _VALUE_X  = 62.0
    _VALUE_W  = 150.0 - _VALUE_X
    _FS       = REDEEMER_VALUE_FONT_SIZE
    _FS_MIN   = 4.7
    _BLACK    = (0.0, 0.0, 0.0)

    _row_baselines = [
        REDEEMER_CLASS_VALUE_BASELINE_Y,
        REDEEMER_FATHER_BASELINE_Y,
        REDEEMER_MOBILE_BASELINE_Y,
        REDEEMER_ADDRESS_BASELINE_Y,
    ]
    for _by in _row_baselines:
        page.insert_text(
            (_COLON_X, _by), ":",
            fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
            fontsize=_FS, color=_BLACK, overlay=True,
        )

    class_val  = str(student.get("class") or "").strip().upper()
    father_val = str(student.get("father_name") or "").strip()
    mobile_val = str(student.get("mobile") or "").strip()
    addr_val   = str(student.get("address") or "").strip()

    if not class_val:  class_val  = "NOT FOUND"
    if not father_val: father_val = "NOT FOUND"
    if not mobile_val: mobile_val = "NOT FOUND"
    if not addr_val:   addr_val   = "NOT FOUND"

    _addr_wrap_word = max(addr_val.split(), key=lambda w: bold_obj.text_length(w, fontsize=_FS), default=addr_val)
    _uniform_fs = _FS
    for _test_val in (class_val, father_val, mobile_val, _addr_wrap_word):
        _uniform_fs = min(_uniform_fs, _fit_size(bold_obj, _test_val, _VALUE_W, _FS, _FS_MIN))

    page.insert_text((_VALUE_X, REDEEMER_CLASS_VALUE_BASELINE_Y), class_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=_BLACK, overlay=True)
    page.insert_text((_VALUE_X, REDEEMER_FATHER_BASELINE_Y), father_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=_BLACK, overlay=True)
    page.insert_text((_VALUE_X, REDEEMER_MOBILE_BASELINE_Y), mobile_val,
                     fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                     fontsize=_uniform_fs, color=_BLACK, overlay=True)

    _addr_lines, _ = wrap_and_shrink_text(bold_obj, addr_val, _VALUE_W, 2, base_size=_uniform_fs)
    for _ai, _aline in enumerate(_addr_lines):
        page.insert_text((_VALUE_X, REDEEMER_ADDRESS_BASELINE_Y + _ai * (_uniform_fs * REDEEMER_ADDRESS_LINE_GAP)),
                         _aline, fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                         fontsize=_uniform_fs, color=_BLACK, overlay=True)

    _sess = clean_card_value(student.get("session","")) or DEFAULT_SESSION
    _sess_rect = fitz.Rect(*REDEEMER_SESSION_VALUE_RECT)
    _sess_fs = _fit_size(anton_obj, _sess, _sess_rect.width,
                         REDEEMER_SESSION_FONT_SIZE, 5.6)
    _sess = _ellipsize_to_width(anton_obj, _sess, _sess_rect.width, _sess_fs)
    _put_single(page, _sess_rect, _sess, anton_fn, fn_anton, _sess_fs, _BLACK, anton_obj)

    _buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(_buf, **_save_opts)
    except TypeError:
        doc.save(_buf, deflate=True, garbage=3, clean=True, incremental=False)
    doc.close()
    return _buf.getvalue()
