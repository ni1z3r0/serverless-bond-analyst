import boto3
import urllib.request
import json
import os
import re
import datetime
import time

# --- CONFIG ---
BUCKET_NAME = os.environ.get('BUCKET_NAME')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
s3 = boto3.client('s3')

def get_config():
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key="infrastructure/portfolio_config.json")
        return json.loads(obj['Body'].read())
    except Exception as e:
        try:
            obj = s3.get_object(Bucket=BUCKET_NAME, Key="portfolio_config.json")
            return json.loads(obj['Body'].read())
        except: return []

def extract_with_ai(ticker, text, fund_type):
    print(f"🤖 AI Extracting (Structured): {ticker}...")
    
    # 1. Define strict Schemas (Same as before)
    if fund_type == 'bond':
        schema_name = "bond_fund_metrics"
        keys = ["Price", "Yield", "Duration", "OAS", "YTM", "Coupon", "Maturity", "Convexity"]
        schema_definition = {
            "type": "object",
            "properties": {
                "Price": { "type": ["number", "null"], "description": "Current Price or NAV." },
                "Yield": { "type": ["number", "null"], "description": "30-Day SEC Yield in percent." },
                "Duration": { "type": ["number", "null"], "description": "Effective Duration in years." },
                "OAS": { "type": ["number", "null"], "description": "Option Adjusted Spread in basis points." },
                "YTM": { "type": ["number", "null"], "description": "Average Yield to Maturity in percent." },
                "Coupon": { "type": ["number", "null"], "description": "Weighted Average Coupon in percent." },
                "Maturity": { "type": ["number", "null"], "description": "Weighted Average Maturity in years." },
                "Convexity": { "type": ["number", "null"], "description": "Convexity." }
            },
            "required": keys, 
            "additionalProperties": False
        }
    else:
        schema_name = "equity_fund_metrics"
        keys = ["Price", "PE_Ratio", "Beta", "Price_Book", "Div_Yield", "Std_Dev"]
        schema_definition = {
            "type": "object",
            "properties": {
                "Price": { "type": ["number", "null"], "description": "Current Price." },
                "PE_Ratio": { "type": ["number", "null"], "description": "Price to Earnings Ratio." },
                "Beta": { "type": ["number", "null"], "description": "Beta (3y)." },
                "Price_Book": { "type": ["number", "null"], "description": "Price to Book Ratio." },
                "Div_Yield": { "type": ["number", "null"], "description": "12m Trailing Dividend Yield in percent." },
                "Std_Dev": { "type": ["number", "null"], "description": "Standard Deviation (3y) in percent." }
            },
            "required": keys,
            "additionalProperties": False
        }

    # 2. Call OpenAI
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    
    payload = {
        # COST FIX: Switched from 'gpt-4o-2024-08-06' to 'gpt-4o-mini'
        # gpt-4o-mini is ~95% cheaper and supports Structured Outputs
        "model": "gpt-4o-mini", 
        "messages": [
            {"role": "system", "content": "Extract data. Return null if not found."}, 
            {"role": "user", "content": text[:15000]} 
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema_definition
            }
        },
        "temperature": 0
    }

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = json.loads(response.read())
            return json.loads(res_body['choices'][0]['message']['content'])
            
    except Exception as e:
        print(f"[ALERT:OpenAI] AI Extraction Failed for {ticker}: {e}")
        return None
    
def scrape_ishares_page(fund):
    ticker = fund['ticker']
    print(f"Fetching HTML for {ticker}...")
    
    try:
        req = urllib.request.Request(
            fund['url'], 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0'}
        )
        
        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read().decode('utf-8', errors='ignore')

        # 1. HYBRID STEP: Run Regex for Price IMMEDIATELY (It's reliable)
        # Looks for the first occurrence of $XX.XX or $XXX.XX
        price_match = re.search(r'\$\s*(\d{2,5}\.\d{2})', html)
        regex_price = price_match.group(1) if price_match else "N/A"

        # 2. Clean HTML for AI
        html = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style.*?>.*?</style>', '', html, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()

        # 3. Ask AI for the complex stats
        data = extract_with_ai(ticker, text, fund.get('type', 'bond'))
        
        if data:
            # 4. MERGE: If AI missed the price, use the Regex price
            if data.get('Price') == "N/A" or data.get('Price') is None:
                data['Price'] = regex_price
                
            data['Ticker'] = ticker
            data['Type'] = fund.get('type', 'bond')
            data['Date'] = str(datetime.date.today())
            data['SourceURL'] = fund['url']
            return data
            
        return None

    except Exception as e:
        print(f"[ALERT:DataFetch] ❌ Error scraping {ticker}: {str(e)}")
        return None
    
def lambda_handler(event, context):
    config = get_config()
    if not config: 
        print("[ALERT:Critical] Configuration Missing")
        return {'statusCode': 500, 'body': "Config Missing"}
    
    summary = ""
    # 1. Scrape all funds (This creates 14 files)
    for fund in config:
        stats = scrape_ishares_page(fund)
        time.sleep(4)  # Rate limiting to avoid being blocked
        if stats:
            s3.put_object(
                Bucket=BUCKET_NAME, 
                Key=f"data/{fund['ticker']}_{datetime.date.today()}.json", 
                Body=json.dumps(stats), 
                ContentType='application/json'
            )
            summary += f"✅ {fund['ticker']}: {stats.get('Price')}\n"
        else:
            summary += f"❌ {fund['ticker']}: Failed\n"
    
    # 2. THE FIX: Upload a "Trigger File" at the very end
    # We will configure the Analyst to ONLY listen for this specific file.
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="data/upload_complete.json",
        Body=json.dumps({"status": "done", "timestamp": str(datetime.datetime.now())}),
        ContentType='application/json'
    )
            
    return {'statusCode': 200, 'body': summary}