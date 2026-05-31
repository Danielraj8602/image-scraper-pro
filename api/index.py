import sys
import os

# Add the project root to python path so backend.app is importable
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app import app
