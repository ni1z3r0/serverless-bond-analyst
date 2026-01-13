# Automated deployment script for Windows (PowerShell)
Write-Host "Cleaning previous build artifacts..."
if (Test-Path .aws-sam) { Remove-Item -Recurse -Force .aws-sam }

Write-Host "Building SAM Application..."
sam build --use-container

Write-Host "Loading secrets and deploying..."
# Execute the Python helper directly
python deploy_helper.py
