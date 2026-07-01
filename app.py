import os
import logging
from src.app import create_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("idcard.root")

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Werkzeug debug mode can reload when python files are changed
    _debug = os.environ.get("DEBUG_RELOAD", "0").strip() in ("1", "true", "yes")
    log.info("Running modularized Flask ID card generator server on port %d...", port)
    app.run(host="0.0.0.0", port=port, debug=_debug, threaded=True)
