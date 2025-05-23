import os
import re
import time
import json
import logging
import requests
import matplotlib.pyplot as plt
import google.generativeai as genai
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from io import BytesIO
import hashlib
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('math_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load API keys with validation
def get_env_var(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise ValueError(f"❌ Missing required environment variable: {name}")
    return value

TELEGRAM_TOKEN = get_env_var("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = get_env_var("GEMINI_API_KEY")
DEBUG = os.getenv("DEBUG_MODE", "False").lower() == "true"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

# Constants
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
MAX_MESSAGE_LENGTH = 4000  # Telegram's actual limit is 4096
MAX_RETRIES = 3
RETRY_DELAY = 2
JSON_LOG_FILE = "user_questions.json"
CACHE_FILE = "response_cache.json"
CACHE_EXPIRY_DAYS = 7
RATE_LIMIT_PER_USER = 5  # Max requests per minute per user

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
generation_config = {
    "temperature": 0.2,  # More deterministic for math
    "top_p": 0.9,
    "top_k": 32,
    "max_output_tokens": 4096,
}
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

try:
    gemini_model = genai.GenerativeModel(
        model_name="models/gemini-1.5-pro-latest",
        generation_config=generation_config,
        safety_settings=safety_settings
    )
    logger.info(f"✅ Using Gemini model: {gemini_model.model_name}")
except Exception as e:
    logger.error(f"❌ Failed to initialize Gemini model: {e}")
    raise

# Configure matplotlib with improved LaTeX-like rendering
valid_styles = plt.style.available
if 'seaborn' in valid_styles:
    plt.style.use('seaborn')  # Default to seaborn if available
elif 'ggplot' in valid_styles:
    plt.style.use('ggplot')   # Fallback to ggplot
else:
    plt.style.use('default')  # Ultimate fallback

plt.rcParams.update({
    "font.size": 12,
    "font.family": "serif",
    "mathtext.fontset": "stix",
    "axes.edgecolor": "#2e3440",
    "axes.labelcolor": "#2e3440",
    "text.color": "#2e3440",
    "figure.facecolor": "#f8f9fa",
    "axes.facecolor": "#f8f9fa",
    "savefig.facecolor": "#f8f9fa",
    "savefig.dpi": 300,
    "savefig.format": "png",
    "savefig.bbox": "tight",
    "lines.linewidth": 1.5,
    "axes.grid": True,
    "grid.color": "#e5e9f0",
    "grid.alpha": 0.5
})

class ResponseCache:
    """Enhanced cache system with expiration and better performance."""
    def __init__(self, cache_file: str = CACHE_FILE):
        self.cache_file = cache_file
        self.cache = self._load_cache()
        self.lock = False  # Simple lock mechanism for thread safety

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                    # Clean expired entries on load
                    return {k: v for k, v in cache.items()
                           if (datetime.now() - datetime.fromisoformat(v["timestamp"])).days < CACHE_EXPIRY_DAYS}
        except Exception as e:
            logger.error(f"Error loading cache: {e}")
            return {}
        return {}

    def save_cache(self):
        while self.lock:
            time.sleep(0.1)
        self.lock = True
        try:
            temp_file = f"{self.cache_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=4, ensure_ascii=False)
            os.replace(temp_file, self.cache_file)
        except Exception as e:
            logger.error(f"Error saving cache: {e}")
        finally:
            self.lock = False

    def get(self, question: str) -> Optional[Dict[str, Any]]:
        question_hash = self._hash_question(question)
        if cached := self.cache.get(question_hash):
            if (datetime.now() - datetime.fromisoformat(cached["timestamp"])).days < CACHE_EXPIRY_DAYS:
                return cached["response"]
        return None

    def set(self, question: str, response: str):
        question_hash = self._hash_question(question)
        self.cache[question_hash] = {
            "response": response,
            "timestamp": datetime.now().isoformat()
        }
        self.save_cache()

    def _hash_question(self, question: str) -> str:
        return hashlib.md5(question.encode('utf-8')).hexdigest()

response_cache = ResponseCache()

class RateLimiter:
    """Simple rate limiter to prevent abuse."""
    def __init__(self):
        self.user_requests = {}

    def check_rate_limit(self, user_id: int) -> bool:
        now = time.time()
        if user_id not in self.user_requests:
            self.user_requests[user_id] = [now]
            return True

        # Remove old requests
        self.user_requests[user_id] = [
            t for t in self.user_requests[user_id]
            if now - t < 60
        ]

        if len(self.user_requests[user_id]) >= RATE_LIMIT_PER_USER:
            return False

        self.user_requests[user_id].append(now)
        return True

rate_limiter = RateLimiter()

def preprocess_math_text(text: str) -> str:
    """Improved preprocessing of math text with better symbol handling."""
    replacements = [
        (r'\\pmod\b', ' mod '),
        (r'\\mod\b', ' mod '),
        (r'\\begin\{.*?\}', ''),
        (r'\\end\{.*?\}', ''),
        (r'\\boxed\{([^}]*)\}', r'[\1]'),
        (r'\\text\{([^}]*)\}', r'\1'),
        (r'\\qquad', '    '),
        (r'\\quad', '  '),
        (r'\\,', ' '),
        (r'\\ ', ' '),
        (r'\\left\\\{', '{'),
        (r'\\right\\\}', '}'),
        (r'\\left\(', '('),
        (r'\\right\)', ')'),
        (r'\\left\[', '['),
        (r'\\right\]', ']'),
        (r'\\times', '×'),
        (r'\\div', '÷'),
        (r'\\leq', '≤'),
        (r'\\geq', '≥'),
        (r'\\neq', '≠'),
        (r'\\approx', '≈'),
        (r'\\pm', '±'),
        (r'\\to', '→'),
        (r'\\infty', '∞'),
        (r'\\sum', 'Σ'),
        (r'\\prod', 'Π'),
        (r'\\int', '∫'),
        (r'\\sqrt', '√'),
        (r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2'),
        (r'\\dot\{([^}]*)\}', r'\1̇'),  # Combining dot
        (r'\\ddot\{([^}]*)\}', r'\1̈'),  # Combining diaeresis
        (r'\\hat\{([^}]*)\}', r'\1̂'),  # Combining circumflex
        (r'\\tilde\{([^}]*)\}', r'\1̃'),  # Combining tilde
    ]

    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)

    # Handle exponents and subscripts
    text = re.sub(r'\^\{([^}]*)\}', r'^\1', text)
    text = re.sub(r'_\{([^}]*)\}', r'_\1', text)

    return text.strip()

def escape_markdown_v2(text: str) -> str:
    """More comprehensive MarkdownV2 escaping for Telegram."""
    escape_chars = r'_*[]()~`>#+-=|{}.!<>'
    text = text.replace("\\", "\\\\")
    for char in escape_chars:
        text = text.replace(char, f"\\{char}")
    return text

def make_telegram_request(
    endpoint: str,
    params: Optional[Dict] = None,
    files: Optional[Dict] = None,
    retries: int = MAX_RETRIES
) -> Optional[Dict]:
    """Improved Telegram API request with better timeout handling."""
    url = TELEGRAM_API_URL + endpoint
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        max_retries=retries,
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS
    )
    session.mount('https://', adapter)

    try:
        if files:
            response = session.post(url, files=files, data=params, timeout=(10, 30))
        else:
            response = session.post(url, json=params, timeout=(10, 30))
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"🔴 Telegram API request failed: {e}")
        if DEBUG:
            logger.error(f"Request details - URL: {url}, Params: {params}, Files: {files}")
        return None
    finally:
        session.close()

def get_updates(offset: Optional[int] = None) -> Dict:
    """Get updates with better error handling."""
    params = {"offset": offset, "timeout": 30} if offset else {"timeout": 30}
    try:
        response = make_telegram_request("getUpdates", params=params)
        return response or {"ok": False, "result": []}
    except Exception as e:
        logger.error(f"Failed to get updates: {e}")
        return {"ok": False, "result": []}

def send_typing(chat_id: int):
    """Send typing action with rate limiting."""
    try:
        make_telegram_request("sendChatAction",
                            params={"chat_id": chat_id, "action": "typing"})
    except Exception as e:
        logger.error(f"Failed to send typing action: {e}")

def send_message(
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict] = None,
    reply_to_message_id: Optional[int] = None,
    parse_mode: str = "MarkdownV2"
) -> bool:
    """Improved message sending with better chunking and error handling."""
    if not text.strip():
        return False

    # Truncate very long messages to avoid hitting Telegram limits
    text = text[:MAX_MESSAGE_LENGTH]

    payload = {
        "chat_id": chat_id,
        "text": escape_markdown_v2(text) if parse_mode == "MarkdownV2" else text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
        "disable_notification": True
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        response = make_telegram_request("sendMessage", params=payload)
        return response.get("ok", False)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return False

def send_image(
    chat_id: int,
    image_data: BytesIO,
    caption: Optional[str] = None,
    reply_to_message_id: Optional[int] = None
) -> bool:
    """Send image with better error handling."""
    try:
        image_data.seek(0)
        files = {"photo": ("solution.png", image_data, "image/png")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = escape_markdown_v2(caption)
            data["parse_mode"] = "MarkdownV2"
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        response = make_telegram_request("sendPhoto", files=files, params=data)
        return response.get("ok", False)
    except Exception as e:
        logger.error(f"🔴 Error sending image: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())
        return False

def send_pdf(
    chat_id: int,
    pdf_data: BytesIO,
    reply_to_message_id: Optional[int] = None
) -> bool:
    """Send PDF with better error handling."""
    try:
        pdf_data.seek(0)
        files = {"document": ("solution.pdf", pdf_data, "application/pdf")}
        data = {
            "chat_id": chat_id,
            "caption": "📄 *Downloadable PDF solution*",
            "parse_mode": "MarkdownV2"
        }
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        response = make_telegram_request("sendDocument", files=files, params=data)
        return response.get("ok", False)
    except Exception as e:
        logger.error(f"🔴 Error sending PDF: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())
        return False

def get_gemini_response(user_question: str) -> Optional[str]:
    """Improved Gemini response with better prompt engineering."""
    cached_response = response_cache.get(user_question)
    if cached_response:
        logger.info("✅ Serving response from cache")
        return cached_response

    prompt = (
        "You are an expert math tutor specializing in clear, step-by-step explanations. "
        "Follow these guidelines strictly:\n"
        "1. Use proper mathematical notation with Unicode symbols\n"
        "2. Format clearly with spacing between steps\n"
        "3. Highlight key transformations with → symbol\n"
        "4. Box final answers: [answer]\n"
        "5. For non-math questions, politely explain you specialize in mathematics\n"
        "6. Include brief explanations for each step\n"
        "7. Use standard mathematical terminology\n\n"
        "Example format:\n"
        "Problem: Solve 2x + 5 = 15\n"
        "Step 1: Subtract 5 from both sides\n"
        "2x + 5 - 5 = 15 - 5 → 2x = 10\n"
        "Step 2: Divide both sides by 2\n"
        "2x/2 = 10/2 → x = 5\n"
        "Solution: [x = 5]\n\n"
        f"Now solve this problem:\n{user_question}\n\n"
        "Provide your solution following the exact format above:"
    )

    for attempt in range(MAX_RETRIES):
        try:
            response = gemini_model.generate_content(prompt)
            if response.text:
                processed_response = preprocess_math_text(response.text)
                response_cache.set(user_question, processed_response)
                return processed_response.strip()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                logger.warning(f"⚠️ Gemini API error (attempt {attempt + 1}), retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            logger.error(f"⚠️ Gemini API error after {MAX_RETRIES} attempts: {e}")
            if DEBUG:
                logger.error(traceback.format_exc())
            return None

def render_math_to_image(math_text: str) -> BytesIO:
    """Improved math rendering with better formatting."""
    try:
        math_text = preprocess_math_text(math_text)

        # Split into lines and wrap text
        lines = math_text.split('\n')
        wrapped_lines = []
        for line in lines:
            if len(line) > 80:
                wrapped_lines.extend([line[i:i+80] for i in range(0, len(line), 80)])
            else:
                wrapped_lines.append(line)
        math_text = '\n'.join(wrapped_lines)

        fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
        ax.axis("off")
        ax.text(0.05, 0.95, math_text, ha="left", va="top", wrap=True,
               bbox=dict(facecolor='#f8f9fa', alpha=0.9,
                         edgecolor='#dee2e6', boxstyle='round,pad=0.5'),
               fontfamily='serif')

        plt.tight_layout(pad=2)
        img_buffer = BytesIO()
        fig.savefig(img_buffer, format='png', dpi=150, bbox_inches='tight')
        plt.close()
        return img_buffer
    except Exception as e:
        logger.error(f"🔴 Failed to render image: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())
        raise

def render_math_to_pdf(math_text: str) -> BytesIO:
    """Improved PDF rendering with better formatting."""
    try:
        math_text = preprocess_math_text(math_text)

        fig, ax = plt.subplots(figsize=(8.27, 11.69))  # A4 size
        ax.axis("off")
        ax.text(0.05, 0.95, math_text, ha="left", va="top", wrap=True,
               fontfamily='serif', linespacing=1.5)

        plt.tight_layout(pad=2)
        pdf_buffer = BytesIO()
        fig.savefig(pdf_buffer, format='pdf', dpi=150, bbox_inches='tight')
        plt.close()
        return pdf_buffer
    except Exception as e:
        logger.error(f"🔴 Failed to render PDF: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())
        raise

def save_question_to_json(chat_id: int, user_info: Dict, question_text: str, response: Optional[str] = None):
    """Improved logging with more user details."""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "chat_id": chat_id,
        "user": {
            "id": user_info.get("id"),
            "username": user_info.get("username"),
            "first_name": user_info.get("first_name"),
            "last_name": user_info.get("last_name")
        },
        "question": question_text,
        "response": response[:1000] + "..." if response and len(response) > 1000 else response,
        "response_length": len(response) if response else 0
    }

    try:
        existing_data = []
        if os.path.exists(JSON_LOG_FILE):
            with open(JSON_LOG_FILE, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)

        existing_data.append(log_entry)
        with open(JSON_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔴 Error saving to JSON log: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())

def create_keyboard_markup() -> Dict:
    """Enhanced keyboard with more math examples."""
    return {
        "keyboard": [
            [{"text": "∫(2x² + 3x) dx"}, {"text": "Solve 2x + 5 = 15"}],
            [{"text": "Area of circle r=5"}, {"text": "lim x→∞ (1 + 1/x)ˣ"}],
            [{"text": "Factor x² - 4"}, {"text": "Derivative of ln(x)"}],
            [{"text": "/help"}, {"text": "/about"}, {"text": "/examples"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": True
    }

def get_examples_message() -> str:
    """Returns formatted math examples."""
    examples = [
        "🔢 *Algebra Examples*:",
        "• `Solve 3x + 7 = 22`",
        "• `Factor x² - 9`",
        "• `Expand (x + 2)(x - 3)`",
        "",
        "∫ *Calculus Examples*:",
        "• `Derivative of sin(x²)`",
        "• `Integral of e^x dx`",
        "• `lim x→0 (sin x)/x`",
        "",
        "△ *Geometry Examples*:",
        "• `Area of circle r=5`",
        "• `Volume of sphere r=3`",
        "• `Pythagorean theorem a=3 b=4`"
    ]
    return escape_markdown_v2('\n'.join(examples))

def process_math_question(chat_id: int, user_info: Dict, question_text: str) -> Tuple[bool, str]:
    """Process a math question with improved error handling."""
    if not rate_limiter.check_rate_limit(user_info["id"]):
        return False, "⚠️ Too many requests. Please wait a minute before sending more questions."

    send_typing(chat_id)
    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(get_gemini_response, question_text)
            response = future.result(timeout=60)  # 1 minute timeout

        if not response or len(response.strip()) < 10:
            return False, "🤔 I couldn't generate a solution. Please try rephrasing your question."

        # Generate outputs in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            img_future = executor.submit(render_math_to_image, response)
            pdf_future = executor.submit(render_math_to_pdf, response)

            img_buffer = img_future.result()
            pdf_buffer = pdf_future.result()

        # Send outputs
        success = True
        if not send_image(chat_id, img_buffer, "📘 *Math Solution*"):
            success = False
        if not send_pdf(chat_id, pdf_buffer):
            success = False

        elapsed = round(time.time() - start_time, 2)
        status_msg = f"✅ Generated in {elapsed}s\n_Ask another question!_" if success else "⚠️ Some outputs may not have sent"

        # Log the interaction
        save_question_to_json(chat_id, user_info, question_text, response)

        return success, status_msg

    except Exception as e:
        logger.error(f"🔴 Error processing question: {e}")
        if DEBUG:
            logger.error(traceback.format_exc())
        return False, "⚠️ An error occurred while processing your question. Please try again."

def process_messages():
    """Main processing loop with improved error handling."""
    offset = None
    logger.info("🤖 Math Genius Bot is now running...")
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    while True:
        try:
            updates = get_updates(offset)

            if not updates.get("ok"):
                time.sleep(5)
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                if "message" not in update:
                    continue

                message = update["message"]
                chat_id = message["chat"]["id"]
                text = message.get("text", "").strip()
                user_info = message.get("from", {})

                if not text or not user_info.get("id"):
                    continue

                logger.info(f"📩 Message from {chat_id} (@{user_info.get('username')}): {text[:100]}...")

                # Handle commands
                if text.startswith("/"):
                    if text == "/start":
                        welcome_msg = (
                            "👋 *Math Genius Bot*\n\n"
                            "I can help with:\n"
                            "- Calculus problems\n"
                            "- Algebra equations\n"
                            "- Geometry proofs\n"
                            "- Trigonometry\n\n"
                            "Send a math problem or try an example below!"
                        )
                        send_message(chat_id, escape_markdown_v2(welcome_msg),
                                   reply_markup=create_keyboard_markup())
                    elif text == "/help":
                        help_msg = (
                            "🧮 *Help Menu*\n\n"
                            "Send math problems like:\n"
                            "• `Solve 2x + 5 = 15`\n"
                            "• `Find derivative of sin(x)`\n"
                            "• `Calculate ∫(x² + 3x)dx`\n\n"
                            "I'll provide step-by-step solutions with visual formatting."
                        )
                        send_message(chat_id, escape_markdown_v2(help_msg))
                    elif text == "/about":
                        about_msg = (
                            "🤖 *About This Bot*\n\n"
                            "Version: 3.0\n"
                            "Powered by Gemini 1.5 Pro\n"
                            "• Clean mathematical notation\n"
                            "• Image and PDF outputs\n"
                            "• Step-by-step explanations\n"
                            "• Rate limited to 5 requests/minute"
                        )
                        send_message(chat_id, escape_markdown_v2(about_msg))
                    elif text == "/examples":
                        send_message(chat_id, get_examples_message())
                    else:
                        send_message(chat_id, escape_markdown_v2("❌ Unknown command. Try /help"))
                    continue

                # Process math question
                success, status_msg = process_math_question(chat_id, user_info, text)
                send_message(chat_id, escape_markdown_v2(status_msg))

        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped by user.")
            executor.shutdown(wait=True)
            break
        except Exception as e:
            logger.exception(f"🔴 Unexpected error in main loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    try:
        if DEBUG:
            logger.info("🧪 Debug mode active - verbose logging enabled")
        process_messages()
    except Exception as e:
        logger.exception(f"🔴 Fatal error: {e}")
        raise