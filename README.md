Simple OCR Server

A lightweight OCR REST API built with Flask, Tesseract OCR, and Pillow.

The API extracts text from uploaded images and returns the recognized text along with the detected language, OCR confidence, and word count.

It supports both raw image uploads (useful for MacroDroid and HTTP clients) and multipart/form-data uploads.

Features

- Extract text from images using Tesseract OCR.
- Supports English ("eng"), Ukrainian ("ukr"), and combined English + Ukrainian ("eng+ukr").
- Supports raw JPEG and PNG image uploads.
- Supports multipart/form-data image uploads.
- Compatible with MacroDroid HTTP Request.
- Returns OCR confidence and word count.
- Maximum upload size: 15 MB.
- JSON responses.
- Health check endpoint.
- No API key or authentication required.

Requirements

- Python
- Flask
- Tesseract OCR
- pytesseract
- Pillow
- Gunicorn (for production deployment)

Install Tesseract OCR

On Ubuntu or Debian:

sudo apt update
sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ukr

Install Python dependencies

pip install -r requirements.txt

Running the API

Start the application:

python app.py

By default, the application listens on port "8088".

For production, you can use Gunicorn:

gunicorn --bind 0.0.0.0:8088 --workers 2 --timeout 120 app:app

Run the command from the directory containing "app.py".

API Endpoints

1. Health Check

Check whether the API is running.

Endpoint:

GET /health

Example response:

{
  "status": "healthy",
  "message": "OCR API is running"
}

2. Extract Text from an Image

Endpoint:

POST /api/v1/read

The API accepts two upload methods.

Method 1: Raw Image Upload

Send the image directly as the HTTP request body.

Supported content types:

- "image/jpeg"
- "image/png"

The language can be specified using a query parameter.

Example:

POST /api/v1/read?language=eng
Content-Type: image/jpeg

cURL example:

curl -X POST "http://localhost:8088/api/v1/read?language=eng" \
  -H "Content-Type: image/jpeg" \
  --data-binary "@image.jpg"

For a PNG image:

curl -X POST "http://localhost:8088/api/v1/read?language=eng" \
  -H "Content-Type: image/png" \
  --data-binary "@image.png"

MacroDroid Configuration

The API supports direct image uploads, making it suitable for MacroDroid.

Configure the HTTP Request action as follows:

Setting| Value
Method| POST
URL| Your API address followed by "/api/v1/read?language=eng"
Content Body| File
File| Your screenshot file
Content Type| "image/jpeg"
Response| JSON

For PNG screenshots, use "image/png".

The API returns the extracted text in the JSON response under the "text" field.

Method 2: Multipart Form Upload

The API also supports the traditional multipart/form-data method.

Parameters:

Parameter| Type| Required| Description
"image"| File| Yes| Image to process
"language"| String| No| OCR language; defaults to "eng"

cURL example:

curl -X POST "http://localhost:8088/api/v1/read" \
  -F "image=@image.jpg" \
  -F "language=eng"

Supported Languages

Language| Code
English| "eng"
Ukrainian| "ukr"
English + Ukrainian| "eng+ukr"

You can specify the language through the URL query parameter or the multipart form field.

If no language is specified, English ("eng") is used.

Successful Response

Example:

{
  "success": true,
  "text": "The quick brown fox jumps over the lazy dog.",
  "language": "eng",
  "confidence": 92.45,
  "word_count": 9
}

Response Fields

Field| Type| Description
"success"| Boolean| Indicates whether OCR processing succeeded
"text"| String| Extracted text
"language"| String| OCR language used
"confidence"| Number| Average OCR confidence score
"word_count"| Integer| Number of extracted words

Error Responses

The API returns JSON error messages.

HTTP Status| Description
"400"| Missing image data, invalid image, or unsupported language
"404"| Endpoint not found
"413"| Image exceeds the 15 MB upload limit
"415"| Unsupported Content-Type for raw image uploads
"500"| Internal server error

Example:

{
  "success": false,
  "error": "Invalid image format"
}

OCR Configuration

The current Tesseract configuration is:

OCR_CONFIG = '--oem 3 --psm 6'

- "--oem 3": Uses the default OCR engine mode.
- "--psm 6": Assumes a single uniform block of text.

Upload Limit

The maximum accepted request size is:

15 MB

Notes

- Raw image uploads must use "image/jpeg" or "image/png".
- Multipart uploads must include the image under the "image" field.
- The API does not return image coordinates or bounding boxes.
- The API returns extracted text and OCR statistics only.
- Tesseract language data must be installed on the host system.
- This API currently has no authentication. Do not expose it to untrusted users without considering the security implications.

License

See the repository's "LICENSE" file for licensing information.
