# Setup Custom Models in Codex Desktop via codex-shim

> Historical plan retained for provenance. Current Codex setup should use the native `/v1/models` endpoint plus `codex-antigravity configure-codex --write`; see `README.md`, `USAGE.md`, and `VERIFICATION.md`.

This script automates cloning `codex-shim`, writing a secure local models configuration file containing your Google Antigravity gateway models, and patching Codex Desktop's visual model picker dropdown so that you can select your Antigravity models directly from the GUI.

## 1. Clone & Install codex-shim
Open your terminal and run:
```bash
git clone https://github.com/0xSero/codex-shim ~/codex-shim
cd ~/codex-shim
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 2. Configure codex-shim Models (`~/.codex-shim/models.json`)
Create the configuration directory:
```bash
mkdir -p ~/.codex-shim
```

Write the following model declarations to `~/.codex-shim/models.json`. This tells `codex-shim` to route the corresponding model picker choices to our local Antigravity gateway:
```json
{
  "models": [
    {
      "model": "gemini-3.5-flash-high",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:51122/v1",
      "api_key": "antigravity-key",
      "display_name": "Gemini 3.5 Flash (Agent High)"
    },
    {
      "model": "gemini-3.5-flash-medium",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:51122/v1",
      "api_key": "antigravity-key",
      "display_name": "Gemini 3.5 Flash (General)"
    },
    {
      "model": "gemini-3.1-pro-high",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:51122/v1",
      "api_key": "antigravity-key",
      "display_name": "Gemini 3.1 Pro (Reasoning)"
    },
    {
      "model": "claude-3.5-sonnet",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:51122/v1",
      "api_key": "antigravity-key",
      "display_name": "Claude 3.5 Sonnet (Google)"
    },
    {
      "model": "claude-opus-4-6",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:51122/v1",
      "api_key": "antigravity-key",
      "display_name": "Claude Opus 4.6 (Google)"
    }
  ]
}
```

## 3. Generate Catalog and Bind codex-shim
With your virtual environment active, run:
```bash
# Generate the custom pick list catalogs
codex-shim generate

# Patch Codex Desktop so the model dropdown hooks into codex-shim
codex-shim app .
```

## 4. Run the Stack
Start your Antigravity gateway, the shim server, and Codex:
1. Terminal 1 (Start Antigravity Gateway):
   ```bash
   codex-antigravity start
   ```
2. Terminal 2 (Start Shim server):
   ```bash
   codex-shim start
   ```

Now, open your **Codex Desktop** app! The "Model" picker dropdown in the lower right of your screen will now display your custom Google Antigravity options alongside the standard options, allowing you to switch between them visual on the fly!
