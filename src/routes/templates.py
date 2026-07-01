import io
import logging
from flask import Blueprint, request, jsonify, send_file
from src.config import (
    TEMPLATE_CONFIGS, EMPLOYEE_TEMPLATE_KEYS, TEMPLATE_BRAND_COLORS, DEFAULT_TEMPLATE
)
from src.renderers.base import (
    normalize_template_key, get_template_config, _get_template_preview_png
)

log = logging.getLogger("idcard.routes.templates")
templates_bp = Blueprint("templates", __name__)


@templates_bp.route("/api/templates", methods=["GET"])
@templates_bp.route("/templates", methods=["GET"])
def get_templates():
    payload = []
    for key, template in TEMPLATE_CONFIGS.items():
        if key in EMPLOYEE_TEMPLATE_KEYS:
            continue
        payload.append({
            "key": key,
            "label": template["label"],
            "display_name": template["display_name"],
            "description": template["description"],
            "fields": template["fields"],
            "color": TEMPLATE_BRAND_COLORS.get(key, "#4F46E5"),
            "preview_url": f"/api/templates/{key}/preview.png",
        })
    return jsonify(payload)


@templates_bp.route("/api/templates/<template_key>/preview.png", methods=["GET"])
@templates_bp.route("/templates/<template_key>/preview.png", methods=["GET"])
def get_template_preview(template_key):
    key = normalize_template_key(template_key)
    try:
        png_bytes = _get_template_preview_png(key)
        if png_bytes:
            return send_file(io.BytesIO(png_bytes), mimetype="image/png",
                             download_name=f"{key}_preview.png")
    except Exception as e:
        log.warning("Template preview rasterization failed for %s: %s", key, e)

    colors = {
        "hebron":    "#DC2626",
        "redeemer":  "#4F46E5",
        "priyanka":  "#0F006A",
        "ab_ascent": "#224499",
    }
    labels = {
        "hebron":    "Hebron Mission School",
        "redeemer":  "My Redeemer Mission School",
        "priyanka":  "Priyanka Dreamnest School",
        "ab_ascent": "Ab Ascent School",
    }
    color = colors.get(key, "#4F46E5")
    label = labels.get(key, key.replace("_", " ").title())
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
  <rect width="320" height="200" rx="12" fill="{color}22"/>
  <rect width="320" height="52" rx="0" fill="{color}"/>
  <rect y="0" width="320" height="52" rx="12" fill="{color}"/>
  <rect y="26" width="320" height="26" fill="{color}"/>
  <text x="160" y="34" font-family="sans-serif" font-size="14" font-weight="bold"
        fill="white" text-anchor="middle">{label}</text>
  <rect x="24" y="72" width="56" height="72" rx="6" fill="{color}44"/>
  <text x="52" y="114" font-family="sans-serif" font-size="9" fill="{color}88" text-anchor="middle">PHOTO</text>
  <rect x="96" y="72" width="140" height="10" rx="4" fill="{color}55"/>
  <rect x="96" y="90" width="100" height="8" rx="4" fill="{color}33"/>
  <rect x="96" y="106" width="120" height="8" rx="4" fill="{color}33"/>
  <rect x="96" y="122" width="80" height="8" rx="4" fill="{color}33"/>
  <rect x="24" y="160" width="272" height="1" fill="{color}22"/>
  <text x="160" y="182" font-family="sans-serif" font-size="9" fill="{color}66" text-anchor="middle">ID Card Template Preview</text>
</svg>"""
    return send_file(io.BytesIO(svg.encode()), mimetype="image/svg+xml",
                     download_name=f"{key}_preview.svg")
