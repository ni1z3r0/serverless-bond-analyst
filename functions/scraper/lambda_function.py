import boto3
import urllib.request
import re
import datetime
import json
import os

BUCKET_NAME = os.environ.get('BUCKET_NAME') 
s3 = boto3.client('s3')

def get_config():
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key="portfolio_config.json")
        return json.loads(obj['Body'].read())
    except: return []

def scrape_ishares_page(fund):
    ticker = fund['ticker']
    print(f"Fetching {ticker}...")
    
    try:
        req = urllib.request.Request(
            fund['url'], 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0'}
        )
        with urllib.request.urlopen(req) as response:
            content = response.read().decode('utf-8', errors='ignore')

        # Clean Text
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'as of [A-Za-z]+ \d{1,2}, \d{4}', ' ', text)
        text = re.sub(r'\s+', ' ', text) 
        
        stats = {'Ticker': ticker, 'Type': fund.get('type', 'bond'), 'Date': str(datetime.date.today())}

        # --- UPDATED REGEX PATTERNS (MATCHING YOUR SCREENSHOT) ---
        if stats['Type'] == 'bond':
            patterns = {
                'Yield': r'30\s*Day\s*SEC\s*Yield\s*[^0-9-]{0,20}([\d\.]+)',
                'Duration': r'Effective\s*Duration\s*[^0-9-]{0,20}([\d\.]+)',
                'OAS': r'Option\s*Adjusted\s*Spread\s*[^0-9-]{0,20}([\d\.]+)',
                'Maturity': r'Weighted\s*Avg\s*Maturity\s*[^0-9-]{0,20}([\d\.]+)'
            }
        else: # Equity
            patterns = {
                # Matches "P/E Ratio 18.44"
                'PE_Ratio': r'P/E\s*Ratio\s*[^0-9-]{0,20}([\d\.]+)',
                
                # Matches "Equity Beta (3y) 1.28" (We skip the '3')
                'Beta': r'Equity\s*Beta\s*\(3y\)\s*[^0-9-]{0,20}([\d\.]+)',
                
                # Matches "P/B Ratio 2.05"
                'Price_Book': r'P/B\s*Ratio\s*[^0-9-]{0,20}([\d\.]+)',
                
                # Matches "12m Trailing Yield 0.98%"
                'Div_Yield': r'12m\s*Trailing\s*Yield\s*[^0-9-]{0,20}([\d\.]+)'
            }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            stats[key] = match.group(1) if match else "N/A"

        # Price is common
        price_match = re.search(r'\$\s*(\d+\.\d+)', text)
        stats['Price'] = price_match.group(1) if price_match else "N/A"

        return stats

    except Exception as e:
        print(f"❌ Error scraping {ticker}: {str(e)}")
        return None

def lambda_handler(event, context):
    config = get_config()
    if not config: return {'statusCode': 500, 'body': "Config Missing"}
    
    summary = ""
    for fund in config:
        stats = scrape_ishares_page(fund)
        if stats:
            s3.put_object(
                Bucket=BUCKET_NAME, 
                Key=f"data/{fund['ticker']}_{datetime.date.today()}.json", 
                Body=json.dumps(stats), 
                ContentType='application/json'
            )
            summary += f"✅ {fund['ticker']}: {stats.get('Price')}\n"
            
    return {'statusCode': 200, 'body': summary}