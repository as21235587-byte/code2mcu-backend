from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from groq import Groq
from dotenv import load_dotenv
import os
import glob
import time
import shutil
import tempfile
import subprocess
import json

load_dotenv()

app = FastAPI()
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # production me isko apni website ke domain tak limit kar sakte ho
    allow_methods=["*"],
    allow_headers=["*"],
)


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(NoCacheMiddleware)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# arduino-cli ka path: .env ka ARDUINO_CLI_PATH, fir PATH, fir plain naam
ARDUINO_CLI = os.getenv("ARDUINO_CLI_PATH") or shutil.which("arduino-cli") or "arduino-cli"

BOARD_FQBN = {
    "uno": "arduino:avr:uno",
    "esp32": "esp32:esp32:esp32",
    "esp8266": "esp8266:esp8266:generic",
}

# ---------------------------------------------------------------------------
# Simple rate limit (per IP) — taaki koi aapki Groq API key ka quota na khatam kar de.
# Memory me rehta hai, server restart par reset ho jaata hai. Zaroorat ho to numbers badlo.
# ---------------------------------------------------------------------------
RATE_LIMIT_MAX = 40          # itne requests
RATE_LIMIT_WINDOW = 600      # itne seconds (10 minute) me
_hits = {}


def too_many_requests(request: Request) -> bool:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    if len(recent) >= RATE_LIMIT_MAX:
        _hits[ip] = recent
        return True
    recent.append(now)
    _hits[ip] = recent
    return False


DANGEROUS_KEYWORDS = [
    "220v", "230v", "ac power", "mains", "short",
    "gnd to vcc", "vcc to gnd", "reverse polarity"
]


def is_dangerous(text: str) -> bool:
    return any(k in text.lower() for k in DANGEROUS_KEYWORDS)


def strip_code_fences(text: str) -> str:
    """Agar AI ne ``` laga diya ho to hata do, warna compile fail hoga."""
    text = (text or "").strip()
    if "```" in text:
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```"))
    return text.strip()


@app.get("/")
def home():
    return FileResponse("static/index.html")


class CommandRequest(BaseModel):
    command: str


@app.post("/generate")
def generate_code(request: CommandRequest, http: Request):
    """Sirf code generate karta hai. Compile/flash nahi karta."""
    if too_many_requests(http):
        return {"error": "Bahut zyada requests. Thodi der baad dobara try karo.", "code": None}
    if len(request.command) > 1000:
        return {"error": "Command bahut lamba hai.", "code": None}
    if is_dangerous(request.command):
        return {"error": "This command was blocked for safety reasons.", "code": None}

    system_prompt = (
        "You are an Arduino code generator. Given a plain-language "
        "instruction, output ONLY valid Arduino C++ sketch code "
        "(setup() and loop() functions). Do not include explanations, "
        "markdown formatting, or code fences — just raw code."
    )

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.command},
            ],
        )
    except Exception as e:
        return {"error": f"Code generate nahi ho paya: {e}", "code": None}

    generated_code = strip_code_fences(response.choices[0].message.content)

    if is_dangerous(generated_code):
        return {"error": "Generated code was blocked for safety reasons.", "code": None}

    return {"error": None, "code": generated_code}


# ---------------------------------------------------------------------------
# /circuit — generated code ko padhkar wiring / circuit JSON banata hai.
# ---------------------------------------------------------------------------
class CircuitRequest(BaseModel):
    code: str
    board: str = "uno"
    lang: str = "en"   # "en" ya "hi" — notes isi bhasha me aayenge


@app.post("/circuit")
def make_circuit(request: CircuitRequest, http: Request):
    """Generated code padhkar professional diagram ke liye typed wiring JSON banata hai."""
    if too_many_requests(http):
        return {"error": "Bahut zyada requests. Thodi der baad dobara try karo.", "circuit": None}
    if len(request.code) > 20000:
        return {"error": "Code bahut bada hai.", "circuit": None}

    notes_lang = (
        "Write the 'notes' field in simple Hindi (Devanagari script)."
        if request.lang == "hi" else
        "Write the 'notes' field in simple English."
    )
    system_prompt = (
        "You are an electronics assistant for beginners. Read the given Arduino sketch and "
        "output ONLY one JSON object (no explanation, no markdown, no code fences) "
        "describing the physical circuit needed to run it.\n\n"
        "Exact format:\n"
        '{"components":[{"type":"led","label":"Red LED","pins":['
        '{"name":"Anode (+)","board_pin":"D9","via":"220 ohm"},'
        '{"name":"Cathode (-)","board_pin":"GND","via":""}]}],'
        '"notes":"one or two short tips"}\n\n'
        "Rules:\n"
        "1. type must be exactly one of: led, rgb_led, button, buzzer, servo, potentiometer, "
        "lm35, ultrasonic, pir, dht, relay, motor, lcd_i2c, generic.\n"
        "2. board_pin must be exactly one of: D0..D13, A0..A5, 5V, 3V3, GND, VIN.\n"
        "3. via is the part placed in series on that wire: '220 ohm' or '10k ohm' or '' (empty).\n"
        "4. Every LED needs a 220 ohm resistor on its anode wire. For rgb_led use pins "
        "Red, Green, Blue (each with 220 ohm) and Common cathode (-) to GND.\n"
        "5. Button: pin 1 to the Arduino pin used in the code, pin 2 to GND (code uses INPUT_PULLUP). "
        "If the code uses plain INPUT, add a 10k ohm pull-down from the button wire.\n"
        "6. Buzzer: Positive (+) to the pin, Negative (-) to GND.\n"
        "7. Servo: Signal to the pin, VCC (red) to 5V, GND (brown) to GND.\n"
        "8. Potentiometer: Pin 1 to 5V, Wiper (middle) to the analog pin, Pin 3 to GND.\n"
        "9. lm35: VCC to 5V, OUT to the analog pin, GND to GND. "
        "ultrasonic (HC-SR04): VCC, Trig, Echo, GND. pir: VCC, OUT, GND. dht: VCC, DATA, GND. "
        "lcd_i2c: VCC, GND, SDA to A4, SCL to A5.\n"
        "10. List a component's pins in the physical order of the part. "
        "Use the exact pin numbers from the code and add power and GND for every part that needs them.\n"
        "11. Only include parts the code really needs. If the code needs no external part, return an empty components list.\n"
        + notes_lang
    )

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.code},
            ],
        )
        raw = response.choices[0].message.content.strip()
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        return {"error": None, "circuit": data}
    except Exception as e:
        return {"error": f"Circuit banane me problem: {e}", "circuit": None}


# ---------------------------------------------------------------------------
# /compile — har request apne alag temp folder me compile hota hai (kai users ek saath
# use kar sakte hain). Compiled .hex file seedha response me wapas jaati hai,
# browser use Web Serial se board me flash karta hai.
# ---------------------------------------------------------------------------
class CompileRequest(BaseModel):
    board: str = "uno"
    code: str


@app.post("/compile")
def compile_sketch(request: CompileRequest):
    board = request.board
    if board != "uno":
        return {"compile_success": False,
                "compile_output": "Abhi online sirf Arduino Uno supported hai.",
                "board": board}
    if not request.code.strip():
        return {"compile_success": False, "compile_output": "Pehle code generate karo.", "board": board}
    if len(request.code) > 20000:
        return {"compile_success": False, "compile_output": "Code bahut bada hai.", "board": board}

    fqbn = BOARD_FQBN["uno"]
    with tempfile.TemporaryDirectory() as tmp:
        sk = os.path.join(tmp, "sketch")          # folder aur .ino ka naam ek hi hona chahiye
        os.makedirs(sk)
        with open(os.path.join(sk, "sketch.ino"), "w", encoding="utf-8") as f:
            f.write(request.code)
        out = os.path.join(tmp, "out")
        try:
            result = subprocess.run(
                [ARDUINO_CLI, "compile", "--fqbn", fqbn, "--output-dir", out, sk],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError as e:
            return {"compile_success": False,
                    "compile_output": f"arduino-cli server par nahi mila ({ARDUINO_CLI!r}): {e}",
                    "board": board}
        except subprocess.TimeoutExpired:
            return {"compile_success": False, "compile_output": "Compile timeout ho gaya.", "board": board}
        except Exception as e:
            return {"compile_success": False,
                    "compile_output": f"{type(e).__name__}: {e}", "board": board}

        if result.returncode != 0:
            return {"compile_success": False, "compile_output": result.stderr, "board": board}

        hexes = [h for h in glob.glob(os.path.join(out, "*.hex")) if "with_bootloader" not in h]
        if not hexes:
            return {"compile_success": False, "compile_output": "hex file nahi bani.", "board": board}
        with open(hexes[0], "r") as f:
            hex_text = f.read()

    return {
        "compile_success": True,
        "compile_output": result.stdout,
        "hex_text": hex_text,
        "board": board,
        "file_type": "hex",
    }


# Local-only purana endpoint. Cloud par koi COM port nahi hota, isliye khaali list.
@app.get("/ports")
def list_ports():
    return {"ports": []}


@app.get("/debug")
def debug_info():
    info = {"arduino_cli_path": ARDUINO_CLI, "cwd": os.getcwd()}
    try:
        v = subprocess.run([ARDUINO_CLI, "version"], capture_output=True, text=True, timeout=15)
        info["version"] = v.stdout.strip() or v.stderr.strip()
        c = subprocess.run([ARDUINO_CLI, "core", "list"], capture_output=True, text=True, timeout=15)
        info["installed_cores"] = c.stdout.strip() or c.stderr.strip()
    except FileNotFoundError:
        info["error"] = "arduino-cli executable not found."
    except Exception as e:
        info["error"] = str(e)
    return info