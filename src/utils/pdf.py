import os
import io
import gc
import re
import time
import zipfile
import logging
import tempfile
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    import pikepdf
    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False

try:
    from PIL import Image, ImageDraw, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from src.config import (
    CARD_W_PT, CARD_H_PT, A4_W_PT, A4_H_PT,
    A4_W_MM, A4_H_MM, CARD_W_MM, CARD_H_MM, ROW_GAP_MM, OFFSET_X_MM, OFFSET_Y_MM,
    OX_PT, OY_PT, COL_STEP, ROW_STEP, ROW_GAP_PT, COLS, ROWS, CARDS_PER_PAGE,
    PDF_TEMP_DIR, CARD_RENDER_WORKERS, ZIP_BUILD_WORKERS, PREFETCH_WORKERS,
    DOWNLOAD_DPI, CHUNK_PAGES, DEFAULT_TEMPLATE, MM_TO_PT,
    TEMPLATE_CONFIGS
)

from src.utils.text import ensure_fonts, clean_card_value
from src.utils.photo import fetch_photo_bytes, prepare_photo_for_rect_cover
from src.jobs import job_set, schedule_delete

log = logging.getLogger("idcard.pdf")

# Detect PyMuPDF capabilities dynamically
def _probe_pymupdf_save_flags() -> dict:
    if not HAS_FITZ:
        return {}
    log.info("[pdf-debug] PyMuPDF version: %s", fitz.version)
    base = dict(
        deflate=True, deflate_images=True,
        garbage=4, clean=True, incremental=False,
    )
    _tmp = fitz.open()
    _tmp.new_page()
    _tp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    _tp.close()
    try:
        _tmp.save(_tp.name, pdf_version=14, deflate=True, garbage=4, clean=True, incremental=False)
        base["pdf_version"] = 14
        log.info("[pdf-debug] pdf_version=14 is SUPPORTED by this build")
    except Exception as e:
        log.warning("[pdf-debug] pdf_version NOT supported (%s) — omitting", e)
    finally:
        _tmp.close()
        try: os.unlink(_tp.name)
        except: pass

    _tmp2 = fitz.open()
    _tmp2.new_page()
    _tp2 = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    _tp2.close()
    try:
        _tmp2.save(_tp2.name, linear=True, deflate=True, garbage=4, clean=True, incremental=False)
        base["linear"] = True
        log.info("[pdf-debug] linear=True is SUPPORTED by this build")
    except Exception as e:
        log.warning("[pdf-debug] linear NOT supported (%s) — omitting", e)
    finally:
        _tmp2.close()
        try: os.unlink(_tp2.name)
        except: pass

    log.info("[pdf-debug] Final _PDF_SAVE_OPTS: %s", base)
    return base

_PDF_SAVE_OPTS = _probe_pymupdf_save_flags()


def _safe_save(doc, path: str):
    log.debug("[pdf-debug] _safe_save -> %s | opts=%s", path, _PDF_SAVE_OPTS)
    try:
        doc.save(path, **_PDF_SAVE_OPTS)
        _log_saved_pdf_info(path, "_safe_save")
    except Exception as e:
        log.error("[pdf-debug] _safe_save FAILED with probed opts: %s — trying bare fallback", e)
        doc.save(path, deflate=True, garbage=4, clean=True, incremental=False)
        _log_saved_pdf_info(path, "_safe_save-bare-fallback")


def _log_saved_pdf_info(path: str, label: str = ""):
    try:
        size_kb = os.path.getsize(path) / 1024
        doc = fitz.open(path)
        pages = doc.page_count
        try:
            ver = doc.pdf_version()
        except AttributeError:
            ver = "unknown"

        try:
            with open(path, "rb") as f:
                header_bytes = f.read(1024)
            is_linear = b"/Linearized" in header_bytes
        except Exception:
            is_linear = "unknown"

        try:
            xref_ok = doc.xref_length() > 0
        except Exception:
            xref_ok = "unknown"

        doc.close()
        log.info(
            "[pdf-debug] SAVED PDF [%s]: version=%s | pages=%d | "
            "size=%.1f KB | linearized=%s | xref_ok=%s | path=%s",
            label, ver, pages, size_kb, is_linear, xref_ok, path
        )
    except Exception as e:
        log.error("[pdf-debug] _log_saved_pdf_info FAILED for %s: %s", path, e)


def _check_tmp_space_mb(path: str, needed_mb: float = 20.0) -> bool:
    try:
        import shutil
        free = shutil.disk_usage(path).free / (1024 * 1024)
        return free >= needed_mb
    except Exception:
        return True


def _resolve_pdf_tmp_dir() -> str:
    candidates = [PDF_TEMP_DIR, "/tmp", tempfile.gettempdir()]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            if _check_tmp_space_mb(d, needed_mb=10.0):
                t = tempfile.NamedTemporaryFile(delete=True, dir=d, suffix=".pdf")
                t.close()
                return d
        except Exception:
            continue
    return tempfile.gettempdir()


def _flatten_pdf_to_images(path: str, dpi: int = 300) -> bool:
    if not HAS_FITZ:
        log.warning("[flatten] PyMuPDF not available")
        return False
    tmp_flat = path + ".flat.tmp"
    try:
        if os.path.exists(tmp_flat):
            os.unlink(tmp_flat)
    except Exception:
        pass
    try:
        src_doc = fitz.open(path)
        n_pages = src_doc.page_count
        log.info("[flatten] rasterising %d page(s) at %d dpi → %s", n_pages, dpi, path)

        flat_merger = fitz.open()

        for page_idx in range(n_pages):
            page = src_doc[page_idx]
            pw, ph = page.rect.width, page.rect.height
            effective_dpi = dpi
            scale = effective_dpi / 72.0
            mat   = fitz.Matrix(scale, scale)

            pix       = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csRGB)
            img_bytes = pix.tobytes("png")
            pix = None

            one_page_doc = fitz.open()
            one_page     = one_page_doc.new_page(width=pw, height=ph)
            one_page.insert_image(one_page.rect, stream=img_bytes, keep_proportion=False)
            img_bytes = None

            flat_merger.insert_pdf(one_page_doc)
            one_page_doc.close()
            del one_page_doc
            gc.collect()

        src_doc.close()
        flat_merger.save(
            tmp_flat,
            deflate=True, deflate_images=True,
            garbage=4, clean=True, incremental=False,
        )
        flat_merger.close()
        gc.collect()

        os.replace(tmp_flat, path)
        log.info("[flatten] done — %.1f KB: %s", os.path.getsize(path) / 1024, path)
        return True
    except Exception as e:
        log.error("[flatten] FAILED: %s — %s", path, e)
        try:
            if os.path.exists(tmp_flat):
                os.unlink(tmp_flat)
        except Exception:
            pass
        return False


def _pikepdf_downgrade_to_14(path: str) -> bool:
    flatten_ok = _flatten_pdf_to_images(path)
    if not flatten_ok:
        log.warning("[pikepdf] flatten step failed")

    try:
        file_size = os.path.getsize(path)
        if file_size < 500:
            log.error("[pikepdf] file too small after flatten")
            return False
        with open(path, "rb") as _f:
            header = _f.read(4)
        if header != b"%PDF":
            log.error("[pikepdf] file does not start with %PDF")
            return False
    except Exception as check_err:
        log.error("[pikepdf] pre-pikepdf validity check failed: %s", check_err)
        return False

    if not HAS_PIKEPDF:
        log.warning("[pikepdf] pikepdf not installed — stays PDF 1.7")
        return False

    tmp_v14 = path + ".v14.tmp"
    try:
        with pikepdf.open(path) as pdf:
            pdf.save(
                tmp_v14,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.disable,
                linearize=False,
                force_version="1.4",
                recompress_flate=True,
            )
        v14_size = os.path.getsize(tmp_v14)
        if v14_size < 500:
            raise RuntimeError(f"pikepdf output too small: {v14_size} bytes")
        os.replace(tmp_v14, path)
        log.info("[pikepdf] PDF 1.4 pinned — %.1f KB: %s", os.path.getsize(path) / 1024, path)
        return True
    except TypeError:
        try:
            with pikepdf.open(path) as pdf:
                pdf.save(tmp_v14,
                         compress_streams=True,
                         object_stream_mode=pikepdf.ObjectStreamMode.disable,
                         linearize=False,
                         force_version="1.4")
            v14_size = os.path.getsize(tmp_v14)
            if v14_size < 500:
                raise RuntimeError("pikepdf output too small")
            os.replace(tmp_v14, path)
            log.info("[pikepdf] PDF 1.4 pinned (no recompress) — %.1f KB: %s",
                     os.path.getsize(path) / 1024, path)
            return True
        except Exception as e2:
            log.error("[pikepdf] version-pin fallback failed: %s", e2)
            try:
                if os.path.exists(tmp_v14):
                    os.unlink(tmp_v14)
            except Exception:
                pass
            return False
    except Exception as e:
        log.error("[pikepdf] version-pin failed: %s", e)
        try:
            if os.path.exists(tmp_v14):
                os.unlink(tmp_v14)
        except Exception:
            pass
        return False


def draw_serial_badge_vector(page, serial: int, cx: float, cy: float, gap_h: float):
    txt    = f"#{serial}"
    fs     = max(5.0, gap_h * 0.38)
    _, bold_obj, _, bold_fn, _, fn_bold = ensure_fonts()
    if bold_obj is None:
        return
    tw     = bold_obj.text_length(txt, fontsize=fs)
    pad_x  = 3.5
    pad_y  = 2.2
    rx0    = cx - tw / 2.0 - pad_x
    ry0    = cy - fs / 2.0 - pad_y
    rx1    = cx + tw / 2.0 + pad_x
    ry1    = cy + fs / 2.0 + pad_y + 0.5
    rect   = fitz.Rect(rx0, ry0, rx1, ry1)

    page.draw_rect(rect, color=(0.14, 0.25, 0.78), fill=(0.94, 0.96, 1.0), width=0.7, overlay=True)

    baseline = ry0 + (ry1 - ry0 - fs) / 2.0 + fs * bold_obj.ascender - 0.4
    page.insert_text(
        (cx - tw / 2.0, baseline), txt,
        fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
        fontsize=fs, color=(0.14, 0.25, 0.78), overlay=True,
    )


def _render_a4_page(out_doc, page_idx: int, students: list,
                    template_key: str, template_doc, source_rect,
                    tmpl_bytes: bytes, use_per_card: bool, render_fn):
    from src.renderers.base import draw_card_on_page

    student_start = page_idx * CARDS_PER_PAGE
    student_batch = students[student_start: student_start + CARDS_PER_PAGE]
    a4_page = out_doc.new_page(width=A4_W_PT, height=A4_H_PT)

    batch_rendered = None
    if use_per_card and render_fn:
        batch_workers = min(CARD_RENDER_WORKERS, len(student_batch))
        if batch_workers > 1:
            batch_rendered = [None] * len(student_batch)
            with ThreadPoolExecutor(max_workers=batch_workers) as pool:
                fut_map = {
                    pool.submit(render_fn, student_batch[i], tmpl_bytes): i
                    for i in range(len(student_batch))
                }
                for f in as_completed(fut_map):
                    bi = fut_map[f]
                    try:
                        batch_rendered[bi] = f.result()
                    except Exception as e:
                        log.error("Card render FAILED student[%d]: %s", student_start + bi, e)
                        batch_rendered[bi] = None
        else:
            batch_rendered = []
            for s in student_batch:
                try:
                    batch_rendered.append(render_fn(s, tmpl_bytes))
                except Exception as e:
                    log.error("Card render FAILED: %s", e)
                    batch_rendered.append(None)

    for idx, student in enumerate(student_batch):
        col = idx % COLS
        row = idx // COLS
        card_x = OX_PT + col * COL_STEP
        card_y = OY_PT + row * ROW_STEP
        target_rect = fitz.Rect(card_x, card_y, card_x + CARD_W_PT, card_y + CARD_H_PT)

        if use_per_card:
            card_bytes = batch_rendered[idx]
            if card_bytes:
                card_doc = fitz.open("pdf", card_bytes)
                a4_page.show_pdf_page(target_rect, card_doc, 0,
                                      keep_proportion=False, overlay=True)
                card_doc.close()
            batch_rendered[idx] = None
        else:
            draw_card_on_page(
                a4_page, student, target_rect, template_key,
                template_doc=template_doc, template_source_rect=source_rect,
            )

        if ROWS > 1:
            if row == 0:
                gap_cy = card_y + CARD_H_PT + ROW_GAP_PT / 2.0
            else:
                gap_cy = card_y - ROW_GAP_PT / 2.0
            badge_cx = card_x + CARD_W_PT / 2.0
            draw_serial_badge_vector(
                a4_page, student_start + idx + 1,
                badge_cx, gap_cy, ROW_GAP_PT,
            )

    if batch_rendered is not None:
        batch_rendered.clear()


def build_pdf_file_vector(students: list, template_key: str = DEFAULT_TEMPLATE,
                          progress_cb=None):
    from src.renderers.base import (
        _resolve_card_renderer, _resolve_renderer_key,
        _ensure_template, _get_template_doc, normalize_template_key
    )

    if not HAS_FITZ:
        log.error("build_pdf_file_vector: PyMuPDF not installed")
        return None
    template_key = normalize_template_key(template_key)
    tmpl_bytes = _ensure_template(template_key)
    if tmpl_bytes is None:
        log.error("build_pdf_file_vector: template PDF not found for key='%s'", template_key)
        return None

    template_doc = _get_template_doc(template_key)
    if template_doc is None:
        log.error("build_pdf_file_vector: could not open template doc")
        return None

    _kind = "employees" if str(template_key).endswith("_emp") else "students"
    log.info("build_pdf_file_vector: %d %s, template=%s, chunk_pages=%d",
             len(students), _kind, template_key, CHUNK_PAGES)

    source_rect = fitz.Rect(template_doc[0].rect)
    n_pages = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    tmp_dir = _resolve_pdf_tmp_dir()

    tmp_final = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=tmp_dir)
    tmp_final.close()
    out_path = tmp_final.name

    use_per_card, render_fn = _resolve_card_renderer(template_key)
    if not use_per_card:
        render_fn = None

    chunk_paths = []
    chunk_doc = fitz.open()
    pages_in_chunk = 0

    def _flush_chunk():
        nonlocal chunk_doc, pages_in_chunk
        if pages_in_chunk == 0:
            chunk_doc.close()
            chunk_doc = fitz.open()
            return
        cp = tempfile.NamedTemporaryFile(delete=False, suffix=".chunk.pdf", dir=tmp_dir)
        cp.close()
        try:
            _safe_save(chunk_doc, cp.name)
            chunk_paths.append(cp.name)
        finally:
            chunk_doc.close()
            chunk_doc = fitz.open()
            pages_in_chunk = 0
            gc.collect()

    try:
        for page_idx in range(n_pages):
            _render_a4_page(
                chunk_doc, page_idx, students, template_key,
                template_doc, source_rect, tmpl_bytes,
                use_per_card, render_fn,
            )
            pages_in_chunk += 1
            if pages_in_chunk >= CHUNK_PAGES:
                _flush_chunk()
            if progress_cb:
                try: progress_cb(page_idx + 1, n_pages)
                except Exception: pass

        if pages_in_chunk > 0:
            _flush_chunk()
        else:
            chunk_doc.close()

        # Merge Chunks
        merger = fitz.open()
        for idx, cp in enumerate(chunk_paths):
            c_doc = fitz.open(cp)
            merger.insert_pdf(c_doc)
            c_doc.close()
            os.unlink(cp)
            gc.collect()

        _safe_save(merger, out_path)
        merger.close()
        return out_path
    except Exception as e:
        log.error("build_pdf_file_vector failed: %s", e)
        for cp in chunk_paths:
            try: os.unlink(cp)
            except: pass
        try: os.unlink(out_path)
        except: pass
        return None


def _placeholder_card_pil(student, dpi=150):
    if not HAS_PIL:
        return None
    w = int(55 / 25.4 * dpi); h = int(86 / 25.4 * dpi)
    img  = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, int(h*0.3)], fill=(200, 30, 30))
    name = student.get("student_name","Student").upper()
    draw.text((10, 10), name, fill="white")
    draw.text((10, int(h*0.35)), f"Class: {student.get('class','')}", fill=(100,100,100))
    return img


def build_pdf_file_raster_fallback(students, dpi=150):
    if not HAS_FITZ or not HAS_PIL:
        return None

    def mm2px(mm): return int(round(mm / 25.4 * dpi))
    a4_w_px  = mm2px(A4_W_MM); a4_h_px   = mm2px(A4_H_MM)
    card_w_px= mm2px(CARD_W_MM); card_h_px = mm2px(CARD_H_MM)
    ox_px    = mm2px(OFFSET_X_MM); oy_px    = mm2px(OFFSET_Y_MM)
    gap_px   = mm2px(ROW_GAP_MM); col_gap_px= mm2px(1.0)
    a4_w_pt  = A4_W_MM * MM_TO_PT; a4_h_pt  = A4_H_MM * MM_TO_PT

    out_doc  = fitz.open()
    _tmp_dir = _resolve_pdf_tmp_dir()
    tmp      = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=_tmp_dir)
    tmp.close()
    n_pages  = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE

    try:
        for page_idx in range(n_pages):
            batch = students[page_idx * CARDS_PER_PAGE : (page_idx+1) * CARDS_PER_PAGE]
            sheet = Image.new("RGB", (a4_w_px, a4_h_px), (245,245,245))
            for idx, s in enumerate(batch):
                col   = idx % COLS; row = idx // COLS
                x     = ox_px + col * (card_w_px + col_gap_px)
                y     = oy_px + row * (card_h_px + gap_px)
                card  = _placeholder_card_pil(s, dpi)
                if card:
                    sheet.paste(card.resize((card_w_px, card_h_px)), (x, y))
                    card.close()
            buf = io.BytesIO()
            sheet.save(buf, format="JPEG", quality=80, optimize=True)
            sheet.close()
            pg = out_doc.new_page(width=a4_w_pt, height=a4_h_pt)
            pg.insert_image(fitz.Rect(0,0,a4_w_pt,a4_h_pt), stream=buf.getvalue(), overlay=True, keep_proportion=False)
            gc.collect()
        out_doc.save(tmp.name, deflate=True, garbage=4, clean=True, linear=True, pdf_version=14)
        return tmp.name
    except Exception:
        try:
            if os.path.exists(tmp.name): os.unlink(tmp.name)
        except: pass
        raise
    finally:
        out_doc.close()
        gc.collect()


def build_id_card_size_pdf(record: dict, template_key: str = DEFAULT_TEMPLATE, skip_flatten=False) -> str:
    from src.renderers.base import (
        _resolve_card_renderer, _ensure_template, _get_template_doc,
        draw_card_on_page, normalize_template_key
    )

    if not HAS_FITZ:
        return None

    template_key = normalize_template_key(template_key)
    tmpl_bytes   = _ensure_template(template_key)
    tmp_dir      = _resolve_pdf_tmp_dir()
    tmp          = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=tmp_dir)
    tmp.close()
    out_path = tmp.name

    try:
        # Path 1: per-card renderer
        use_per_card, render_fn = _resolve_card_renderer(template_key)
        if use_per_card and render_fn and tmpl_bytes:
            card_bytes = render_fn(record, tmpl_bytes)
            if card_bytes:
                card_doc = fitz.open("pdf", card_bytes)
                try:
                    _safe_save(card_doc, out_path)
                finally:
                    card_doc.close()
                if not skip_flatten:
                    _pikepdf_downgrade_to_14(out_path)
                return out_path

        # Path 2: overlay renderer (Hebron student)
        template_doc  = _get_template_doc(template_key)
        source_rect   = fitz.Rect(template_doc[0].rect) if template_doc else fitz.Rect(0, 0, CARD_W_PT, CARD_H_PT)
        from src.jobs import _fitz_render_lock
        with _fitz_render_lock:
            out_doc   = fitz.open()
            card_page = out_doc.new_page(width=CARD_W_PT, height=CARD_H_PT)
            target_rect = fitz.Rect(0, 0, CARD_W_PT, CARD_H_PT)
            draw_card_on_page(
                card_page, record, target_rect, template_key,
                template_doc=template_doc, template_source_rect=source_rect,
            )
            try:
                _safe_save(out_doc, out_path)
            finally:
                out_doc.close()
        if not skip_flatten:
            _pikepdf_downgrade_to_14(out_path)
        return out_path

    except Exception as e:
        log.error("build_id_card_size_pdf FAILED for '%s': %s",
                  record.get("student_name") or record.get("employee_name", "?"), e)
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except Exception:
            pass
        return None


def build_pdf_file(students, dpi=150, template_key: str = DEFAULT_TEMPLATE):
    from src.renderers.base import get_template_config
    template = get_template_config(template_key)
    if HAS_FITZ and template["pdf"].exists():
        return build_pdf_file_vector(students, template_key=template["key"])
    return build_pdf_file_raster_fallback(students, dpi=dpi)


def _pdf_to_png_bytes(pdf_path: str, dpi: int = 600) -> bytes:
    if not HAS_FITZ:
        log.warning("[png] PyMuPDF not installed")
        return None
    if not pdf_path or not os.path.exists(pdf_path):
        log.warning("[png] file missing: %s", pdf_path)
        return None
    try:
        scale = dpi / 72.0
        from src.jobs import _fitz_render_lock
        with _fitz_render_lock:
            doc  = fitz.open(pdf_path)
            page = doc[0]
            pix  = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            doc.close()
            raw    = bytes(pix.samples)
            pw, ph = pix.width, pix.height
            del pix
        if not HAS_PIL:
            return None
        img = Image.frombytes("RGB", [pw, ph], raw)
        del raw
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img); img = bg; del bg
        img = img.filter(ImageFilter.UnsharpMask(radius=0.5, percent=60, threshold=3))
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1, optimize=False)
        img.close()
        result = buf.getvalue()
        return result
    except Exception as e:
        log.error("[png] FAILED %s: %s\n%s", pdf_path, e, traceback.format_exc())
        return None


def run_job(jid: str, students: list, template_key: str, download_name: str):
    job_set(jid, status="running", phase="prefetch", started_at=time.time())
    try:
        try:
            urls = list({s.get("photo_url","").strip()
                         for s in students if s.get("photo_url","").strip()})
            total_urls = max(1, len(urls))
            done_urls = 0
            if urls:
                workers = min(PREFETCH_WORKERS, len(urls))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [pool.submit(fetch_photo_bytes, u) for u in urls]
                    for f in as_completed(futs):
                        done_urls += 1
                        try: f.result()
                        except Exception: pass
                        job_set(jid,
                                phase="prefetch",
                                progress=round(30.0 * done_urls / total_urls, 1))
            else:
                job_set(jid, progress=30.0)
        except Exception as e:
            log.warning("job %s prefetch error (non-fatal): %s", jid, e)
            job_set(jid, progress=30.0)

        n_total_pages = (len(students) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE

        def _on_page(done_pages, total_pages):
            pct = 30.0 + 62.0 * done_pages / max(1, total_pages)
            job_set(jid, phase="render",
                     progress=round(pct, 1),
                     done=min(len(students), done_pages * CARDS_PER_PAGE))

        job_set(jid, phase="render", progress=30.0)

        if HAS_FITZ:
            out_path = build_pdf_file_vector(
                students, template_key=template_key, progress_cb=_on_page,
            )
        else:
            out_path = build_pdf_file(students, dpi=DOWNLOAD_DPI, template_key=template_key)

        if not out_path:
            raise RuntimeError("PDF build failed")

        job_set(jid, phase="writing", progress=96.0)

        size = os.path.getsize(out_path)
        job_set(jid, status="done", phase="done", progress=100.0,
                 file_path=out_path, file_size=size,
                 download_name=download_name,
                 finished_at=time.time(),
                 done=len(students))
        log.info("job %s done: %s (%.1f KB)", jid, out_path, size / 1024.0)
    except Exception as e:
        log.error("job %s FAILED: %s\n%s", jid, e, traceback.format_exc())
        job_set(jid, status="error", phase="error", error=str(e),
                 finished_at=time.time())


def run_zip_job(jid: str, records: list, template_key: str, download_name: str,
                name_field: str = "student_name", group_field: str = "class",
                output_format: str = "pdf"):
    job_set(jid, status="running", phase="prefetch", started_at=time.time())
    use_png = (output_format == "jpeg")
    try:
        urls = list({r.get("photo_url","").strip() for r in records if r.get("photo_url","").strip()})
        total_urls = max(1, len(urls))
        done_urls  = 0
        if urls:
            workers = min(PREFETCH_WORKERS, len(urls))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(fetch_photo_bytes, u) for u in urls]
                for f in as_completed(futs):
                    done_urls += 1
                    try: f.result()
                    except Exception: pass
                    job_set(jid, phase="prefetch",
                             progress=round(30.0 * done_urls / total_urls, 1))
        else:
            job_set(jid, progress=30.0)

        job_set(jid, phase="render", progress=30.0)
        total = max(1, len(records))
        tmp_dir = _resolve_pdf_tmp_dir()
        zip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=tmp_dir)
        zip_tmp.close()
        used_names = {}

        def _build_one(record):
            rname = (record.get(name_field) or "?").strip()
            try:
                pdf_path = build_id_card_size_pdf(
                    record, template_key=template_key,
                    skip_flatten=use_png,
                )
                if not pdf_path or not Path(pdf_path).exists():
                    log.warning("[zip-build] build_id_card_size_pdf returned no file for '%s'", rname)
                    return None, None
                if use_png:
                    png_bytes = _pdf_to_png_bytes(pdf_path, dpi=600)
                    try: Path(pdf_path).unlink(missing_ok=True)
                    except Exception: pass
                    return None, png_bytes
                return pdf_path, None
            except Exception as _exc:
                log.error("[zip-build] _build_one EXCEPTION for '%s': %s", rname, _exc)
                return None, None

        n_workers = min(ZIP_BUILD_WORKERS, len(records))
        with zipfile.ZipFile(zip_tmp.name, mode="w",
                             compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                fut_to_idx = {pool.submit(_build_one, rec): i
                              for i, rec in enumerate(records)}
                results = [None] * len(records)
                done_count = 0
                for f in as_completed(fut_to_idx):
                    idx = fut_to_idx[f]
                    done_count += 1
                    try:
                        results[idx] = f.result()
                    except Exception as exc:
                        log.error("ZIP job %s: render failed for record[%d]: %s", jid, idx, exc)
                        results[idx] = (None, None)
                    pct = 30.0 + 66.0 * done_count / total
                    job_set(jid, phase="render", progress=round(pct, 1), done=done_count)

            for idx, record in enumerate(records):
                raw_name    = (record.get(name_field) or f"record_{idx+1}").strip()
                group_label = (record.get(group_field) or "unknown").strip().upper()
                safe        = re.sub(r"[^\w\-]", "_", raw_name)
                ext         = "png" if use_png else "pdf"
                base_name   = f"{group_label}_{safe}.{ext}"
                if base_name in used_names:
                    used_names[base_name] += 1
                    base_name = f"{group_label}_{safe}_{used_names[base_name]}.{ext}"
                else:
                    used_names[base_name] = 1

                res = results[idx]
                if res is None:
                    continue
                pdf_path, png_bytes = res
                if use_png:
                    if png_bytes:
                        zf.writestr(base_name, png_bytes)
                else:
                    if pdf_path and Path(pdf_path).exists():
                        zf.write(pdf_path, arcname=base_name)
                        try: Path(pdf_path).unlink(missing_ok=True)
                        except Exception: pass

        size = os.path.getsize(zip_tmp.name)
        job_set(jid, status="done", phase="done", progress=100.0,
                 file_path=zip_tmp.name, file_size=size,
                 download_name=download_name, finished_at=time.time(),
                 done=len(records))
        log.info("ZIP job %s done: %s (%.1f KB)", jid, zip_tmp.name, size / 1024.0)

    except Exception as e:
        log.error("ZIP job %s FAILED: %s\n%s", jid, e, traceback.format_exc())
        job_set(jid, status="error", phase="error", error=str(e), finished_at=time.time())


def _sanitize_filename(name):
    keep = []
    for ch in str(name or "file.pdf"):
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    cleaned = "".join(keep).strip("._") or "file"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def _external_storage_enabled():
    from src.config import (
        STORAGE_BACKEND, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_BUCKET,
        GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, GOOGLE_DRIVE_REFRESH_TOKEN
    )
    if STORAGE_BACKEND == "supabase":
        return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_BUCKET)
    if STORAGE_BACKEND == "google_drive":
        return bool(GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN)
    return False


def _google_access_token():
    import requests
    from src.config import GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, GOOGLE_DRIVE_REFRESH_TOKEN
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     GOOGLE_DRIVE_CLIENT_ID,
            "client_secret": GOOGLE_DRIVE_CLIENT_SECRET,
            "refresh_token": GOOGLE_DRIVE_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
        timeout=20,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Google Drive token refresh failed")
    return token


def _upload_to_google_drive(local_path, download_name):
    import requests
    import json
    from src.config import GOOGLE_DRIVE_FOLDER_ID
    token    = _google_access_token()
    metadata = {"name": _sanitize_filename(download_name)}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
    start = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name",
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/pdf",
        },
        data=json.dumps(metadata),
        timeout=30,
    )
    start.raise_for_status()
    session_url = start.headers.get("Location")
    if not session_url:
        raise RuntimeError("Google Drive resumable upload URL missing")
    file_size = os.path.getsize(local_path)
    with open(local_path, "rb") as fh:
        uploaded = requests.put(
            session_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/pdf",
                "Content-Length": str(file_size),
            },
            data=fh,
            timeout=300,
        )
    uploaded.raise_for_status()
    file_id = uploaded.json().get("id")
    if not file_id:
        raise RuntimeError("Google Drive file id missing")
    requests.post(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"fields": "id"},
        data=json.dumps({"role": "reader", "type": "anyone"}),
        timeout=30,
    ).raise_for_status()
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def _upload_to_supabase(local_path, download_name):
    import requests
    import json
    import uuid
    from src.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_BUCKET, SUPABASE_SIGNED_URL_TTL
    object_name = f"generated/{uuid.uuid4().hex}_{_sanitize_filename(download_name)}"
    upload_url  = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_name}"
    with open(local_path, "rb") as fh:
        requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "apikey":        SUPABASE_SERVICE_ROLE_KEY,
                "x-upsert":      "true",
                "Content-Type":  "application/pdf",
            },
            data=fh,
            timeout=300,
        ).raise_for_status()
    sign = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{SUPABASE_BUCKET}/{object_name}",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey":        SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type":  "application/json",
        },
        data=json.dumps({"expiresIn": SUPABASE_SIGNED_URL_TTL}),
        timeout=30,
    )
    sign.raise_for_status()
    payload = sign.json()
    signed  = payload.get("signedURL") or payload.get("signedUrl")
    if not signed:
        raise RuntimeError("Supabase signed URL missing")
    return signed if signed.startswith("http") else f"{SUPABASE_URL}/storage/v1{signed}"


def upload_pdf_to_external_storage(local_path, download_name):
    from src.config import STORAGE_BACKEND
    if STORAGE_BACKEND == "google_drive":
        return _upload_to_google_drive(local_path, download_name)
    if STORAGE_BACKEND == "supabase":
        return _upload_to_supabase(local_path, download_name)
    return None

