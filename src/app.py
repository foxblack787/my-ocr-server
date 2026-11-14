
from flask import Flask, request, jsonify, g
import pytesseract
from PIL import Image, UnidentifiedImageError
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import dropbox
from dropbox.files import WriteMode
import io
import os
import logging
import re
import json
import uuid

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

SUPPORTED_LANGUAGES = ["eng", "ukr", "eng+ukr"]
OCR_CONFIG = "--oem 3 --psm 6"

DROPBOX_ENV_FILE = "/home/admin/.dropbox-ocr.env"
DROPBOX_ROOT = "/OCR"

LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")
RETENTION_DAYS = 3

archive_executor = ThreadPoolExecutor(max_workers=2)


# --------------------------------------------------
# Environment configuration
# --------------------------------------------------

def read_env_file(path):

    config = {}

    if not os.path.isfile(path):
        return config

    with open(path, "r", encoding="utf-8") as file:

        for line in file:

            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)

            config[key.strip()] = (
                value.strip().strip('"').strip("'")
            )

    return config


# --------------------------------------------------
# Dropbox connection
# --------------------------------------------------

def get_dropbox_client():

    config = read_env_file(DROPBOX_ENV_FILE)

    app_key = (
        os.environ.get("DROPBOX_APP_KEY")
        or config.get("DROPBOX_APP_KEY")
    )

    app_secret = (
        os.environ.get("DROPBOX_APP_SECRET")
        or config.get("DROPBOX_APP_SECRET")
    )

    refresh_token = (
        os.environ.get("DROPBOX_REFRESH_TOKEN")
        or config.get("DROPBOX_REFRESH_TOKEN")
    )

    if not all([app_key, app_secret, refresh_token]):
        raise RuntimeError("Dropbox configuration is incomplete.")

    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
        timeout=30
    )


# --------------------------------------------------
# Count ALL words
# --------------------------------------------------

def count_all_words(text):

    words = re.findall(
        r"[^\W\d_]+(?:['’][^\W\d_]+)*",
        text,
        flags=re.UNICODE
    )

    counts = Counter(words)

    return dict(counts)


# --------------------------------------------------
# Count repeated words
# --------------------------------------------------

def count_repeated_words(word_counts):

    return {
        word: count
        for word, count in word_counts.items()
        if count >= 2
    }


# --------------------------------------------------
# Dropbox folder management
# --------------------------------------------------

def ensure_dropbox_folder(client, path):

    try:

        client.files_create_folder_v2(
            path,
            autorename=False
        )

    except dropbox.exceptions.ApiError as error:

        if (
            "conflict" not in str(error).lower()
            and "already" not in str(error).lower()
        ):
            raise


# --------------------------------------------------
# Delete old daily folders
# --------------------------------------------------

def cleanup_old_daily_folders(client, today):

    cutoff = today - timedelta(
        days=RETENTION_DAYS - 1
    )

    result = client.files_list_folder(DROPBOX_ROOT)

    while True:

        for entry in result.entries:

            if not isinstance(
                entry,
                dropbox.files.FolderMetadata
            ):
                continue

            try:

                folder_date = datetime.strptime(
                    entry.name,
                    "%Y-%m-%d"
                ).date()

            except ValueError:
                continue

            if folder_date < cutoff:

                try:

                    client.files_delete_v2(
                        entry.path_lower or entry.path_display
                    )

                    logger.info(
                        "Old Dropbox folder moved to trash: %s",
                        entry.name
                    )

                except Exception:

                    logger.exception(
                        "Could not clean old Dropbox folder: %s",
                        entry.name
                    )

        if not result.has_more:
            break

        result = client.files_list_folder_continue(
            result.cursor
        )


# --------------------------------------------------
# Archive OCR result to Dropbox
# --------------------------------------------------

def archive_ocr_result(
    image_bytes,
    response_data,
    content_type,
    started_at
):

    try:

        client = get_dropbox_client()

        local_started = started_at.astimezone(
            LOCAL_TIMEZONE
        )

        day_folder = local_started.strftime(
            "%Y-%m-%d"
        )

        operation_id = (
            local_started.strftime("%H-%M-%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )

        daily_path = f"{DROPBOX_ROOT}/{day_folder}"
        operation_path = f"{daily_path}/{operation_id}"

        ensure_dropbox_folder(client, DROPBOX_ROOT)
        ensure_dropbox_folder(client, daily_path)
        ensure_dropbox_folder(client, operation_path)

        image_name = (
            "original.png"
            if content_type == "image/png"
            else "original.jpg"
        )

        txt_bytes = response_data.get(
            "text", ""
        ).encode("utf-8")

        json_bytes = json.dumps(
            response_data,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")

        # Upload original image
        client.files_upload(
            image_bytes,
            f"{operation_path}/{image_name}",
            mode=WriteMode.overwrite,
            mute=True
        )

        # Upload extracted text
        client.files_upload(
            txt_bytes,
            f"{operation_path}/result.txt",
            mode=WriteMode.overwrite,
            mute=True
        )

        # Upload JSON including all word counts
        client.files_upload(
            json_bytes,
            f"{operation_path}/result.json",
            mode=WriteMode.overwrite,
            mute=True
        )

        logger.info(
            "OCR archive uploaded to Dropbox: %s",
            operation_path
        )

        cleanup_old_daily_folders(
            client,
            local_started.date()
        )

    except Exception:

        logger.exception(
            "Dropbox OCR archive failed"
        )


# --------------------------------------------------
# Background archive submission
# --------------------------------------------------

def submit_archive(
    image_bytes,
    response_data,
    content_type,
    started_at
):

    if not image_bytes:

        logger.warning(
            "Dropbox archive skipped: original image bytes are missing"
        )

        return

    try:

        archive_executor.submit(
            archive_ocr_result,
            bytes(image_bytes),
            dict(response_data),
            content_type,
            started_at
        )

    except Exception:

        logger.exception(
            "Could not schedule Dropbox archive"
        )


# --------------------------------------------------
# Capture request information
# --------------------------------------------------

@app.before_request
def initialize_ocr_request():

    if (
        request.path == "/api/v1/read"
        and request.method == "POST"
    ):

        g.ocr_started_at = datetime.now(timezone.utc)
        g.ocr_image_bytes = None
        g.ocr_content_type = "image/jpeg"


@app.after_request
def schedule_dropbox_after_response(response):

    if (
        request.path != "/api/v1/read"
        or request.method != "POST"
    ):

        return response

    try:

        result = response.get_json(silent=True) or {}

        if not result.get("success"):
            return response

        started_at = getattr(
            g,
            "ocr_started_at",
            datetime.now(timezone.utc)
        )

        image_bytes = getattr(
            g,
            "ocr_image_bytes",
            None
        )

        content_type = getattr(
            g,
            "ocr_content_type",
            "image/jpeg"
        )

        submit_archive(
            image_bytes,
            result,
            content_type,
            started_at
        )

    except Exception:

        logger.exception(
            "Could not prepare Dropbox archive"
        )

    return response


# --------------------------------------------------
# Health endpoint
# --------------------------------------------------

@app.route("/health", methods=["GET"])
def health_check():

    return jsonify({
        "status": "healthy",
        "message": "OCR API is running"
    })


# --------------------------------------------------
# OCR endpoint
# --------------------------------------------------

@app.route("/api/v1/read", methods=["POST"])
def extract_text():

    try:

        logger.info("OCR request received")

        language = request.args.get(
            "language",
            request.form.get("language", "eng")
        )

        if language not in SUPPORTED_LANGUAGES:

            return jsonify({
                "success": False,
                "error": "Unsupported language",
                "supported_languages": SUPPORTED_LANGUAGES
            }), 400

        # Multipart upload
        if "image" in request.files:

            uploaded_file = request.files["image"]

            if uploaded_file.filename == "":

                return jsonify({
                    "success": False,
                    "error": "No image file selected"
                }), 400

            image_bytes = uploaded_file.read()

        # Raw image upload from MacroDroid
        else:

            image_bytes = request.get_data()

            if not image_bytes:

                return jsonify({
                    "success": False,
                    "error": "No image data received"
                }), 400

            if request.mimetype not in [
                "image/jpeg",
                "image/png"
            ]:

                return jsonify({
                    "success": False,
                    "error": "Content-Type must be image/jpeg or image/png"
                }), 415

        # Preserve image in memory
        g.ocr_image_bytes = image_bytes

        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):

            g.ocr_content_type = "image/png"

        elif image_bytes.startswith(b"\xff\xd8\xff"):

            g.ocr_content_type = "image/jpeg"

        else:

            g.ocr_content_type = (
                request.mimetype or "image/jpeg"
            )

        # Validate image
        try:

            image = Image.open(
                io.BytesIO(image_bytes)
            )

            image.load()

        except (
            UnidentifiedImageError,
            OSError,
            ValueError
        ):

            return jsonify({
                "success": False,
                "error": "Invalid image format"
            }), 400

        # Convert to RGB
        if image.mode != "RGB":

            image = image.convert("RGB")

        logger.info(
            "Processing image using language: %s",
            language
        )

        # Extract text
        extracted_text = pytesseract.image_to_string(
            image,
            lang=language,
            config=OCR_CONFIG
        ).strip()

        # Calculate confidence
        data = pytesseract.image_to_data(
            image,
            lang=language,
            config=OCR_CONFIG,
            output_type=pytesseract.Output.DICT
        )

        confidences = [
            int(conf)
            for conf in data["conf"]
            if int(conf) > 0
        ]

        avg_confidence = (
            sum(confidences) / len(confidences)
            if confidences
            else 0
        )

        # Count every word, including words appearing once
        word_counts = count_all_words(extracted_text)

        # Keep repeated_words for backward compatibility
        repeated_words = count_repeated_words(word_counts)

        # JSON response
        result = {
            "success": True,
            "text": extracted_text,
            "language": language,
            "confidence": round(avg_confidence, 2),
            "word_count": len(extracted_text.split()),
            "word_counts": word_counts,
            "repeated_words": repeated_words
        }

        logger.info(
            "OCR successful. Total unique words: %s",
            len(word_counts)
        )

        logger.info(
            "UTC count: %s",
            word_counts.get("UTC", 0)
        )

        return jsonify(result)

    except Exception:

        logger.exception(
            "OCR processing failed"
        )

        return jsonify({
            "success": False,
            "error": "Internal server error during text extraction"
        }), 500


# --------------------------------------------------
# Error handlers
# --------------------------------------------------

@app.errorhandler(413)
def too_large(error):

    return jsonify({
        "success": False,
        "error": "File too large. Maximum size is 15 MB"
    }), 413


@app.errorhandler(404)
def not_found(error):

    return jsonify({
        "success": False,
        "error": "Endpoint not found"
    }), 404


# --------------------------------------------------
# Application startup
# --------------------------------------------------

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 8088)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
