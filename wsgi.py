# PythonAnywhere WSGI entry (optional helper).
# Prefer pointing the Web tab WSGI file at this module, or paste the snippet from DEPLOY-PYTHONANYWHERE.txt
import sys
import os

# Project folder = directory containing this file
project = os.path.dirname(os.path.abspath(__file__))
if project not in sys.path:
    sys.path.insert(0, project)

# Optional: set a strong secret in PA Web → Environment variables: EHA_SECRET_KEY
from demo_server import app as application
