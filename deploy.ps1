# Automated deployment script for Windows (PowerShell)
param(
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$S3_BUCKET = "bond-dashboard-rc11292025"
$CLOUDFRONT_DISTRIBUTION_ID = "EVJ7GFWAVQS8R"  
# --- BACKEND DEPLOYMENT ---
if (-not $SkipBackend) {
    Write-Host "`n=== BACKEND DEPLOYMENT ===" -ForegroundColor Cyan

    Write-Host "Cleaning previous build artifacts..."
    if (Test-Path .aws-sam) { Remove-Item -Recurse -Force .aws-sam }

    Write-Host "Building SAM Application..."
    sam build --use-container

    Write-Host "Loading secrets and deploying..."
    python deploy_helper.py
}
else {
    Write-Host "`n=== SKIPPING BACKEND ===" -ForegroundColor Yellow
}

# --- FRONTEND DEPLOYMENT ---
if (-not $SkipFrontend) {
    Write-Host "`n=== FRONTEND DEPLOYMENT ===" -ForegroundColor Cyan

    Write-Host "Uploading frontend to S3..."
    aws s3 cp frontend/index.html "s3://$S3_BUCKET/index.html" --content-type "text/html" --cache-control "max-age=300"

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Frontend uploaded successfully!" -ForegroundColor Green

        # Invalidate CloudFront cache
        Write-Host "Invalidating CloudFront cache..."
        aws cloudfront create-invalidation --distribution-id $CLOUDFRONT_DISTRIBUTION_ID --paths "/index.html" 2>$null

        if ($LASTEXITCODE -eq 0) {
            Write-Host "CloudFront cache invalidation started!" -ForegroundColor Green
        }
        else {
            Write-Host "CloudFront invalidation failed (check distribution ID)" -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "Frontend upload failed!" -ForegroundColor Red
    }
}
else {
    Write-Host "`n=== SKIPPING FRONTEND ===" -ForegroundColor Yellow
}

Write-Host "`n=== DEPLOYMENT COMPLETE ===" -ForegroundColor Green
