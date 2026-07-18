#!/usr/bin/env python3
"""
Test script for Redeemer student overlay rendering.
This script tests if the Redeemer student overlay is working correctly
by generating a sample ID card with test data.
"""

import sys
import io
import fitz
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import (
    TEMPLATE_PDF_REDEEMER, REDEEMER_PHOTO_RECT_COORDS,
    REDEEMER_PHOTO_OUTER_RECT, REDEEMER_NAME_BASELINE_Y,
    REDEEMER_CLASS_BASELINE_Y, REDEEMER_CLASS_VALUE_BASELINE_Y,
    REDEEMER_FATHER_BASELINE_Y, REDEEMER_MOBILE_BASELINE_Y,
    REDEEMER_ADDRESS_BASELINE_Y, REDEEMER_SESSION_VALUE_RECT,
    REDEEMER_SESSION_FONT_SIZE, REDEEMER_NAME_FONT_SIZE,
    REDEEMER_NAME_MIN_SIZE, REDEEMER_CLASS_FONT_SIZE,
    REDEEMER_VALUE_FONT_SIZE, REDEEMER_ADDRESS_LINE_GAP,
    REDEEMER_WHITE, REDEEMER_BLACK, PHOTO_EMBED_SCALE,
    DEFAULT_SESSION, REDEEMER_SESSION_CLEAN_COORDS
)
from src.utils.text import (
    ensure_fonts, clean_card_value, _fit_size,
    _ellipsize_to_width, _put_single, wrap_and_shrink_text
)
from src.utils.photo import (
    prepare_photo_for_rect_cover, fetch_photo_bytes, insert_image_safe
)

def test_redeemer_overlay():
    """Test the Redeemer student overlay with sample data."""
    
    # Sample student data
    student = {
        "student_name": "Test Student",
        "class": "10A",
        "section": "A",
        "roll": "15",
        "father_name": "Test Father",
        "mobile": "9876543210",
        "address": "123 Test Street, Test City",
        "dob": "15-01-2008",
        "session": "2024-25",
        "photo_url": "",  # Empty for test
    }
    
    print("Testing Redeemer student overlay...")
    print(f"Student: {student['student_name']}")
    print(f"Class: {student['class']}")
    
    # Load template
    if not TEMPLATE_PDF_REDEEMER.exists():
        print(f"ERROR: Template not found: {TEMPLATE_PDF_REDEEMER}")
        return False
    
    with open(str(TEMPLATE_PDF_REDEEMER), "rb") as f:
        tmpl_bytes = f.read()
    
    doc = fitz.open("pdf", tmpl_bytes)
    page = doc[0]
    
    # Get fonts
    anton_obj, bold_obj, anton_fn, bold_fn, fn_anton, fn_bold = ensure_fonts()
    if bold_obj is None:
        print("ERROR: Could not load fonts")
        doc.close()
        return False
    
    print("✓ Fonts loaded successfully")
    
    # Test overlay rendering
    _MASK_BG = (0.94, 0.97, 0.99)
    _BANNER_BLUE = (35/255, 64/255, 200/255)
    _SESSION_BG = (0.98, 0.99, 1.0)
    
    # Draw background rectangles
    page.draw_rect(fitz.Rect(57.0, 163.5, 155.0, 172.5), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 172.5, 155.0, 182.0), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 182.0, 155.0, 191.5), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    page.draw_rect(fitz.Rect(57.0, 191.5, 155.0, 205.0), color=_MASK_BG, fill=_MASK_BG, width=0, overlay=True)
    
    page.draw_rect(fitz.Rect(12.0, 137.0, 112.0, 159.0), color=_BANNER_BLUE, fill=_BANNER_BLUE, width=0, overlay=True)
    page.draw_rect(fitz.Rect(109.0, 105.0, 142.0, 115.0), color=_SESSION_BG, fill=_SESSION_BG, width=0, overlay=True)
    
    print("✓ Background rectangles drawn")
    
    # Draw photo area
    _PHOTO_OUTER = fitz.Rect(53.55, 72.70, 99.45, 129.72)
    _PHOTO_INNER = fitz.Rect(54.58, 73.78, 98.59, 128.68)
    page.draw_rect(_PHOTO_OUTER, color=(1,1,1), fill=(1,1,1), width=0, overlay=True)
    page.draw_rect(_PHOTO_OUTER, color=(0,0,0), fill=None, width=1.0, overlay=True)
    
    print("✓ Photo area drawn")
    
    # Draw name
    _name_val = clean_card_value(student["student_name"])
    if _name_val:
        _name_max_w = 104.0
        _name_fs = _fit_size(bold_obj, _name_val, _name_max_w,
                             REDEEMER_NAME_FONT_SIZE, REDEEMER_NAME_MIN_SIZE)
        _name_val_fit = _ellipsize_to_width(bold_obj, _name_val, _name_max_w, _name_fs)
        _name_tw = bold_obj.text_length(_name_val_fit, fontsize=_name_fs)
        _name_x = 4.0 + (_name_max_w - _name_tw) / 2.0
        page.insert_text(
            (_name_x, REDEEMER_NAME_BASELINE_Y),
            _name_val_fit,
            fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
            fontsize=_name_fs, color=(1,1,1), overlay=True,
        )
        print(f"✓ Name drawn: {_name_val}")
    
    # Draw class/section/roll
    _sec_text = clean_card_value(student["section"]).upper()
    _roll_text = clean_card_value(student["roll"])
    _class_text = clean_card_value(student["class"]).upper()
    _banner_fs = REDEEMER_CLASS_FONT_SIZE
    _banner_min = 4.5
    _bl2 = REDEEMER_CLASS_BASELINE_Y
    
    if _sec_text or _roll_text:
        if _sec_text:
            _s = f"Sec: {_sec_text}"
            _sz = _fit_size(bold_obj, _s, 55.0, _banner_fs, _banner_min)
            _s = _ellipsize_to_width(bold_obj, _s, 55.0, _sz)
            page.insert_text((17.47, _bl2), _s,
                             fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                             fontsize=_sz, color=(1,1,1), overlay=True)
            print(f"✓ Section drawn: {_sec_text}")
        if _roll_text:
            _r = f"Roll: {_roll_text}"
            _sz = _fit_size(bold_obj, _r, 30.0, _banner_fs, _banner_min)
            _r = _ellipsize_to_width(bold_obj, _r, 30.0, _sz)
            page.insert_text((78.78, _bl2), _r,
                             fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                             fontsize=_sz, color=(1,1,1), overlay=True)
            print(f"✓ Roll drawn: {_roll_text}")
    elif _class_text:
        _s = f"CLASS: {_class_text}"
        _sz = _fit_size(bold_obj, _s, 104.0, _banner_fs, _banner_min)
        _tw = bold_obj.text_length(_s, fontsize=_sz)
        page.insert_text((4.0 + (104.0 - _tw) / 2.0, _bl2), _s,
                         fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                         fontsize=_sz, color=(1,1,1), overlay=True)
        print(f"✓ Class drawn: {_class_text}")
    
    # Draw colons and values
    _COLON_X = 57.49
    _VALUE_X = 62.0
    _VALUE_W = 150.0 - _VALUE_X
    _FS = REDEEMER_VALUE_FONT_SIZE
    _FS_MIN = 4.7
    _BLACK = (0.0, 0.0, 0.0)
    
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
    
    print("✓ Colons drawn")
    
    # Draw values
    class_val = str(student.get("class") or "").strip().upper()
    father_val = str(student.get("father_name") or "").strip()
    mobile_val = str(student.get("mobile") or "").strip()
    addr_val = str(student.get("address") or "").strip()
    
    if not class_val: class_val = "NOT FOUND"
    if not father_val: father_val = "NOT FOUND"
    if not mobile_val: mobile_val = "NOT FOUND"
    if not addr_val: addr_val = "NOT FOUND"
    
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
    
    print(f"✓ Class: {class_val}")
    print(f"✓ Father: {father_val}")
    print(f"✓ Mobile: {mobile_val}")
    
    # Draw address
    _addr_lines, _ = wrap_and_shrink_text(bold_obj, addr_val, _VALUE_W, 2, base_size=_uniform_fs)
    for _ai, _aline in enumerate(_addr_lines):
        page.insert_text((_VALUE_X, REDEEMER_ADDRESS_BASELINE_Y + _ai * (_uniform_fs * REDEEMER_ADDRESS_LINE_GAP)),
                         _aline, fontname=fn_bold, fontfile=str(bold_fn) if bold_fn else None,
                         fontsize=_uniform_fs, color=_BLACK, overlay=True)
    print(f"✓ Address: {addr_val}")
    
    # Draw session
    _sess = clean_card_value(student.get("session", "")) or DEFAULT_SESSION
    _sess_rect = fitz.Rect(*REDEEMER_SESSION_VALUE_RECT)
    _sess_fs = _fit_size(anton_obj, _sess, _sess_rect.width,
                         REDEEMER_SESSION_FONT_SIZE, 5.6)
    _sess = _ellipsize_to_width(anton_obj, _sess, _sess_rect.width, _sess_fs)
    _put_single(page, _sess_rect, _sess, anton_fn, fn_anton, _sess_fs, _BLACK, anton_obj)
    print(f"✓ Session: {_sess}")
    
    # Save output
    output_path = Path(__file__).parent / "test_redeemer_output.pdf"
    buf = io.BytesIO()
    try:
        doc.save(buf, deflate=True, garbage=3, clean=True, incremental=False)
    except Exception as e:
        print(f"ERROR saving PDF: {e}")
        doc.close()
        return False
    
    doc.close()
    
    with open(output_path, "wb") as f:
        f.write(buf.getvalue())
    
    print(f"\n✅ SUCCESS! Test PDF saved to: {output_path}")
    print("Open this file to verify the Redeemer student overlay is working correctly.")
    return True

if __name__ == "__main__":
    success = test_redeemer_overlay()
    sys.exit(0 if success else 1)
