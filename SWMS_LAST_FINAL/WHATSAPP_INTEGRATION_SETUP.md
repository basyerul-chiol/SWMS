# SWMS WhatsApp Integration

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Configure `.env`

```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_FROM=+14155238886
TWILIO_DEFAULT_RECIPIENT=
```

Do not add `whatsapp:` in `.env`; SWMS adds it automatically.

## 3. Save employee WhatsApp number

Admin → Employees → Edit → WhatsApp number → Save.
Use `0169045976` or `+60169045976`.

For Twilio Sandbox, every destination number must join the sandbox first.

## 4. Test

- Manager creates a task: Calendar invitation + WhatsApp notification.
- Manager approves/rejects leave: Calendar invitation for approval + WhatsApp status notification.

WhatsApp errors do not cancel task creation or leave processing. The result is shown as a flash message and saved in the task/leave JSON record.
