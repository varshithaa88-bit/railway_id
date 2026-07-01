import fitz
from src.config import (
    WHITE, BLOOD_RED, PHOTO_RECT_COORDS, PHOTO_EMBED_SCALE,
    NAME_TEXT_RECT_COORDS, NAME_FONT_SIZE, NAME_COLOR,
    CLASS_TEXT_RECT_COORDS, CLASS_FONT_SIZE, FATHER_CLEAN_COORDS,
    MOTHER_CLEAN_COORDS, DOB_CLEAN_COORDS, ADDRESS_CLEAN_COORDS,
    MOBILE_CLEAN_COORDS, ADM_WHITEOUT_COORDS, SESSION_WHITEOUT_COORDS,
    FATHER_VALUE_RECT_COORDS, MOTHER_VALUE_RECT_COORDS, MOBILE_VALUE_RECT_COORDS,
    VALUE_FONT_SIZE, VALUE_COLOR, DOB_VALUE_RECT_COORDS, ADDRESS_VALUE_RECT_COORDS,
    ADM_VALUE_RECT_COORDS, ADM_FONT_SIZE, DEFAULT_SESSION,
    SESSION_VALUE_RECT_COORDS, SESSION_FONT_SIZE, BLOOD_VALUE_RECT_COORDS,
    BLOOD_FONT_SIZE, BAND_Y0, BAND_Y1, BANNER_RED
)
from src.utils.text import (
    ensure_fonts, draw_text_vertically_centered, clean_card_value,
    _fit_size, _put_single, render_address, redraw_blood_teardrop_transformed,
    draw_text_centered_hv, _tr_point, _tr_rect, _tr_font_size
)
from src.utils.photo import (
    prepare_photo_for_rect_cover, fetch_photo_bytes, insert_image_safe
)

def draw_card_overlay_hebron(page, student: dict, tr):
    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if anton_obj is None or bold_obj is None:
        return

    shape = page.new_shape()
    def band_right_x(y):
        return -0.3952 * y + 172.6234
    pts = [
        _tr_point(tr, 0, BAND_Y0),
        _tr_point(tr, band_right_x(BAND_Y0), BAND_Y0),
        _tr_point(tr, band_right_x(BAND_Y1), BAND_Y1),
        _tr_point(tr, 0, BAND_Y1),
    ]
    shape.draw_polyline(pts)
    shape.draw_line(pts[-1], pts[0])
    shape.finish(color=BANNER_RED, fill=BANNER_RED, width=0)
    shape.commit(overlay=True)

    for coords in [FATHER_CLEAN_COORDS, MOTHER_CLEAN_COORDS, DOB_CLEAN_COORDS,
                   ADDRESS_CLEAN_COORDS, MOBILE_CLEAN_COORDS,
                   ADM_WHITEOUT_COORDS, SESSION_WHITEOUT_COORDS]:
        page.draw_rect(_tr_rect(tr, coords), color=WHITE, fill=WHITE, width=0, overlay=True)

    redraw_blood_teardrop_transformed(page, tr, BLOOD_RED)

    _hebron_photo_rect = _tr_rect(tr, PHOTO_RECT_COORDS)
    photo_bytes = prepare_photo_for_rect_cover(
        fetch_photo_bytes(student.get("photo_url", "")),
        (_hebron_photo_rect.x0, _hebron_photo_rect.y0,
         _hebron_photo_rect.x1, _hebron_photo_rect.y1),
        scale=PHOTO_EMBED_SCALE, output_format="JPEG",
    )
    insert_image_safe(page, _hebron_photo_rect, photo_bytes)

    draw_text_vertically_centered(
        page, _tr_rect(tr, NAME_TEXT_RECT_COORDS),
        str(student.get("student_name", "")).strip().upper(),
        anton_fn, fn_anton, anton_obj, _tr_font_size(tr, NAME_FONT_SIZE), NAME_COLOR,
    )

    cls = str(student.get("class", "")).strip().upper()
    sec = str(student.get("section", "")).strip().upper()
    roll = str(student.get("roll", "")).strip()
    parts = []
    if cls:
        parts.append(f"CLASS:{cls}")
    if sec:
        parts.append(f"SEC:{sec}")
    if roll:
        parts.append(f"ROLL:{roll}")
    draw_text_vertically_centered(
        page, _tr_rect(tr, CLASS_TEXT_RECT_COORDS),
        "  ".join(parts),
        bold_fn, fn_bold, bold_obj, _tr_font_size(tr, CLASS_FONT_SIZE), NAME_COLOR,
    )

    for coords, key in [
        (FATHER_VALUE_RECT_COORDS, "father_name"),
        (MOTHER_VALUE_RECT_COORDS, "mother_name"),
        (MOBILE_VALUE_RECT_COORDS, "mobile"),
    ]:
        rect = _tr_rect(tr, coords)
        txt = clean_card_value(student.get(key, ""))
        if txt:
            sz = _fit_size(bold_obj, txt, rect.width, _tr_font_size(tr, VALUE_FONT_SIZE))
            _put_single(page, rect, txt, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    dob = clean_card_value(student.get("dob", ""))
    if dob:
        rect = _tr_rect(tr, DOB_VALUE_RECT_COORDS)
        sz = _fit_size(bold_obj, dob, rect.width, _tr_font_size(tr, VALUE_FONT_SIZE))
        _put_single(page, rect, dob, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    render_address(
        page, _tr_rect(tr, ADDRESS_VALUE_RECT_COORDS),
        student.get("address", ""),
        bold_fn, fn_bold, bold_obj, VALUE_COLOR,
    )

    adm = clean_card_value(student.get("adm_no", ""))
    if adm:
        rect = _tr_rect(tr, ADM_VALUE_RECT_COORDS)
        sz = _fit_size(bold_obj, adm, rect.width, _tr_font_size(tr, ADM_FONT_SIZE))
        _put_single(page, rect, adm, bold_fn, fn_bold, sz, VALUE_COLOR, bold_obj)

    sess = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    rect = _tr_rect(tr, SESSION_VALUE_RECT_COORDS)
    sz = _fit_size(anton_obj, sess, rect.width, _tr_font_size(tr, SESSION_FONT_SIZE))
    _put_single(page, rect, sess, anton_fn, fn_anton, sz, VALUE_COLOR, anton_obj)

    blood = str(student.get("blood_group", "")).strip().upper()
    if blood and blood.lower() not in {"nan", "none"} and any(c.isalpha() for c in blood):
        draw_text_centered_hv(
            page, _tr_rect(tr, BLOOD_VALUE_RECT_COORDS),
            blood, bold_fn, fn_bold, bold_obj, _tr_font_size(tr, BLOOD_FONT_SIZE), WHITE,
        )
