# Automated deployment script for Windows (PowerShell)
Write-Host "🧹 Cleaning previous build artifacts..."
if (Test-Path .aws-sam) {
    Remove-Item -Recurse -Force .aws-sam
}

Write-Host "🏗️ Building SAM Application..."
# Using --use-container to ensure Python 3.12 compatibility
sam build --use-container

Write-Host "🚀 Deploying to AWS..."
sam deploy --guided --capabilities CAPABILITY_IAM
