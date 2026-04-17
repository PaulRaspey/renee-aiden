#!/usr/bin/env python
"""
Bootstrap script. Windows-friendly.

Run: python scripts/bootstrap.py
"""
import sys
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def run(cmd, check=True):
    print(f"→ {cmd}")
    result = subprocess.run(cmd, shell=True, check=check)
    return result.returncode

def main():
    print("=" * 60)
    print("Renée/Aiden bootstrap")
    print("=" * 60)
    
    # Check Python version
    if sys.version_info < (3, 11):
        print(f"Need Python 3.11+, got {sys.version}")
        sys.exit(1)
    
    # Create venv
    venv = ROOT / ".venv"
    if not venv.exists():
        print("Creating venv...")
        run(f"{sys.executable} -m venv {venv}")
    
    # Activate hint
    if os.name == "nt":
        activate = venv / "Scripts" / "activate.bat"
        pip = venv / "Scripts" / "pip.exe"
    else:
        activate = venv / "bin" / "activate"
        pip = venv / "bin" / "pip"
    
    print(f"\nActivate with: {activate}")
    
    # Install deps
    print("\nInstalling requirements...")
    run(f"{pip} install --upgrade pip")
    run(f"{pip} install -r {ROOT / 'requirements.txt'}")
    
    # Create state directories
    for d in ["state", "voices/renee", "voices/aiden", "paralinguistics/renee", "paralinguistics/aiden", "logs"]:
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    
    # Check for .env
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("\nCreating .env template...")
        env_file.write_text(
            "ANTHROPIC_API_KEY=\n"
            "GROQ_API_KEY=\n"
            "UAHP_REGISTRY_URL=http://localhost:8000\n"
            "RENEE_HOME=./state\n"
            "LOG_LEVEL=INFO\n"
        )
        print(f"  → fill in {env_file}")
    
    # Check Ollama
    print("\nChecking Ollama...")
    if run("ollama --version", check=False) == 0:
        print("  ✓ Ollama present")
        run("ollama pull gemma2:2b", check=False)
    else:
        print("  ! Ollama not found. Install from https://ollama.com")
    
    # Check UAHP
    print("\nChecking UAHP...")
    try:
        import uahp  # noqa
        print(f"  ✓ UAHP {uahp.__version__}")
    except ImportError:
        print("  ! UAHP not installed. Run: pip install uahp>=0.5.4")
    
    print("\n" + "=" * 60)
    print("Bootstrap complete. Next:")
    print(f"  1. Activate venv: {activate}")
    print("  2. Fill in .env with API keys")
    print("  3. Start UAHP-Registry (separate terminal)")
    print("  4. Begin M0 per BUILD_ORDER.md")
    print("=" * 60)

if __name__ == "__main__":
    main()
