import subprocess
import sys


def run_command(command_list):
    """Helper function to run a system command and exit if it fails."""
    try:
        subprocess.check_call(command_list)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error executing command: {' '.join(command_list)}")
        sys.exit(e.returncode)


# 1. Update pip using 'python' command explicitly
print("🔄 Upgrading pip...")
run_command(["python", "-m", "pip", "install", "--upgrade", "pip"])

# 2. Install dependencies using 'python' command explicitly
print("\n📦 Installing requirements.txt...")
run_command(["python", "-m", "pip", "install", "-r", "requirements.txt"])

# 3. Launch the Streamlit application using 'python' command explicitly
print("\n🚀 Launching Streamlit App...\n")
run_command(["python", "-m", "streamlit", "run", "main.py"])
