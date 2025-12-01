
import boto3
import logging
import botocore
import urllib.request
import urllib.error
import socket
import re
import json
import datetime
import os

BUCKET_NAME = os.environ.get('BUCKET_NAME') 
if not BUCKET_NAME:
    raise ValueError("BUCKET_NAME environment variable is required")
s3 = boto3.client('s3')

BUCKET_NAME = os.environ.get('BUCKET_NAME') 
s3 = boto3.client('s3')

# Logger - Lambda runtime handles basicConfig
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
def get_config():
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key="portfolio_config.json")
        body = obj['Body'].read()
        try:
            return json.loads(body)
        except json.JSONDecodeError as jde:
            logger.exception("Invalid JSON in portfolio_config.json")
            raise ValueError(f"Invalid JSON in portfolio_config.json: {jde}") from jde
    except botocore.exceptions.ClientError as ce:
        # Inspect S3 error code; if the object doesn't exist return empty list, else re-raise after logging
        err_code = ce.response.get('Error', {}).get('Code', '')
        if err_code in ('NoSuchKey', '404', 'NotFound'):
            logger.info("portfolio_config.json not found in S3 (key missing): %s", err_code)
            return []
        logger.exception("S3 ClientError when fetching portfolio_config.json: %s", err_code)
        raise


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

        # Clean Text: Remove HTML tags and excessive whitespace
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        stats = {'Ticker': ticker, 'Type': fund.get('type', 'bond'), 'Date': str(datetime.date.today())}

        # --- IMPROVED REGEX PATTERNS ---
        # We use [^0-9-]{0,50} to allow for up to 50 chars of junk between label and number
        if stats['Type'] == 'bond':
            patterns = {
                'Yield': r'30\s*Day\s*SEC\s*Yield\s*[^0-9-]{0,50}([\d\.]+)',
                'Duration': r'Effective\s*Duration\s*[^0-9-]{0,50}([\d\.]+)',
                'OAS': r'Option\s*Adjusted\s*Spread\s*[^0-9-]{0,50}([\d\.]+)',
                'Maturity': r'Weighted\s*Avg\s*Maturity\s*[^0-9-]{0,50}([\d\.]+)'
            }
        else: 
            patterns = {
                'PE_Ratio': r'P/E\s*Ratio\s*[^0-9-]{0,50}([\d\.]+)',
                'Beta': r'Beta\s*[^0-9-]{0,50}([\d\.]+)', # Loosened "Equity Beta" to just "Beta"
                'Price_Book': r'P/B\s*Ratio\s*[^0-9-]{0,50}([\d\.]+)',
                'Div_Yield': r'12m\s*Trailing\s*Yield\s*[^0-9-]{0,50}([\d\.]+)'
            }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            stats[key] = match.group(1) if match else "N/A"

        # Price Grab (Look for the first dollar sign followed by digits)
        price_match = re.search(r'\$\s*(\d{2,5}\.\d{2})', text)
        stats['Price'] = price_match.group(1) if price_match else "N/A"

        return stats

    except Exception as e:
        print(f"❌ Error scraping {ticker}: {str(e)}")
        return None
def lambda_handler(event, context):
    try:
        config = get_config()
    except Exception as e:
        logger.exception("Config retrieval failed")
        return {'statusCode': 500, 'body': "Internal server error"}

    if not config:
        return {'statusCode': 400, 'body': "Config is empty"}
    # Build a summary of fetched items
    summary = ''
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