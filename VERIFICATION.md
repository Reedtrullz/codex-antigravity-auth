## Google Antigravity Auth for OpenAI Codex Setup & Verification Plan

### Prerequisites
Before running, you must configure a Google Desktop OAuth Client ID and Client Secret in the Google Cloud Console. Set up the redirect URI as `http://localhost:51121/oauth-callback`.

Then configure these credentials:
- Option A: Export them in your environment:
  ```bash
  export ANTIGRAVITY_CLIENT_ID="your-client-id.apps.googleusercontent.com"
  export ANTIGRAVITY_CLIENT_SECRET="your-client-secret"
  ```
- Option B: Write them to `~/.codex/antigravity-credentials.json`:
  ```json
  {
    "client_id": "your-client-id.apps.googleusercontent.com",
    "client_secret": "your-client-secret"
  }
  ```

---

### Step-by-Step Verification

#### 1. Setup the virtual environment
Ensure you have cloned this repository, set up a virtual environment, and installed it in editable mode:
```bash
uv pip install -e .
```

#### 2. Run Diagnostics (`doctor`)
Verify your client credentials configuration and existing setup:
```bash
codex-antigravity doctor
```
Ensure that the Google OAuth credentials check shows `[PASS]`.

#### 3. Log In (`login`)
Run the interactive PKCE OAuth login process:
```bash
codex-antigravity login
```
This will open your browser to choose a Google account. Follow the prompts and return to the terminal. You should see:
`[+] Successfully authenticated new Google Account: <email>`

#### 4. Configure Codex
Add the custom `model_provider` to your `~/.codex/config.toml`:
```toml
model = "gemini-3.5-flash-high"
model_provider = "antigravity"
wire_api = "responses"

[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
```

#### 5. Start the Server
Start the local Responses API gateway:
```bash
codex-antigravity start
```

#### 6. Verify with Codex
Now trigger an action inside Codex to verify everything functions end-to-end. Codex will automatically route its Responses API requests to your local gateway server, translating them to Google Antigravity and streaming the response back!
