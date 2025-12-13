# 📉 Serverless AI Bond & Equity ETF Analyst

![Build Status](https://img.shields.io/badge/status-draft-orange)
![Language](https://img.shields.io/badge/language-Python%20%2B%20JS-blue)
![Built with](https://img.shields.io/badge/Built%20with-Google%20Antigravity-4285F4)

An automated, event-driven financial dashboard that scrapes market data, performs quantitative regression analysis, and uses GPT-4o to generate "CIO-style" daily strategy reports.

## 🏗️ Architecture
This project uses a **Serverless Microservices Architecture**, now fully managed via **AWS SAM (Serverless Application Model)** for robust Infrastructure-as-Code (IaC) deployment.

**The Pipeline:**
1.  **Ingestion (BondDataScraper):** A scheduled Python Lambda scrapes daily yield/duration data for ETFs and stores it in an **S3 Data Lake**.
2.  **Analysis (BondAnalyst):** An **S3 Event Trigger** wakes the Analyst Lambda. It fetches live macro data (FRED API), calculates yield curve anomalies, and prompts **GPT-4o** to generate a written market summary.
3.  **Chatbot (ChatAnalyst):** A RAG-enabled Lambda provides a conversational interface to the market data, secured by **Email-Based Access Control**.
4.  **Delivery (EmailForwarder):** A final Lambda consumes an SQS Queue to deliver reports via **AWS SES** (decoupled for reliability).
5.  **Visualization:** A static HTML/JS dashboard hosted in S3 (served via **CloudFront**) fetches the raw data to render interactive charts.

## 🛠️ Tech Stack
* **Cloud:** AWS SAM (managing Lambda, S3, SQS, SES, EventBridge, IAM)
* **Language:** Python 3.12 (Analyst/Chat), Python 3.10 (Mailer/Scraper)
* **Security:** Cognito (Frontend Auth) + S3 Allowlist (Chat Backend Auth)
* **AI:** OpenAI GPT-4o (Structured Outputs)
* **Frontend:** HTML5, CSS3, Chart.js, CloudFront CDN

## ⚡ Quick Start
### Prerequisites
* AWS CLI installed and configured
* AWS SAM CLI installed
* Docker (optional, for local builds)

### 1. Deployment
The entire backend stack is defined in `template.yaml`. Deploy it with a single command:

```powershell
.\deploy.ps1
```

This script handles:
* Clearing build artifacts
* Building the SAM application (using containers if needed)
* Deploying the CloudFormation stack (`sam-app`)

### 2. Frontend Setup
1.  Upload the frontend code to your S3 bucket:
    ```powershell
    aws s3 cp frontend/index.html s3://your-bucket-name/index.html
    ```
2.  Invalidate CloudFront cache (to see changes immediately):
    ```powershell
    aws cloudfront create-invalidation --distribution-id YOUR_DIST_ID --paths "/index.html"
    ```

### 3. Access Control (Chatbot)
The Chat Analyst is secured to specific subscribers.
1.  Edit `infrastructure/subscribers.json` to add authorized emails.
2.  Upload to S3:
    ```powershell
    aws s3 cp infrastructure/subscribers.json s3://your-bucket-name/infrastructure/subscribers.json
    ```

## 🚀 Key Features
* **Infrastructure as Code:** Fully reproducible stack via AWS SAM.
* **Secure Chat:** Hybrid authentication using Cognito (Frontend) and S3-based Allowlist (Backend).
* **Self-Healing:** SQS queues ensure email delivery even if SES is temporarily down.
* **Smart RAG:** Self-feeding knowledge base that learns from its own daily reports.

## 🛠️ Troubleshooting & Tips
- If the frontend shows "Report pending for today.", verify the object `reports/ai_analysis_YYYY-MM-DD.json` exists in your bucket and is publicly readable (or accessible via CloudFront).
- If the Analyst Lambda is triggered multiple times, ensure the S3 trigger suffix is set to `upload_complete.json` and that your scraper writes exactly one completion object per run.
- For Cognito: ensure the App Client's redirect URI matches `REDIRECT_URI` and that `response_type=code` is enabled for the app.
- If emails are not delivered, check SES sandbox limits and verify both sender and recipient email addresses.
- Add CloudWatch logs and set the Lambda role to allow `logs:CreateLogGroup` and `logs:CreateLogStream` for troubleshooting.

---

## 📈 Architecture Diagram
```mermaid
graph LR
    subgraph "Ingestion Layer"
        direction TB
        Scheduler["⏰ EventBridge Schedule"] -->|Trigger| Scraper("λ Bond Scraper")
        Scraper -->|Write Raw Data| S3[(S3 Data Lake)]
    end

    subgraph "Processing Layer"
        direction TB
        Analyst(λ Bond Analyst) -->|Fetch Macros| API["FRED & Yahoo APIs"]
        Analyst -->|Generate Report| GPT["🧠 GPT-4o"]
        Analyst -->|Publish Payload| SQS{"AWS SQS Queue"}
        
        %% The Critical Link: Analyst updates S3 with processed data
        Analyst -.->|Save Dashboard JSON| S3
        Analyst -.->|Read Context| Context[(Context Index)]
    end

    subgraph "Context Layer (Local)"
        direction TB
        CLI["💻 manage_context.py"] -->|Scrape & Embed| Context["index.faiss + metadata.json"]
        CLI -->|Upload| S3
    end

    subgraph "Delivery Layer"
        direction TB
        Mailer("λ Email Forwarder") -->|Send| SES[AWS SES]
        SES -->|Deliver| Inbox["📧 User Inbox"]
    end

    subgraph "Visualization Layer"
        direction TB
        Dashboard["📊 Web Dashboard"] -->|Render| Browser["🖥️ User Browser"]
    end

    %% Critical Workflow Connections
    S3 -->|Object Create Trigger| Analyst
    SQS -->|Trigger| Mailer
    S3 -.->|Fetch Processed Data| Dashboard
    
    style SQS fill:#ff9900,stroke:#232f3e,color:white
    style S3 fill:#569a31,stroke:#232f3e,color:white
    style GPT fill:#74aa9c,stroke:#232f3e,color:white
    style Dashboard fill:#8e44ad,stroke:#232f3e,color:white
```
