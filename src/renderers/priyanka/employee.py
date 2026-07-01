import io
import fitz
from src.config import PHOTO_EMBED_SCALE
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    wrap_and_shrink_text, _emp_value
)
from src.utils.photo import (
    fetch_photo_bytes, insert_image_safe, HAS_PIL
)
from src.utils.pdf import _PDF_SAVE_OPTS

if HAS_PIL:
    from PIL import Image

def _render_priyanka_emp_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    NAME_BG_COLOR     = (255/255, 188/255, 245/255)
    NAME_BG_COLOR_PIL = (255, 188, 245, 255)
    NAVY_COLOR        = (0x0F/255, 0x00/255, 0x6A/255)
    WHITE_BG          = (1, 1, 1)

    detail_value_rects = [
        fitz.Rect(55.0, 164.5, 105.0, 173.0),
        fitz.Rect(55.0, 173.5, 105.0, 182.0),
        fitz.Rect(55.0, 181.0, 105.0, 190.0),
        fitz.Rect(55.0, 196.5, 105.0, 205.5),
    ]
    for r in detail_value_rects:
        _erase = page.new_shape()
        _erase.draw_rect(r)
        _erase.finish(color=None, fill=WHITE_BG, width=0)
        _erase.commit(overlay=True)
        page.add_redact_annot(r, fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    name_bg_rect    = fitz.Rect(40.0, 139.5, 105.0, 151.2)
    desig_bg_strip  = fitz.Rect(43.0, 151.5,  52.0, 158.0)
    for r, fill in ((name_bg_rect, NAME_BG_COLOR),
                    (desig_bg_strip, NAME_BG_COLOR)):
        _s = page.new_shape()
        _s.draw_rect(r)
        _s.finish(color=None, fill=fill, width=0)
        _s.commit(overlay=True)

    PHOTO_RECT     = fitz.Rect(51.8, 73.0, 92.5, 129.5)
    PHOTO_CORNER_R = 4.0
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))

    _ps = page.new_shape()
    _ps.draw_rect(PHOTO_RECT)
    _ps.finish(color=None, fill=WHITE_BG, width=0)
    _ps.commit(overlay=True)
    page.add_redact_annot(PHOTO_RECT, fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    if photo_bytes and HAS_PIL:
        try:
            from PIL import ImageDraw as _PILDraw
            _scale = max(PHOTO_EMBED_SCALE, 6)
            tw_px = max(1, int(round(PHOTO_RECT.width  * _scale)))
            th_px = max(1, int(round(PHOTO_RECT.height * _scale)))
            r_px  = max(2, int(round(PHOTO_CORNER_R    * _scale)))

            with Image.open(io.BytesIO(photo_bytes)) as _src:
                _rgb = _src.convert("RGB")
                _resized = _rgb.resize((tw_px, th_px), Image.Resampling.LANCZOS)

            _mask = Image.new("L", (tw_px, th_px), 0)
            _PILDraw.Draw(_mask).rounded_rectangle(
                (0, 0, tw_px - 1, th_px - 1), radius=r_px, fill=255)
            _out = Image.new("RGBA", (tw_px, th_px), NAME_BG_COLOR_PIL)
            _out.paste(_resized.convert("RGBA"), (0, 0), _mask)
            _resized.close()
            _mask.close()

            _buf = io.BytesIO()
            _out.save(_buf, format="PNG")
            _out.close()
            page.insert_image(PHOTO_RECT, stream=_buf.getvalue(),
                              keep_proportion=False, overlay=True)
        except Exception:
            insert_image_safe(page, PHOTO_RECT, photo_bytes)
    elif photo_bytes:
        insert_image_safe(page, PHOTO_RECT, photo_bytes)

    name_text = _emp_value(student, "employee_name", "student_name",
                            upper=True)
    if name_text:
        fs = 7.5
        while bold_obj.text_length(name_text, fontsize=fs) > 95 and fs > 5:
            fs -= 0.5
        tw = bold_obj.text_length(name_text, fontsize=fs)
        page.insert_text(((15 + 120) / 2 - tw / 2, 149.5), name_text,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=NAVY_COLOR, overlay=True)

    desig = _emp_value(student, "designation")
    if desig:
        fs = _fit_size(bold_obj, desig, 60.0, 5.2, 3.5)
        desig = _ellipsize_to_width(bold_obj, desig, 60.0, fs)
        page.insert_text((43.5, 156.0), desig,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=NAVY_COLOR, overlay=True)

    _fh_v2     = clean_card_value(_emp_value(student, "father_name", "fh_name") or "")
    _dob_v2    = clean_card_value(_emp_value(student, "dob") or "")
    _addr_v2   = clean_card_value(_emp_value(student, "address") or "")
    _mobile_v2 = clean_card_value(_emp_value(student, "mobile", "contact_no") or "")
    _addr2_longest = max(_addr_v2.split(), key=lambda w: bold_obj.text_length(w, fontsize=5.5), default=_addr_v2) if _addr_v2 else ""
    _DET2_BASE = 5.5
    _DET2_MIN  = 5.0
    _DET2_W    = 50.0
    _det2_fs = _DET2_BASE
    for _tv2 in (_fh_v2, _dob_v2, _mobile_v2, _addr2_longest):
        if _tv2:
            _det2_fs = min(_det2_fs, _fit_size(bold_obj, _tv2, _DET2_W, _DET2_BASE, _DET2_MIN))
    _det2_fs = max(_DET2_MIN, min(_DET2_BASE, _det2_fs))

    if _fh_v2:
        page.insert_text((56.8, 171.5), _fh_v2,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=_det2_fs, color=NAVY_COLOR, overlay=True)
    if _dob_v2:
        page.insert_text((56.8, 180.5), _dob_v2,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=_det2_fs, color=NAVY_COLOR, overlay=True)
    if _addr_v2:
        _addr2_lines, _ = wrap_and_shrink_text(bold_obj, _addr_v2, _DET2_W, 2, base_size=_det2_fs)
        for _ai2, _aline2 in enumerate(_addr2_lines):
            page.insert_text((56.8, 187.0 + _ai2 * (_det2_fs * 1.15)), _aline2,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=_det2_fs, color=NAVY_COLOR, overlay=True)
    if _mobile_v2:
        page.insert_text((56.8, 203.5), _mobile_v2,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=_det2_fs, color=NAVY_COLOR, overlay=True)

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
