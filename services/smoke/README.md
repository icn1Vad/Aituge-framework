# Aituge Smoke Service

This deliberately small service verifies that Framework can mount more than one
independent capability entry. It exposes a deterministic `smoke_echo` HTTP tool
and registers the `smoke.echo.chat` Task without a Skill package.

Run it locally with:

```bash
uvicorn aituge_smoke.app:app --app-dir src --host 127.0.0.1 --port 18200
```
