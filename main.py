from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from groq import Groq
from dotenv import load_dotenv
import os
import glob
import shutil
import subprocess

load_dotenv()

app = FastAPI()
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")

# --- CORS: taaki aapki website kisi bhi domain se is backend ko call kar sake ---
# (Jab aap frontend ko bhi cloud/CDN par daalenge, iski zaroorat padegi)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # production me isko apni actual website ke domain tak limit kar dena
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

SKETCH_DIR = "generated_sketch"
SKETCH_FILE = os.path.join(SKETCH_DIR, "generated_sketch.ino")

# --- NAYA: har compile ka output ek fixed, jaana-pehchana folder me jaayega ---
BUILD_DIR = "build_output"

BOARD_FQBN = {
    "uno": "arduino:avr:uno",
    "esp32": "esp32:esp32:esp32",
    "esp8266": "esp8266:esp8266:generic",
}

# Kis board ka firmware kis file-type me hota hai, yeh batata hai
BOARD_FILE_TYPE = {
    "uno": "hex",
    "esp32": "bin",
    "esp8266": "bin",
}

hardware_config = {}

class HardwareConfig(BaseModel):
    components: dict

@app.post("/set-hardware")
def set_hardware(config: HardwareConfig):
    global hardware_config
    hardware_config = config.components
    return {"status": "saved", "hardware": hardware_config}

DANGEROUS_KEYWORDS = [
    "220v", "230v", "ac power", "mains", "short",
    "gnd to vcc", "vcc to gnd", "reverse polarity"
]

def is_dangerous(text: str) -> bool:
    return any(k in text.lower() for k in DANGEROUS_KEYWORDS)


class CommandRequest(BaseModel):
    command: str

@app.post("/generate")
def generate_code(request: CommandRequest):
    """Sirf code generate karta hai. Compile/flash nahi karta."""
    if is_dangerous(request.command):
        return {"error": "This command was blocked for safety reasons.", "code": None}

    hardware_info = ""
    if hardware_config:
        hardware_info = "Hardware wiring:\n" + "\n".join(
            f"- {name} is connected to pin {pin}" for name, pin in hardware_config.items()
        )

    system_prompt = (
        "You are an Arduino code generator. Given a plain-language "
        "instruction, output ONLY valid Arduino C++ sketch code "
        "(setup() and loop() functions). Do not include explanations, "
        "markdown formatting, or code fences — just raw code.\n\n"
        f"{hardware_info}\n"
        "Use these exact pin numbers when the instruction refers to "
        "these components by name."
    )

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.command}
        ]
    )

    generated_code = response.choices[0].message.content

    if is_dangerous(generated_code):
        return {"error": "Generated code was blocked for safety reasons.", "code": None}

    os.makedirs(SKETCH_DIR, exist_ok=True)
    with open(SKETCH_FILE, "w") as f:
        f.write(generated_code)

    return {"error": None, "code": generated_code}


class CompileRequest(BaseModel):
    board: str = "uno"

@app.post("/compile")
def compile_sketch(request: CompileRequest):
    """
    Abhi jo bhi sketch save hai (last /generate se), usko compile karta hai.
    NAYA: Ab yeh --output-dir use karta hai taaki compiled .hex/.bin file
    ek fixed jagah (BUILD_DIR) me mile, jise baad me /download-firmware
    endpoint se browser tak bheja ja sake. Isme kahin bhi local COM port
    ki zaroorat nahi hai — isliye yeh cloud server par bhi chal sakta hai.
    """
    if not os.path.exists(SKETCH_FILE):
        return {"compile_success": False, "compile_output": "No sketch found. Generate code first."}

    board = request.board
    fqbn = BOARD_FQBN.get(board, BOARD_FQBN["uno"])

    os.makedirs(BUILD_DIR, exist_ok=True)
    # Purane build files hata do taaki purani file naye compile ke saath mix na ho
    for f in glob.glob(os.path.join(BUILD_DIR, "*")):
        try:
            os.remove(f)
        except OSError:
            pass

    # NAYA: agar arduino-cli hi na mile (ya koi aur unexpected error aaye), to
    # server crash karke raw "Internal Server Error" na de — ek saaf JSON
    # error wapas kare taaki frontend usko theek se dikha sake.
    try:
        result = subprocess.run(
            [
                "arduino-cli", "compile",
                "--fqbn", fqbn,
                "--output-dir", BUILD_DIR,
                "--export-binaries",
                SKETCH_DIR,
            ],
            capture_output=True,
            text=True
        )
    except FileNotFoundError:
        return {
            "compile_success": False,
            "compile_output": "arduino-cli not found on this server. Check the Dockerfile install step and the /debug endpoint.",
            "firmware_ready": False,
            "firmware_files": [],
            "board": board,
            "file_type": BOARD_FILE_TYPE.get(board, "bin"),
        }
    except Exception as e:
        return {
            "compile_success": False,
            "compile_output": f"Unexpected server error while compiling: {e}",
            "firmware_ready": False,
            "firmware_files": [],
            "board": board,
            "file_type": BOARD_FILE_TYPE.get(board, "bin"),
        }

    success = result.returncode == 0

    firmware_ready = False
    firmware_files = []
    if success:
        firmware_files = [os.path.basename(f) for f in glob.glob(os.path.join(BUILD_DIR, "*"))]
        firmware_ready = len(firmware_files) > 0

    return {
        "compile_success": success,
        "compile_output": result.stdout if success else result.stderr,
        "firmware_ready": firmware_ready,
        "firmware_files": firmware_files,   # frontend ko pata chalega kaunsi files download karni hain
        "board": board,
        "file_type": BOARD_FILE_TYPE.get(board, "bin"),
    }


@app.get("/download-firmware/{filename}")
def download_firmware(filename: str):
    """
    NAYA endpoint: compile ke baad ban chuki firmware file (.hex ya .bin)
    ko seedha browser me download/fetch karne ke liye.
    Phase 4 me Web Serial (esptool-js / avrgirl-arduino) isi file ko
    fetch karke user ke board par likhega.
    """
    # Security: sirf BUILD_DIR ke andar ki file hi serve karo, koi bhi random
    # file-path traversal allow mat karo
    safe_name = os.path.basename(filename)
    file_path = os.path.join(BUILD_DIR, safe_name)
    if not os.path.exists(file_path):
        return {"error": "File not found. Compile first."}
    return FileResponse(file_path, filename=safe_name, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# NEECHE WAALE DONO ENDPOINTS SIRF LOCAL USE KE LIYE HAIN
# (jab aap apne hi PC par, apne hi board ke saath test kar rahe ho).
# Cloud par deploy karne par yeh kaam nahi karenge kyunki wahan koi
# physical COM port hota hi nahi — isliye frontend inhe sirf tab call
# karega jab board Web Serial se connect na ho.
# ---------------------------------------------------------------------------

class FlashRequest(BaseModel):
    port: str
    board: str = "uno"

@app.post("/flash")
def flash_sketch(request: FlashRequest):
    """(Local-only) Compiled sketch ko diye gaye port par upload karta hai."""
    fqbn = BOARD_FQBN.get(request.board, BOARD_FQBN["uno"])

    try:
        result = subprocess.run(
            ["arduino-cli", "upload", "-p", request.port, "--fqbn", fqbn, SKETCH_DIR],
            capture_output=True,
            text=True
        )
    except FileNotFoundError:
        return {"upload_success": False, "upload_output": "arduino-cli not found on this server."}
    except Exception as e:
        return {"upload_success": False, "upload_output": f"Unexpected server error while flashing: {e}"}

    success = result.returncode == 0
    return {
        "upload_success": success,
        "upload_output": result.stdout if success else result.stderr
    }


@app.get("/ports")
def list_ports():
    """(Local-only) Connected COM ports ki list deta hai."""
    try:
        result = subprocess.run(
            ["arduino-cli", "board", "list"],
            capture_output=True,
            text=True
        )
    except FileNotFoundError:
        return {"ports": []}
    except Exception:
        return {"ports": []}

    ports = []
    for line in result.stdout.splitlines()[1:]:
        if line.strip():
            parts = line.split()
            if parts and parts[0].startswith("COM"):
                ports.append(parts[0])
    return {"ports": ports}


# ---------------------------------------------------------------------------
# NAYA: DEBUG endpoint — yeh batata hai ki server par arduino-cli mila ya
# nahi, uska version kya hai, aur konse board "cores" install hain.
# Ise browser me kholkar (GET request) seedha JSON dikh jaayega — koi
# terminal/shell access ki zaroorat nahi. Kaam ho jaane ke baad ise hata
# dena behtar hai (security ke liye), lekin abhi debugging ke liye rakhna
# theek hai.
# ---------------------------------------------------------------------------
@app.get("/debug")
def debug_info():
    info = {}

    info["arduino_cli_path"] = shutil.which("arduino-cli")

    try:
        v = subprocess.run(["arduino-cli", "version"], capture_output=True, text=True, timeout=15)
        info["version_stdout"] = v.stdout.strip()
        info["version_stderr"] = v.stderr.strip()
        info["version_returncode"] = v.returncode
    except FileNotFoundError:
        info["version_error"] = "arduino-cli executable not found (FileNotFoundError)."
    except Exception as e:
        info["version_error"] = str(e)

    try:
        c = subprocess.run(["arduino-cli", "core", "list"], capture_output=True, text=True, timeout=15)
        info["installed_cores_stdout"] = c.stdout.strip()
        info["installed_cores_stderr"] = c.stderr.strip()
    except FileNotFoundError:
        info["core_list_error"] = "arduino-cli executable not found (FileNotFoundError)."
    except Exception as e:
        info["core_list_error"] = str(e)

    info["sketch_file_exists"] = os.path.exists(SKETCH_FILE)
    info["build_dir_exists"] = os.path.exists(BUILD_DIR)
    info["path_env"] = os.environ.get("PATH", "")

    return info