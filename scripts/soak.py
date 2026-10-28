#!/usr/bin/env python3
"""Standalone compatibility entry for the shared stress runner."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.stress import *
if __name__=='__main__':raise SystemExit(main())
