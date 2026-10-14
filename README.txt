EHA Registration (Flask + SQLite)
================================

LOCAL (Windows laptop)
----------------------
  python -m pip install flask reportlab
  start-demo.bat
  or: python demo_server.py
  Open http://localhost:3000

ONLINE (PythonAnywhere)
-----------------------
  See DEPLOY-PYTHONANYWHERE.txt for full steps.
  Summary:
    1. Upload this folder to /home/YOUR_USERNAME/handball-sqlite
    2. pip3.10 install --user flask reportlab
    3. Web → Manual config → WSGI points to demo_server:app
    4. Static files: /static/ → .../handball-sqlite/static/
    5. Reload → https://YOUR_USERNAME.pythonanywhere.com

Default accounts (first empty DB only):
  admin / admin123
  officer / officer123
  coach_lions / coach123
