#!/usr/bin/env python3
"""
TradeSight Continuous Paper Trader Launcher
Double-click this file to start the continuous trading loop.
"""
import os
import sys

def main():
    # Resolve project directory relative to this script
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)
    
    # Auto-redirect to local venv python if running with system python
    venv_python = os.path.abspath(os.path.join(project_dir, ".venv", "bin", "python"))
    if os.path.exists(venv_python) and ".venv" not in sys.executable:
        os.execv(venv_python, [venv_python] + sys.argv)

    print("🎯 TradeSight Paper Trading Loop Launcher")
    print("=" * 60)
    sys.path.insert(0, project_dir)
    sys.path.append(os.path.join(project_dir, "src"))
    
    # Locate core runner script
    script = os.path.join(project_dir, "run_paper_trader.py")
    
    # Execute loop via execv
    try:
        os.execv(sys.executable, [sys.executable, script, "--loop"])
    except Exception as e:
        print(f"❌ Launcher Error: {e}")
        input("\nPress Enter to exit...")

if __name__ == "__main__":
    main()
