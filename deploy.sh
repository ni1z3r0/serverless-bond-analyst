#!/bin/bash
# automated deployment script
echo "🏗️ Building SAM Application..."
sam build --use-container

echo "🚀 Deploying to AWS..."
sam deploy --guided --capabilities CAPABILITY_IAM
