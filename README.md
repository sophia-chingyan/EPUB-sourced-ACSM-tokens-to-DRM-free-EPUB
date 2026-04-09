# ebookassistant

A web app to convert Adobe ACSM ebook tokens to DRM-free EPUB files.

## Authentication

This app uses **TOTP (Time-based One-Time Password)** — compatible with Google Authenticator, Authy, 1Password, etc.

### First-time setup

1. **Generate a secret** (run once locally):
   ```bash
   python3 -c "import pyotp; print(pyotp.random_base32())"
   ```

2. **Set environment variables** in Zeabur (or your host):
   - `TOTP_SECRET` — the base32 string generated above
   - `SECRET_KEY` — a long random string for Flask sessions:
     ```bash
     python3 -c "import secrets; print(secrets.token_hex(32))"
     ```

3. **Scan the QR code** — visit `/setup` on your deployed app and scan with Google Authenticator.

4. After scanning, you can restrict access to `/setup` or leave it (it only shows setup info, no actions).

### Logging in

Open Google Authenticator, find "ACSM Converter", and enter the current 6-digit code.
