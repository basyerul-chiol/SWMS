# SWMS Render + Google Calendar Setup

## 1. Push this clean folder to GitHub
The real `.env`, `credentials.json`, and `google_tokens/` files are intentionally excluded.

## 2. Create the Render Web Service
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

## 3. Add Render Environment Variables
Add the real values in Render > Environment:

- `SECRET_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI=https://YOUR-SERVICE.onrender.com/google-calendar/callback`
- `GOOGLE_TOKEN_FOLDER=/tmp/google_tokens`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`
- Database variables shown in `.env.example`

## 4. Google Cloud Console
Create an OAuth Client ID of type **Web application**. Add this exact Authorized redirect URI:

`https://YOUR-SERVICE.onrender.com/google-calendar/callback`

Enable Google Calendar API. If the OAuth consent screen is in Testing mode, add the lecturer/demo Google account as a Test user.

## 5. Demonstration
1. Open the deployed website and log in.
2. Open Google Calendar integration.
3. Click Connect Google Calendar and approve access.
4. Click Add Test Event.
5. Open Google Calendar to show the event.

Note: on Render free service, `/tmp` is temporary. After a service restart, reconnect Google Calendar before the demonstration.
