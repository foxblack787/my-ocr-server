
from flask import Flask, request, jsonify
import pytesseract
from PIL import Image, UnidentifiedImageError
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


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "message": "OCR API is running"
    })


@app.route('/api/v1/read', methods=['POST'])
def extract_text():
    try:
        logger.info("OCR request received")

        # Language can be supplied through query parameters
        # Example: /api/v1/read?language=eng
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

        # Method 1: Multipart form upload (backward compatible)
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

        logger.info(f"Processing image using language: {language}")

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8088))
    app.run(host='0.0.0.0', port=port, debug=False)
