
from flask import Flask, request, jsonify, g
import pytesseract
from PIL import Image, UnidentifiedImageError
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import smtplib
import ssl
import io
import os
import logging

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

# Maximum upload size: 15 MB
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024

SUPPORTED_LANGUAGES = ['eng', 'ukr', 'eng+ukr']
OCR_CONFIG = '--oem 3 --psm 6'

# Background email workers
email_executor = ThreadPoolExecutor(max_workers=2)

# Persistent email configuration outside the project
MAIL_ENV_FILE = '/home/admin/.ocr-mail.env'


# --------------------------------------------------
# Email configuration
# --------------------------------------------------

def load_mail_config():
    """Load email configuration from the external env file."""

    config = {}

    if os.path.isfile(MAIL_ENV_FILE):
        try:
            with open(MAIL_ENV_FILE, 'r', encoding='utf-8') as file:
                for line in file:
                    line = line.strip()

                    if not line or line.startswith('#') or '=' not in line:
                        continue

                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip().strip('"').strip("'")

        except Exception:
            logger.exception("Could not read email configuration")

    # Environment variables override file values
    for key in [
        'SMTP_HOST',
        'SMTP_PORT',
        'SMTP_USER',
        'SMTP_PASSWORD',
        'MAIL_TO'
    ]:
        if os.environ.get(key):
            config[key] = os.environ[key]

    return {
        'host': config.get('SMTP_HOST', 'smtp.gmail.com'),
        'port': int(config.get('SMTP_PORT', '587')),
        'user': config.get('SMTP_USER', ''),
        'password': config.get('SMTP_PASSWORD', ''),
        'recipient': config.get('MAIL_TO', '')
    }


# --------------------------------------------------
# Email report
# --------------------------------------------------

def send_email_report(report):
    """Send an OCR report in the background."""

    try:
        config = load_mail_config()

        if not all([
            config['user'],
            config['password'],
            config['recipient']
        ]):
            logger.error("Email configuration is incomplete")
            return

        success = report['success']

        subject = (
            '✅ OCR Extraction Successful'
            if success
            else '❌ OCR Extraction Failed'
        )

        message = EmailMessage()
        message['Subject'] = subject
        message['From'] = config['user']
        message['To'] = config['recipient']

        body = [
            'OCR Processing Report',
            '=====================',
            '',
            f"Status: {'SUCCESS' if success else 'FAILED'}",
            f"Processing time: {report['time']}",
            f"HTTP status: {report['http_status']}",
            f"Language: {report.get('language', 'Unknown')}",
        ]

        if success:
            body.extend([
                '',
                'Extracted text:',
                '---------------',
                report.get('text', ''),
                '',
                f"Confidence: {report.get('confidence', 0)}",
                f"Word count: {report.get('word_count', 0)}"
            ])
        else:
            body.extend([
                '',
                'Error details:',
                '--------------',
                report.get('error', 'Unknown OCR error')
            ])

        message.set_content('\n'.join(body))

        # Attach original JPEG or PNG image
        image_bytes = report.get('image_bytes')

        if image_bytes:
            if image_bytes.startswith(b'\xff\xd8\xff'):
                message.add_attachment(
                    image_bytes,
                    maintype='image',
                    subtype='jpeg',
                    filename='original-image.jpg'
                )

            elif image_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
                message.add_attachment(
                    image_bytes,
                    maintype='image',
                    subtype='png',
                    filename='original-image.png'
                )

        context = ssl.create_default_context()

        with smtplib.SMTP(
            config['host'],
            config['port'],
            timeout=20
        ) as server:

            server.ehlo()
            server.starttls(context=context)
            server.ehlo()

            password = config['password'].replace(' ', '')

            server.login(config['user'], password)
            server.send_message(message)

        logger.info("OCR email report sent successfully")

    except Exception:
        # Email failures must never affect OCR
        logger.exception("Failed to send OCR email report")


def submit_email_report(report):
    """Submit email task without blocking the HTTP response."""

    try:
        email_executor.submit(send_email_report, report)

    except Exception:
        logger.exception("Could not schedule email report")


# --------------------------------------------------
# Capture request information
# --------------------------------------------------

@app.before_request
def initialize_ocr_report():

    if request.path == '/api/v1/read' and request.method == 'POST':
        g.ocr_started_at = datetime.now(timezone.utc)
        g.ocr_image_bytes = None


@app.after_request
def schedule_email_after_response(response):
    """
    Schedule email after the HTTP response has been closed.
    The email operation runs independently in the background.
    """

    if request.path != '/api/v1/read' or request.method != 'POST':
        return response

    try:
        result = response.get_json(silent=True) or {}

        started_at = getattr(
            g,
            'ocr_started_at',
            datetime.now(timezone.utc)
        )

        image_bytes = getattr(g, 'ocr_image_bytes', None)

        report = {
            'success': bool(result.get('success', False)),
            'text': result.get('text', ''),
            'language': result.get('language', 'Unknown'),
            'confidence': result.get('confidence', 0),
            'word_count': result.get('word_count', 0),
            'error': result.get('error', 'OCR request failed'),
            'http_status': response.status_code,
            'time': started_at.isoformat(),
            'image_bytes': bytes(image_bytes) if image_bytes else None
        }

        # The email is submitted only after the response closes.
        response.call_on_close(
            lambda report=report: submit_email_report(report)
        )

    except Exception:
        logger.exception("Could not prepare OCR email report")

    return response


# --------------------------------------------------
# Health endpoint
# --------------------------------------------------

@app.route('/health', methods=['GET'])
def health_check():

    return jsonify({
        "status": "healthy",
        "message": "OCR API is running"
    })


# --------------------------------------------------
# OCR endpoint
# --------------------------------------------------

@app.route('/api/v1/read', methods=['POST'])
def extract_text():

    try:
        logger.info("OCR request received")

        language = request.args.get(
            'language',
            request.form.get('language', 'eng')
        )

        if language not in SUPPORTED_LANGUAGES:
            return jsonify({
                "success": False,
                "error": "Unsupported language",
                "supported_languages": SUPPORTED_LANGUAGES
            }), 400

        # Method 1: Multipart upload
        if 'image' in request.files:

            uploaded_file = request.files['image']

            if uploaded_file.filename == '':
                return jsonify({
                    "success": False,
                    "error": "No image file selected"
                }), 400

            image_bytes = uploaded_file.read()

        # Method 2: Raw image upload (MacroDroid)
        else:

            image_bytes = request.get_data()

            if not image_bytes:
                return jsonify({
                    "success": False,
                    "error": "No image data received"
                }), 400

            content_type = request.mimetype

            if content_type not in ['image/jpeg', 'image/png']:
                return jsonify({
                    "success": False,
                    "error": "Content-Type must be image/jpeg or image/png"
                }), 415

        # Preserve original image for email attachment
        g.ocr_image_bytes = image_bytes

        # Open and validate image
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()

        except (UnidentifiedImageError, OSError, ValueError):
            return jsonify({
                "success": False,
                "error": "Invalid image format"
            }), 400

        # Convert image to RGB
        if image.mode != 'RGB':
            image = image.convert('RGB')

        logger.info(
            f"Processing image using language: {language}"
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
            for conf in data['conf']
            if int(conf) > 0
        ]

        avg_confidence = (
            sum(confidences) / len(confidences)
            if confidences else 0
        )

        response = {
            "success": True,
            "text": extracted_text,
            "language": language,
            "confidence": round(avg_confidence, 2),
            "word_count": len(extracted_text.split())
        }

        logger.info(
            f"OCR successful. Word count: {response['word_count']}"
        )

        return jsonify(response)

    except Exception:
        logger.exception("OCR processing failed")

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

if __name__ == '__main__':

    port = int(os.environ.get('PORT', 8088))

    app.run(
        host='0.0.0.0',
        port=port,
        debug=False
    )
