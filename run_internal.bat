@echo off
REM Launch the Cost Reconciler for internal LAN access.
REM Binding, port and headless mode come from .streamlit/config.toml.
REM Others reach it at  http://<this-PC-IPv4>:8501  (run `ipconfig` for the IP).
REM Access is restricted by the Windows Firewall allowlist on port 8501 -- see
REM INTERNAL_HOSTING.md. Keep this window open; closing it stops the app.
cd /d "%~dp0"
streamlit run app.py
