import io
import logging
import fitz
from src.config import PHOTO_EMBED_SCALE, DEFAULT_SESSION
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size, _ellipsize_to_width,
    wrap_and_shrink_text
)
from src.utils.photo import (
    fetch_photo_bytes, insert_image_safe, HAS_PIL
)
from src.utils.pdf import _PDF_SAVE_OPTS

if HAS_PIL:
    from PIL import Image

log = logging.getLogger("idcard.renderer.priyanka.student")

def _render_priyanka_card_bytes(student: dict, tmpl_bytes: bytes):
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]

    _, bold_obj, _, bold_fn, _, fn_bold = ensure_fonts()
    if bold_obj is None:
        doc.close()
        return None

    PRIY_BLUE = (15/255, 0/255, 106/255)

    sample_value_rects = [
        ( 8.13, 130.69,  90.08, 140.73),
        (26.29, 141.29,  52.74, 148.32),
        (70.81, 141.29,  74.84, 148.32),
        (96.53, 141.29, 103.41, 148.32),
        (109.16, 110.50, 128.80, 117.11),
        (56.76, 155.18, 109.17, 161.88),
        (56.76, 162.73, 105.50, 169.42),
        (56.76, 170.27,  87.42, 176.97),
        (56.76, 178.56,  84.73, 185.26),
        (56.76, 186.56, 112.37, 193.25),
        (56.76, 194.35,  90.09, 201.05),
    ]
    for x0, y0, x1, y1 in sample_value_rects:
        page.add_redact_annot(fitz.Rect(x0 - 0.3, y0 - 0.3, x1 + 0.3, y1 + 0.3), fill=None)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    PAD = 2.2
    BOX_X, BOX_Y, BOX_W, BOX_H = 46.34, 57.75, 49.92, 63.95
    _inner_erase_x0 = BOX_X + PAD
    _inner_erase_y0 = BOX_Y + PAD
    _inner_erase_x1 = BOX_X + BOX_W - PAD
    _inner_erase_y1 = BOX_Y + BOX_H - PAD
    _erase_shape = page.new_shape()
    _erase_shape.draw_rect(fitz.Rect(_inner_erase_x0, _inner_erase_y0,
                                     _inner_erase_x1, _inner_erase_y1))
    _erase_shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0)
    _erase_shape.commit(overlay=True)
    page.add_redact_annot(
        fitz.Rect(_inner_erase_x0, _inner_erase_y0, _inner_erase_x1, _inner_erase_y1),
        fill=None
    )
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    box_inner_w = BOX_W - 2 * PAD
    box_inner_h = BOX_H - 2 * PAD
    photo_inner_rect = fitz.Rect(BOX_X + PAD, BOX_Y + PAD,
                                  BOX_X + BOX_W - PAD, BOX_Y + BOX_H - PAD)
    photo_bytes = fetch_photo_bytes(student.get("photo_url", ""))
    if photo_bytes and HAS_PIL:
        try:
            scale = PHOTO_EMBED_SCALE
            target_w = max(1, int(round(box_inner_w * scale)))
            target_h = max(1, int(round(box_inner_h * scale)))
            with Image.open(io.BytesIO(photo_bytes)) as _img:
                _rgb = _img.convert("RGB")
                _resized = _rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)
            _radius = max(4, int(box_inner_w * scale * 0.08))
            _rgba = _resized.convert("RGBA")
            _resized.close()
            _mask = Image.new("L", (target_w, target_h), 0)
            from PIL import ImageDraw as _IDraw
            _md = _IDraw.Draw(_mask)
            _md.rounded_rectangle((0, 0, target_w - 1, target_h - 1),
                                   radius=_radius, fill=255)
            _rgba.putalpha(_mask)
            _buf = io.BytesIO()
            _rgba.save(_buf, format="PNG")
            _rgba.close()
            _mask.close()
            page.insert_image(photo_inner_rect, stream=_buf.getvalue(),
                              keep_proportion=False, overlay=True)
        except Exception:
            insert_image_safe(page, photo_inner_rect, photo_bytes)
    else:
        insert_image_safe(page, photo_inner_rect, photo_bytes)

    def put(text, x, baseline_y, max_width, sz=6.0, min_sz=3.5):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        fs = _fit_size(bold_obj, val, max_width, sz, min_sz)
        page.insert_text((x, baseline_y), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=fs, color=PRIY_BLUE, overlay=True)

    name = clean_card_value(student.get("student_name", "")).upper()
    if name:
        sz = _fit_size(bold_obj, name, 100.0, 8.99, 4.0)
        page.insert_text((8.13, 138.6), name,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=sz, color=PRIY_BLUE, overlay=True)

    put(student.get("class", "").upper(),   26.29, 146.8, 30.0, 6.0)
    put(student.get("section", "").upper(), 70.81, 146.8, 12.0, 6.0)
    put(student.get("roll", ""),            96.53, 146.8, 18.0, 6.0)

    sess = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    sz = _fit_size(bold_obj, sess, 28.0, 6.0, 3.5)
    page.insert_text((109.16, 115.5), sess,
                     fontname=fn_bold, fontfile=bold_fn,
                     fontsize=sz, color=PRIY_BLUE, overlay=True)

    _DETAIL_BASE  = 6.0
    _DETAIL_MIN   = 3.8
    _DETAIL_W     = 80.0
    _ADDR_W       = 80.0

    _father_v = clean_card_value(str(student.get("father_name") or ""))
    _mother_v = clean_card_value(str(student.get("mother_name") or ""))
    _dob_v    = clean_card_value(str(student.get("dob") or ""))
    _mobile_v = clean_card_value(str(student.get("mobile") or ""))
    _addr_v   = clean_card_value(str(student.get("address") or ""))
    _addr_longest = max(_addr_v.split(), key=lambda w: bold_obj.text_length(w, fontsize=_DETAIL_BASE), default=_addr_v)

    _unif_fs = _DETAIL_BASE
    for _tv in (_father_v, _mother_v, _dob_v, _mobile_v, _addr_longest):
        if _tv:
            _unif_fs = min(_unif_fs, _fit_size(bold_obj, _tv, _DETAIL_W, _DETAIL_BASE, _DETAIL_MIN))

    def _put_uniform(text, x, y):
        val = clean_card_value(str(text) if text else "")
        if not val:
            return
        page.insert_text((x, y), val,
                         fontname=fn_bold, fontfile=bold_fn,
                         fontsize=_unif_fs, color=PRIY_BLUE, overlay=True)

    _put_uniform(_father_v, 56.76, 160.4)
    _put_uniform(_mother_v, 56.76, 168.4)
    _put_uniform(_dob_v, 56.76, 176.4)

    if _addr_v:
        _addr_lines, _ = wrap_and_shrink_text(bold_obj, _addr_v, _ADDR_W, 2, base_size=_unif_fs)
        for _ai, _aline in enumerate(_addr_lines):
            page.insert_text((56.76, 183.6 + _ai * (_unif_fs * 1.15)), _aline,
                             fontname=fn_bold, fontfile=bold_fn,
                             fontsize=_unif_fs, color=PRIY_BLUE, overlay=True)

    _contact_y = 183.6 + 2 * (_unif_fs * 1.15) + 1.5
    _put_uniform(_mobile_v, 56.76, max(200.0, _contact_y))

    buf = io.BytesIO()
    _save_opts = dict(_PDF_SAVE_OPTS)
    _save_opts.pop("linear", None)
    try:
        doc.save(buf, **_save_opts)
    except TypeError:
        doc.save(buf, garbage=4, deflate=True, clean=True, incremental=False)
    doc.close()
    return buf.getvalue()
