import os
import multiprocessing as _mp
import tempfile
import logging
from pathlib import Path

log = logging.getLogger("idcard.config")

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PDF_HEBRON    = BASE_DIR / "template_id_card.pdf"
TEMPLATE_PDF_REDEEMER  = BASE_DIR / "template_redeemer.pdf"
TEMPLATE_PDF_PRIYANKA  = BASE_DIR / "template_priyanka.pdf"
TEMPLATE_PDF_AB_ASCENT = BASE_DIR / "template_ab_ascent.pdf"
TEMPLATE_PDF_JNANABHARATI = BASE_DIR / "JNANABHARATI_student.pdf"

ANTON_FONT             = BASE_DIR / "Anton-Regular.ttf"
ARIAL_BOLD             = BASE_DIR / "arialbd.ttf"

_FALLBACK_FONT_PATHS = [
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
]
_FALLBACK_FONT = next(
    (str(p) for p in _FALLBACK_FONT_PATHS if p.exists()), None
)

DEFAULT_SESSION = "2026-27"
DEFAULT_TEMPLATE = "redeemer"
DEFAULT_EMP_TEMPLATE = "redeemer_emp"

SCHOOLS = {
    2: "My Redeemer Mission School",
    3: "Hebron Mission School",
    4: "Priyanka Dreamnest School",
    5: "Ab Ascent School",
    6: "Jnanabharati English School",
}

TEMPLATE_PDF_HEBRON_EMP    = BASE_DIR / "template_hebron_emp.pdf"
TEMPLATE_PDF_REDEEMER_EMP  = BASE_DIR / "template_redeemer_emp.pdf"
TEMPLATE_PDF_PRIYANKA_EMP  = BASE_DIR / "template_priyanka_emp.pdf"
TEMPLATE_PDF_AB_ASCENT_EMP = BASE_DIR / "template_ab_ascent_emp.pdf"

# Backside Templates
BACKSIDE_PDF_HEBRON    = BASE_DIR / "hebron_backside.pdf"
BACKSIDE_PDF_REDEEMER  = BASE_DIR / "redeemer_backside.pdf"
BACKSIDE_PDF_PRIYANKA  = BASE_DIR / "priyanka_backside.pdf"
BACKSIDE_PDF_AB_ASCENT = BASE_DIR / "ab_ascent_backside.pdf"
BACKSIDE_PDF_JNANABHARATI = BASE_DIR / "jnanabharati_backside.pdf"
BACKSIDE_PDF_SCHOOL    = BASE_DIR / "school_backside.pdf"
BACKSIDE_PDF_EMPLOYEE  = BASE_DIR / "employee_backside.pdf"
BACKSIDE_PDF_STAFF     = BASE_DIR / "staff_backside.pdf"

TEMPLATE_CONFIGS = {
    "hebron": {
        "key": "hebron",
        "label": "Hebron",
        "display_name": "Hebron Mission School",
        "pdf": TEMPLATE_PDF_HEBRON,
        "renderer": "hebron",
        "description": "Red Hebron layout with section, roll, mother name and blood group.",
        "fields": [
            "student_name", "class", "section", "roll", "father_name",
            "mother_name", "dob", "address", "mobile", "adm_no",
            "blood_group", "session", "photo_url",
        ],
    },
    "redeemer": {
        "key": "redeemer",
        "label": "Redeemer",
        "display_name": "My Redeemer Mission School",
        "pdf": TEMPLATE_PDF_REDEEMER,
        "renderer": "redeemer",
        "description": "Blue Redeemer layout with father name, DOB, mobile and address.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no",
        ],
    },
    "priyanka": {
        "key": "priyanka",
        "label": "Priyanka",
        "display_name": "Priyanka Dreamnest School",
        "pdf": TEMPLATE_PDF_PRIYANKA,
        "renderer": "priyanka",
        "description": "Priyanka Dreamnest School ID layout.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no",
        ],
    },
    "ab_ascent": {
        "key": "ab_ascent",
        "label": "Ab Ascent",
        "display_name": "Ab Ascent School",
        "pdf": TEMPLATE_PDF_AB_ASCENT,
        "renderer": "ab_ascent",
        "description": "Ab Ascent School ID layout.",
        "fields": [
            "student_name", "class", "father_name", "dob", "address",
            "mobile", "session", "photo_url", "adm_no", "bus_route",
        ],
    },
    "jnanabharati": {
        "key":          "jnanabharati",
        "label":        "Jnanabharati",
        "display_name": "Jnanabharati English School",
        "pdf":          TEMPLATE_PDF_JNANABHARATI,
        "renderer":     "jnanabharati",
        "description":  "Jnanabharati English School student ID layout.",
        "fields": [
            "student_name", "class", "father_name", "mother_name", "dob",
            "adm_no", "blood_group", "photo_url",
        ],
    },
}

EMPLOYEE_TEMPLATE_CONFIGS = {
    "hebron_emp": {
        "key":          "hebron_emp",
        "label":        "Hebron — Employee",
        "display_name": "Hebron Mission School (Employee)",
        "pdf":          TEMPLATE_PDF_HEBRON_EMP,
        "renderer":     "hebron",
        "description": "Hebron Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "redeemer_emp": {
        "key":          "redeemer_emp",
        "label":        "Redeemer — Employee",
        "display_name": "My Redeemer Mission School (Employee)",
        "pdf":          TEMPLATE_PDF_REDEEMER_EMP,
        "renderer":     "redeemer",
        "description": "Redeemer Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "priyanka_emp": {
        "key":          "priyanka_emp",
        "label":        "Priyanka — Employee",
        "display_name": "Priyanka Dreamnest School (Employee)",
        "pdf":          TEMPLATE_PDF_PRIYANKA_EMP,
        "renderer":     "priyanka",
        "description": "Priyanka Dreamnest Employee ID layout (dedicated renderer).",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "photo_url",
        ],
    },
    "ab_ascent_emp": {
        "key":          "ab_ascent_emp",
        "label":        "Ab Ascent — Employee",
        "display_name": "Ab Ascent School (Employee)",
        "pdf":          TEMPLATE_PDF_AB_ASCENT_EMP,
        "renderer":     "ab_ascent",
        "description": "Ab Ascent Employee ID layout.",
        "fields": [
            "employee_name", "designation", "father_name", "dob",
            "address", "mobile", "emp_id", "validity", "photo_url",
        ],
    },
}

TEMPLATE_CONFIGS.update(EMPLOYEE_TEMPLATE_CONFIGS)
EMPLOYEE_TEMPLATE_KEYS = set(EMPLOYEE_TEMPLATE_CONFIGS.keys())

# Backside Template Configuration
BACKSIDE_TEMPLATE_CONFIGS = {
    "hebron": BACKSIDE_PDF_HEBRON,
    "redeemer": BACKSIDE_PDF_REDEEMER,
    "priyanka": BACKSIDE_PDF_PRIYANKA,
    "ab_ascent": BACKSIDE_PDF_AB_ASCENT,
    "jnanabharati": BACKSIDE_PDF_JNANABHARATI,
    "school": BACKSIDE_PDF_SCHOOL,
    "employee": BACKSIDE_PDF_EMPLOYEE,
    "staff": BACKSIDE_PDF_STAFF,
}

def get_backside_template(template_key: str):
    """Get the backside template PDF path for a given template key."""
    key = str(template_key or "").strip().lower()
    
    log.info(f"[backside-config] Input template_key: '{template_key}', Normalized key: '{key}'")
    
    # Handle employee template keys (e.g., "hebron_emp" -> "hebron")
    if key.endswith("_emp"):
        key = key.replace("_emp", "")
        log.info(f"[backside-config] Removed _emp suffix, key now: '{key}'")
    
    # Map to base template key
    template_mapping = {
        "hebron_emp": "hebron",
        "redeemer_emp": "redeemer",
        "priyanka_emp": "priyanka",
        "ab_ascent_emp": "ab_ascent",
    }
    
    if key in template_mapping:
        key = template_mapping[key]
        log.info(f"[backside-config] Mapped via template_mapping, key now: '{key}'")
    
    template_path = BACKSIDE_TEMPLATE_CONFIGS.get(key)
    log.info(f"[backside-config] Template path from config: '{template_path}', Exists: {template_path.exists() if template_path else False}")
    
    # Fallback to redeemer backside if specific template doesn't exist
    if template_path and not template_path.exists():
        log.warning(f"Backside template not found for '{key}': {template_path}, falling back to redeemer")
        template_path = BACKSIDE_TEMPLATE_CONFIGS.get("redeemer")
        log.info(f"[backside-config] Fallback path: '{template_path}', Exists: {template_path.exists() if template_path else False}")
    
    return template_path

API_BASE_URL = "https://titusattendence.com/apikey/apistudents?school_id={school_id}"

CLASS_ORDER = {
    "NURSERY": 0, "LKG": 1, "UKG": 2,
    "1ST": 3, "2ND": 4, "3RD": 5, "4TH": 6,
    "5TH": 7, "6TH": 8, "7TH": 9, "8TH": 10,
}

# Network, Session & DB Configs
MAX_CONCURRENT_USERS = int(os.environ.get("MAX_CONCURRENT_USERS", "3"))
ACCESS_CODE = (os.environ.get("ACCESS_CODE") or "").strip()
SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", "900"))
JOB_TTL_SECONDS = 30 * 60
PDF_RETENTION_SECONDS = int(os.environ.get("PDF_RETENTION_SECONDS", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_STUDENTS_PER_REQUEST = int(os.environ.get("MAX_STUDENTS_PER_REQUEST", "5000"))
PREVIEW_DPI = int(os.environ.get("PREVIEW_DPI", "500"))
DOWNLOAD_DPI = int(os.environ.get("DOWNLOAD_DPI", "500"))
PHOTO_TIMEOUT = (4, 8)
MAX_PHOTO_BYTES = int(os.environ.get("MAX_PHOTO_BYTES", str(8 * 1024 * 1024)))

_IS_PRODUCTION = os.environ.get("PRODUCTION", "0").strip() in ("1", "true", "yes")
_PROD_TMP = "/tmp"
PDF_TEMP_DIR = os.environ.get(
    "PDF_TEMP_DIR",
    _PROD_TMP if _IS_PRODUCTION else tempfile.gettempdir()
)

PHOTO_PX = int(os.environ.get("PHOTO_PX", "600"))
PHOTO_JPEG_QUALITY = int(os.environ.get("PHOTO_JPEG_QUALITY", "95"))

_CPU_COUNT = max(1, _mp.cpu_count())
PREFETCH_WORKERS = 64
CARD_RENDER_WORKERS = 16
ZIP_BUILD_WORKERS = 16
CHUNK_PAGES = 50
MERGE_COMPACT_PAGES = 500
MAX_CACHED_PHOTOS = 2000

# Physical Card Layout Dimensions
CARD_W_MM = 55.0
CARD_H_MM = 86.0
A4_W_MM = 297.0
A4_H_MM = 210.0
COLS = 5
ROWS = 2
CARDS_PER_PAGE = 10
ROW_GAP_MM = 10.0
OFFSET_X_MM = 11.0
OFFSET_Y_MM = 14.0

MM_TO_PT = 2.834645669291339
PT_PER_INCH = 72.0

CARD_W_PT = CARD_W_MM * MM_TO_PT
CARD_H_PT = CARD_H_MM * MM_TO_PT
A4_W_PT = A4_W_MM * MM_TO_PT
A4_H_PT = A4_H_MM * MM_TO_PT

OX_PT = 31.181102362204726
OY_PT = 39.68503937007874
ROW_GAP_PT = 28.34645669291339
COL_GAP_PT = 2.834645669291339
COL_STEP = 158.740157480315
ROW_STEP = 272.12598425196853

TEMPLATE_BRAND_COLORS = {
    'hebron': '#DC2626',
    'redeemer': '#4F46E5',
    'priyanka': '#0F006A',
    'ab_ascent': '#224499'
}

PROD_MAX_STUDENTS = 1500
PHOTO_EMBED_SCALE = 7
PREVIEW_EXTERNAL_THRESHOLD = 9999
REDEEMER_GRAD_STEPS = 60

# External Storage Settings
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "generated-pdfs").strip()
SUPABASE_SIGNED_URL_TTL = int(os.environ.get("SUPABASE_SIGNED_URL_TTL", "3600"))

GOOGLE_DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "").strip()
GOOGLE_DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
GOOGLE_DRIVE_REFRESH_TOKEN = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()

# PDF save flags options dict
_PDF_SAVE_OPTS = {
    'deflate': True,
    'deflate_images': True,
    'garbage': 4,
    'clean': True,
    'incremental': False,
    'linear': True
}

ADDR_MAX_LINES = 2
ADDR_LINE_GAP = 1.15

# Redeemer Layout Constants
REDEEMER_BG_COLOR = (0.9686, 0.9922, 0.9961)
REDEEMER_WHITE = (1.0, 1.0, 1.0)
REDEEMER_BLACK = (0.0, 0.0, 0.0)
REDEEMER_PHOTO_OUTER_RECT = (53.55, 72.7, 99.45, 129.72)
REDEEMER_PHOTO_RECT_COORDS = (54.58, 73.78, 98.59, 128.68)
REDEEMER_PHOTO_BORDER_W = 1.0
REDEEMER_BANNER_RECT = (0.0, 135.66, 124.45, 160.65)
REDEEMER_BANNER_TOP_RIGHT_X = 120.0
REDEEMER_BANNER_BOT_RIGHT_X = 111.0
REDEEMER_BANNER_Y0 = 136.0
REDEEMER_BANNER_Y1 = 160.0
REDEEMER_BANNER_TEXT_LEFT = 4.0
REDEEMER_BANNER_TEXT_RIGHT = 108.0
REDEEMER_BANNER_CENTER_X = 60.0

REDEEMER_NAME_BASELINE_Y = 147.0
REDEEMER_CLASS_BASELINE_Y = 155.5
REDEEMER_SESSION_CLEAN_COORDS = (104.0, 93.0, 143.0, 115.5)
REDEEMER_SESSION_VALUE_RECT = (111.08, 105.96, 140.0, 113.78)
REDEEMER_DATA_CLEAN_RECT = (56.0, 163.5, 153.0, 205.0)
REDEEMER_VALUE_X = 62.0
REDEEMER_COLON_X = 57.49
REDEEMER_VALUE_MAX_X = 150.0
REDEEMER_CLASS_VALUE_BASELINE_Y = 170.44
REDEEMER_FATHER_BASELINE_Y = 179.77
REDEEMER_MOBILE_BASELINE_Y = 189.1
REDEEMER_ADDRESS_BASELINE_Y = 198.43
REDEEMER_DOB_BASELINE_Y = 189.1

REDEEMER_NAME_FONT_SIZE = 8.0
REDEEMER_NAME_MIN_SIZE = 5.0
REDEEMER_CLASS_FONT_SIZE = 5.8842
REDEEMER_VALUE_FONT_SIZE = 6.8
REDEEMER_ADDRESS_MAX_LINES = 2
REDEEMER_ADDRESS_LINE_GAP = 1.02
REDEEMER_SESSION_FONT_SIZE = 7.2

TEARDROP_ITEMS = [
    ('l', (126.74588, 84.57169), (119.56597, 72.82723)),
    ('l', (119.56597, 72.82723), (112.9128, 84.49141)),
    ('c', (112.9128, 84.49141), (111.36359, 86.96311), (111.22838, 90.17703), (112.85576, 92.83886)),
    ('c', (112.85576, 92.83886), (115.16902, 96.62247), (120.15327, 97.83719), (123.98969, 95.55492)),
    ('c', (123.98969, 95.55492), (127.82469, 93.27335), (129.05914, 88.35811), (126.74588, 84.57169))
]

# Hebron Layout Constants
WHITE = (1.0, 1.0, 1.0)
BLOOD_RED = (0.8549, 0.0627, 0.0627)
BANNER_RED = (0.7843, 0.0667, 0.0667)
NAME_COLOR = (1.0, 1.0, 1.0)
VALUE_COLOR = (0.6666666666666666, 0.06274509803921569, 0.06274509803921569)

NAME_FONT_SIZE = 9.9
CLASS_FONT_SIZE = 5.9
VALUE_FONT_SIZE = 5.5
ADM_FONT_SIZE = 6.5
SESSION_FONT_SIZE = 7.5
BLOOD_FONT_SIZE = 6.88

PHOTO_RECT_COORDS = (54.25, 67.74, 98.82, 119.07)
BAND_Y0 = 123.8
BAND_Y1 = 151.0
NAME_TEXT_RECT_COORDS = (13.0, 124.7, 112.0, 139.2)
CLASS_TEXT_RECT_COORDS = (13.0, 139.7, 112.0, 147.0)
SIGN_SAFE_X1 = 118.0
ADM_WHITEOUT_COORDS = (18.0, 107.0, 48.0, 116.5)
ADM_VALUE_RECT_COORDS = (18.51, 107.56, 48.0, 115.5)
SESSION_WHITEOUT_COORDS = (109.15, 107.5, 142.0, 118.5)
SESSION_VALUE_RECT_COORDS = (109.15, 108.0, 142.0, 118.5)
BLOOD_VALUE_RECT_COORDS = (112.0, 84.5, 129.0, 97.5)

FATHER_CLEAN_COORDS = (66.3, 153.8, 149.0, 161.2)
MOTHER_CLEAN_COORDS = (66.3, 161.5, 149.0, 169.0)
DOB_CLEAN_COORDS = (66.3, 168.0, 149.0, 175.5)
ADDRESS_CLEAN_COORDS = (66.3, 174.8, 118.0, 188.0)
MOBILE_CLEAN_COORDS = (66.3, 190.5, 113.0, 198.0)

FATHER_VALUE_RECT_COORDS = (66.3, 154.4, 148.0, 160.6)
MOTHER_VALUE_RECT_COORDS = (66.3, 162.2, 148.0, 168.3)
DOB_VALUE_RECT_COORDS = (66.3, 168.8, 148.0, 174.9)
ADDRESS_VALUE_RECT_COORDS = (66.3, 175.4, 118.0, 187.0)
MOBILE_VALUE_RECT_COORDS = (66.3, 191.1, 118.0, 197.2)



