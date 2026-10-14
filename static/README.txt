EHA Registration
================
1. Install (once):
     python -m pip install flask reportlab

   Or just double-click start-demo.bat (it installs deps for you).

2. Run:
     start-demo.bat
   or:
     python demo_server.py

3. Open http://localhost:3000

PDF team cards need reportlab. If you see:
  "PDF engine not installed (reportlab)"
run:
  python -m pip install reportlab
then restart the server.

Default seed accounts (first install only — change after first login):
  admin / admin123
  officer / officer123
  coach_lions / coach123
