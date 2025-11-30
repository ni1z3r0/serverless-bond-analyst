
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
    # Validate input early to avoid KeyError for malformed entries
    if not isinstance(fund, dict):
        logger.error("Invalid fund entry: expected dict, got %s", type(fund))
        return None

    ticker = fund.get('ticker')
    url = fund.get('url')

    if not ticker or not isinstance(ticker, str):
        logger.error("Fund entry missing or invalid 'ticker': %r", fund)
        return None

    if not url or not isinstance(url, str):
        logger.error("Fund entry missing or invalid 'url' for ticker %s: %r", ticker, fund)
        return None

    logger.info("Fetching %s...", ticker)
    # First, fetch the page and handle network-related errors explicitly so we can continue
    try:
        req = urllib.request.Request(
            fund['url'], 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as he:
        logger.exception("HTTP error fetching %s: %s", ticker, he)
        return None
    except urllib.error.URLError as ue:
        logger.exception("URL error fetching %s: %s", ticker, ue)
        return None
    except socket.timeout as te:
        logger.exception("Timeout fetching %s: %s", ticker, te)
        return None

    # If fetch succeeded, continue parsing; if parsing raises unexpected exceptions, log and re-raise
    try:
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
                'PE_Ratio': r'P/E\s*Ratio\s*[^0-9-]{0,20}([\d\.]+)',
                'Beta': r'Equity\s*Beta\s*\(3y\)\s*[^0-9-]{0,20}([\d\.]+)',
                'Price_Book': r'P/B\s*Ratio\s*[^0-9-]{0,20}([\d\.]+)',
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
        # Unexpected parsing error — log and re-raise so it's visible
        logger.exception("Unexpected error parsing content for %s", ticker)
        raise

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