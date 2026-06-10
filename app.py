import subprocess
import sys


def run_command(command_list):
    """Helper function to run a system command and exit if it fails."""
    try:
        subprocess.check_call(command_list)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error executing command: {' '.join(command_list)}")
        sys.exit(e.returncode)


# 1. Update pip to the latest version
print("🔄 Upgrading pip...")
run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])

# 2. Install dependencies (Streamlit must be listed in requirements.txt)
print("\n📦 Installing requirements.txt...")
run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

# 3. Launch the Streamlit application
print("\n🚀 Launching Streamlit App...\n")
# sys.executable points to python, -m streamlit run main.py executes the module safely
run_command([sys.executable, "-m", "streamlit", "run", "main.py"])
