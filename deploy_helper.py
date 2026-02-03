import os
import subprocess
import sys
import shutil

def get_sam_config_params():
    """Extract parameter_overrides from samconfig.toml using tomllib."""
    config_path = "samconfig.toml"
    if not os.path.exists(config_path):
        return ""
    
    try:
        # Python 3.11+ has tomllib built-in
        import tomllib
    except ImportError:
        print("Warning: tomllib not found (requires Python 3.11+). Fallback to regex.")
        return get_sam_config_params_regex(config_path)

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        
        # Navigate to [default.deploy.parameters] -> parameter_overrides
        # Structure: {'default': {'deploy': {'parameters': {'parameter_overrides': '...'}}}}
        params = data.get("default", {}).get("deploy", {}).get("parameters", {})
        overrides = params.get("parameter_overrides", "")
        return overrides
    except Exception as e:
        print(f"Warning: Failed to parse samconfig.toml: {e}")
        return ""

def get_sam_config_params_regex(config_path):
    import re
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    # Improved regex to handle escaped quotes: "(?:[^"\\]|\\.)*"
    match = re.search(r'parameter_overrides\s*=\s*"(.*?)"', content, re.DOTALL)
    if match:
        return match.group(1).replace('\\"', '"')
    return ""

def run_deploy():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("Installing python-dotenv...")
        subprocess.run([sys.executable, "-m", "pip", "install", "python-dotenv"], check=True)
        from dotenv import load_dotenv
        load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY not found in .env file")
        sys.exit(1)

    fred_key = os.getenv("FRED_API_KEY", "")

    sam_executable = shutil.which("sam")
    if not sam_executable:
        print("Error: 'sam' executable not found in PATH")
        sys.exit(1)

    print(f"🚀 Deploying to AWS using {sam_executable}...")
    
    # Get existing params and merge
    existing_params = get_sam_config_params()
    if existing_params:
         combined_params = f"{existing_params} OpenAIApiKey={api_key} FredApiKey={fred_key}"
    else:
         combined_params = f"OpenAIApiKey={api_key} FredApiKey={fred_key}"

    cmd = [
        sam_executable, "deploy",
        "--capabilities", "CAPABILITY_IAM",
        "--force-upload",
        "--no-fail-on-empty-changeset",
        "--parameter-overrides", combined_params
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    run_deploy()
