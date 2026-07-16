#!/usr/bin/env python3
"""
TradeSight Quick Launcher
Double-click this file to start TradeSight
"""

import os
import sys
import time

def main():
    print("🎯 TradeSight - Trading Intelligence Platform")
    print("=" * 50)
    print("🚀 Starting dashboard...")
    
    # Change to TradeSight directory (resolve relative to this script)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)
    # Prioritize project root to load the new web/dashboard.py, append src for internal imports
    sys.path.insert(0, project_dir)
    sys.path.append(os.path.join(project_dir, "src"))
    
    print("🌐 Dashboard will be at: http://localhost:5000")
    print("💡 Please manually open http://localhost:5000 in your browser")
    print("⚠️  Keep this window open while using TradeSight")
    print("")
    
    # Start Flask app
    try:
        from web.dashboard import app
        print("⚡ Web server starting...")
        app.run(host="127.0.0.1", port=5000, debug=False)
    except Exception as e:
        print(f"❌ Error: {e}")
        print(f"💡 Try running from terminal: cd '{project_dir}' && python3 web/dashboard.py")
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
