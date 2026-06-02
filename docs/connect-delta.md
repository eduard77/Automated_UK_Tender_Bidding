# How to connect Delta

When the agent says it needs you to log into Delta, run this once on your own PC:

1. **One-time setup** (skip if you've done it before): open PowerShell in the project folder and run
   `pip install playwright==1.47.0` then `playwright install chromium`.
2. **Run the helper:** `python scripts/connect_delta.py` (or `.\connect-delta.ps1`). Enter your **operator** email and password when asked — that's your cloud-backend login, *not* your Delta login.
3. A browser opens — **log into Delta exactly as you normally do**, including the Microsoft Authenticator prompt on your phone, then leave the window alone.
4. It detects the login, captures the session, uploads it, and prints **"Done — your Delta session is now in the cloud."** That's it.

If it times out, just run it again and make sure you finish the Delta login. Your password is never stored — it's only used to sign in to the backend for the upload.
