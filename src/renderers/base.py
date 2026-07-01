import fitz
import logging
import threading
from src.config import (
    TEMPLATE_CONFIGS, DEFAULT_TEMPLATE,
    CARD_W_PT, CARD_H_PT
)
from src.utils.text import (
    _make_card_transform, _tr_rect, _tr_point
)


# School-specific student overlays and renderers
from src.renderers.hebron.student import draw_card_overlay_hebron
from src.renderers.hebron.employee import _render_hebron_emp_card_bytes

from src.renderers.redeemer.student import _render_redeemer_student_card_bytes, draw_card_overlay_redeemer
from src.renderers.redeemer.employee import _render_redeemer_emp_card_bytes

from src.renderers.priyanka.student import _render_priyanka_card_bytes
from src.renderers.priyanka.employee import _render_priyanka_emp_card_bytes

from src.renderers.ab_ascent.student import _render_ab_ascent_card_bytes
from src.renderers.ab_ascent.employee import _render_ab_ascent_emp_card_bytes
from src.renderers.jnanabharati.student import _render_jnanabharati_student_card_bytes

log = logging.getLogger("idcard.renderer.base")

# Caches and Locks
_template_bytes_cache = {}
_template_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}
_template_doc_cache = {}
_template_doc_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}
_template_preview_cache = {}
_template_preview_locks = {key: threading.Lock() for key in TEMPLATE_CONFIGS}

def normalize_template_key(value):
    key = str(value or DEFAULT_TEMPLATE).strip().lower()
    return key if key in TEMPLATE_CONFIGS else DEFAULT_TEMPLATE

def get_template_config(template_key=None):
    return TEMPLATE_CONFIGS[normalize_template_key(template_key)]

def _ensure_template(template_key: str = DEFAULT_TEMPLATE):
    template = get_template_config(template_key)
    key = template["key"]
    if key in _template_bytes_cache:
        return _template_bytes_cache[key]
    lock = _template_locks[key]
    with lock:
        if key in _template_bytes_cache:
            return _template_bytes_cache[key]
        pdf_path = template["pdf"]
        if not pdf_path.exists():
            return None
        with open(str(pdf_path), "rb") as fh:
            _template_bytes_cache[key] = fh.read()
        return _template_bytes_cache[key]

def _get_template_doc(template_key: str = DEFAULT_TEMPLATE):
    key = normalize_template_key(template_key)
    if key in _template_doc_cache:
        return _template_doc_cache[key]
    lock = _template_doc_locks[key]
    with lock:
        if key in _template_doc_cache:
            return _template_doc_cache[key]
        tmpl_bytes = _ensure_template(key)
        if tmpl_bytes is None:
            return None
        _template_doc_cache[key] = fitz.open("pdf", tmpl_bytes)
        return _template_doc_cache[key]

def _get_template_preview_png(template_key: str = DEFAULT_TEMPLATE):
    key = normalize_template_key(template_key)
    if key in _template_preview_cache:
        return _template_preview_cache[key]
    lock = _template_preview_locks[key]
    with lock:
        if key in _template_preview_cache:
            return _template_preview_cache[key]
        tmpl_bytes = _ensure_template(key)
        if tmpl_bytes is None:
            return None
        doc = fitz.open("pdf", tmpl_bytes)
        try:
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            _template_preview_cache[key] = pix.tobytes("png")
        finally:
            doc.close()
        return _template_preview_cache[key]

def _resolve_renderer_key(template_key: str) -> str:
    cfg = TEMPLATE_CONFIGS.get(template_key)
    if cfg and "renderer" in cfg:
        return cfg["renderer"]
    return template_key

EMP_CARD_RENDERERS = {
    "hebron_emp":    _render_hebron_emp_card_bytes,
    "ab_ascent_emp": _render_ab_ascent_emp_card_bytes,
    "redeemer_emp":  _render_redeemer_emp_card_bytes,
    "priyanka_emp":  _render_priyanka_emp_card_bytes,
}

def _resolve_card_renderer(template_key: str):
    if template_key in EMP_CARD_RENDERERS:
        return True, EMP_CARD_RENDERERS[template_key]
    rk = _resolve_renderer_key(template_key)
    if rk == "priyanka":
        return True, _render_priyanka_card_bytes
    if rk == "ab_ascent":
        return True, _render_ab_ascent_card_bytes
    if rk == "redeemer":
        return True, _render_redeemer_student_card_bytes
    if rk == "jnanabharati":
        return True, _render_jnanabharati_student_card_bytes
    return False, None

def draw_card_on_page(page, student, target_rect, template_key, template_doc, template_source_rect):
    if template_key in EMP_CARD_RENDERERS:
        try:
            tmpl_bytes_local = _ensure_template(template_key)
        except Exception:
            tmpl_bytes_local = None
        if tmpl_bytes_local:
            try:
                card_bytes = EMP_CARD_RENDERERS[template_key](student, tmpl_bytes_local)
                if card_bytes:
                    _card_doc = fitz.open("pdf", card_bytes)
                    try:
                        page.show_pdf_page(target_rect, _card_doc, 0,
                                            keep_proportion=False, overlay=True)
                    finally:
                        _card_doc.close()
                    return
            except Exception as _e:
                log.error("Employee per-card render failed for %s: %s", template_key, _e)

    page.show_pdf_page(target_rect, template_doc, 0, keep_proportion=False, overlay=True)
    tr = _make_card_transform(template_source_rect, target_rect)
    rk = _resolve_renderer_key(template_key)
    if rk == "redeemer":
        draw_card_overlay_redeemer(page, student, tr)
    else:
        draw_card_overlay_hebron(page, student, tr)
