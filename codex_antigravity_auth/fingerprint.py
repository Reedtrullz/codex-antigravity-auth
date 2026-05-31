import uuid
import secrets
import random
import time

PLATFORM_CHOICES = ["darwin", "win32"]
ARCHITECTURES = ["x64", "arm64"]
OS_VERSIONS = {
    "darwin": ["10.15.7", "11.6.8", "12.6.3", "13.5.2", "14.2.1", "14.5"],
    "win32": ["10.0.19041", "10.0.19042", "10.0.19043", "10.0.22000", "10.0.22621", "10.0.22631"],
}
SDK_CLIENTS = [
    "google-cloud-sdk vscode_cloudshelleditor/0.1",
    "google-cloud-sdk vscode/1.86.0",
    "google-cloud-sdk vscode/1.96.0",
]

def generate_device_id() -> str:
    return str(uuid.uuid4())

def generate_session_token() -> str:
    return secrets.token_hex(16)

def generate_fingerprint() -> dict:
    platform = random.choice(PLATFORM_CHOICES)
    arch = random.choice(ARCHITECTURES)
    os_version = random.choice(OS_VERSIONS[platform])
    sdk_client = random.choice(SDK_CLIENTS)

    chrome_major = random.randint(130, 140)
    electron_major = random.randint(32, 38)
    if platform == "darwin":
        os_ua = f"Macintosh; Intel Mac OS X {os_version.replace('.', '_')}"
    else:
        os_ua = f"Windows NT {os_version.split('.')[0]}.{os_version.split('.')[1]}; Win64; x64"

    user_agent = (
        f"Mozilla/5.0 ({os_ua}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Antigravity/2.0.0 "
        f"Chrome/{chrome_major}.0.7204.235 "
        f"Electron/{electron_major}.3.1 Safari/537.36"
    )

    platform_display = "WINDOWS" if platform == "win32" else "MACOS"

    return {
        "deviceId": generate_device_id(),
        "sessionToken": generate_session_token(),
        "userAgent": user_agent,
        "apiClient": sdk_client,
        "clientMetadata": {
            "ideType": "ANTIGRAVITY",
            "platform": platform_display,
            "pluginType": "GEMINI",
        },
        "createdAt": int(time.time() * 1000),
    }
