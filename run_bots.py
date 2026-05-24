import subprocess
import sys

long  = subprocess.Popen([sys.executable, "smart_bot.py"])
short = subprocess.Popen([sys.executable, "smart_bot_short.py"])

long.wait()
short.wait()
