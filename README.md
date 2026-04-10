# ebookassistant

A web app to convert Adobe ACSM ebook tokens to DRM-free EPUB files.

## Authentication

This app uses **Google OAuth2**. Only the email address set in `ALLOWED_EMAIL` can log in.

## Environment variables (set in Zeabur)

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | From Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | From Google Cloud Console |
| `ALLOWED_EMAIL` | Your Google account email — only this address can log in |
| `SECRET_KEY` | Random string for Flask sessions |

## Google OAuth2 setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → APIs & Services → Credentials → OAuth 2.0 Client ID
3. Add authorised redirect URI: `https://YOUR-DOMAIN.zeabur.app/auth/callback`
4. Copy Client ID and Client Secret into Zeabur variables
