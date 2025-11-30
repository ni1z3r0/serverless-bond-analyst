
import boto3
import urllib.request
import json
import os
import datetime
import re
import gzip
import logging

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
BUCKET_NAME = os.environ.get('BUCKET_NAME')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
FRED_API_KEY = os.environ.get('FRED_API_KEY')
GPT_MODEL = "gpt-4o"
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
TARGET_EMAIL = os.environ.get('TARGET_EMAIL')

# Initialize the SNS Client
sns = boto3.client('sns')
sqs_client = boto3.client('sqs') 
s3 = boto3.client('s3')

# 1. LOAD CONFIGURATION (Dynamic Ticker List)
def get_config():
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key="portfolio_config.json")
        return json.loads(obj['Body'].read())
    except Exception as e:
        logger.exception("Failed to load portfolio_config.json from S3")
        return [] # Fail safe

# 2. MACRO DATA FETCHER
def get_macro_data():
    print("Fetching Macro Data...")
    
    def get_dxy():
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=1d"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'gzip, deflate'})
            with urllib.request.urlopen(req) as response:
                content = response.read()
                if content[:2] == b'\x1f\x8b': content = gzip.decompress(content)
                data = json.loads(content)
                return float(data['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception as e:
            logger.exception("Failed to fetch DXY from Yahoo")
            return None

    def get_fred(id):
        try:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=1"
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode('utf-8'))
                val = data['observations'][0]['value']
                return float(val) if val != "." else None
        except Exception as e:
            logger.exception("Failed to fetch FRED series %s", id)
            return None

    us10y, us02y = get_fred('DGS10'), get_fred('DGS2')
    macros = {
        "DXY": get_dxy(), 
        "US10Y": us10y, 
        "Breakeven_5Y": get_fred('T5YIE'), 
        "Date": str(datetime.date.today())
    }
    
    if us10y and us02y:
        macros['Curve_Spread'] = round(us10y - us02y, 2)
        macros['Is_Bear_Steepener'] = (us10y - us02y) > 0.60
        
    return macros

# 3. S3 DATA READER (Reads Today's Scraped Files)
def get_todays_data():
    today = str(datetime.date.today())
    print(f"Reading S3 data for {today}...")
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="data/")
        data_list = []
        if 'Contents' in response:
            for obj in response['Contents']:
                if today in obj['Key']:
                    file_content = s3.get_object(Bucket=BUCKET_NAME, Key=obj['Key'])
                    data = json.loads(file_content['Body'].read())
                    data_list.append(data)
        return data_list
    except Exception as e:
        logger.exception("Failed to list or read S3 data for %s", today)
        return []

# 4. HISTORY FETCHER (Uses Config File)
def fetch_and_save_history():
    print("Fetching Price History...")
    config = get_config()
    history_data = {}
    
    for fund in config:
        ticker = fund['ticker']
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'gzip, deflate'})
            
            with urllib.request.urlopen(req) as response:
                content = response.read()
                if content[:2] == b'\x1f\x8b': content = gzip.decompress(content)
                data = json.loads(content)
                
                result = data['chart']['result'][0]
                timestamps = result['timestamp']
                closes = result['indicators']['quote'][0]['close']
                
                clean_prices = []
                for t, c in zip(timestamps, closes):
                    if c is not None:
                        d_str = datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d')
                        clean_prices.append({'date': d_str, 'price': round(c, 2)})
                history_data[ticker] = clean_prices
        except Exception as e:
            logger.exception("Failed fetching history for %s", ticker)
            pass
            
    if history_data:
        s3.put_object(Bucket=BUCKET_NAME, Key="dashboard_history.json", Body=json.dumps(history_data), ContentType='application/json')

# 5. ANALYTICS (Bond Only Regression)
def calculate_analytics(todays_data):
    print("🧮 Calculating Analytics...")
    # Filter: Only Bonds allow for Yield/Duration regression
    points = []
    for d in todays_data:
        if d.get('Type') == 'bond' and d.get('Yield') != 'N/A' and d.get('Duration') != 'N/A':
            try:
                points.append({
                    'Ticker': d['Ticker'], 
                    'Yield': float(d['Yield']), 
                    'Duration': float(d['Duration'])
                })
            except Exception as e:
                logger.exception("Failed to parse bond record for analytics: %s", d)
                continue
            
    slope, intercept = 0, 0
    n = len(points)
    if n > 1:
        sum_x = sum(p['Duration'] for p in points)
        sum_y = sum(p['Yield'] for p in points)
        sum_xy = sum(p['Duration'] * p['Yield'] for p in points)
        sum_xx = sum(p['Duration'] ** 2 for p in points)
        try:
            slope = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x**2)
            intercept = (sum_y - slope * sum_x) / n
        except Exception as e:
            logger.exception("Regression calculation failed")
            pass

    dashboard_data = {'scatter_points': points, 'regression': {'slope': slope, 'intercept': intercept}}
    s3.put_object(Bucket=BUCKET_NAME, Key="dashboard_data.json", Body=json.dumps(dashboard_data), ContentType='application/json')

# 6. AI CHAIN
def run_chain(macros, data_list):
    print("⛓️ Running AI Chain...")
    
    # Sort data for the prompt
    bonds = [d for d in data_list if d.get('Type') == 'bond']
    equities = [d for d in data_list if d.get('Type') == 'equity']
    
    # Prompt Construction
    prompt = f"""
    ROLE: CIO managing a Multi-Asset Portfolio.
    
    --- MACRO DASHBOARD ---
    DXY: {macros.get('DXY')}
    10Y Treasury: {macros.get('US10Y')}%
    10-2 Spread: {macros.get('Curve_Spread')}%
    Inflation Breakeven: {macros.get('Breakeven_5Y')}%
    
    --- BOND HOLDINGS (Yield & Duration) ---
    {json.dumps(bonds, indent=1)}
    
    --- EQUITY HOLDINGS (Valuation & Beta) ---
    {json.dumps(equities, indent=1)}
    
    --- MISSION ---
    1. MACRO REGIME: Define the current cycle (e.g. "Late Cycle", "Goldilocks").
    2. BOND STRATEGY: Given the Yield Curve ({macros.get('Curve_Spread')}%), should we prefer HYG (Credit) or IEI (Duration)?
    3. EQUITY STRATEGY: Analyze valuations. Is IWM (Small Cap) or EEM (Emerging) showing stress?
    4. ACTION PLAN: Provide 3 clear bullet points for trades.
    """
    
    # Call OpenAI
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": GPT_MODEL,
        "messages": [{"role": "system", "content": "You are a Tactical CIO."}, {"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())['choices'][0]['message']['content']
    except Exception as e: return f"AI Error: {e}"
# --- SMS / EMAIL NOTIFIER ---
def send_text_alert(report_content):
    if not SQS_QUEUE_URL:
        print("⚠️ No SQS configured. Skipping alert.")
        return

    print("📱 Queuing Notification...")
    
    message_body = {
        "subject": "New Bond Analyst Report Available",
        "recipient": TARGET_EMAIL, 
        "report_summary": report_content
    }
    
    try:
        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body)
        )
        print("SUCCESS: Notification message placed in SQS queue.")
    except Exception as e:
        print(f"ERROR publishing to SQS: {e}")

# --- MAIN HANDLER ---
def lambda_handler(event, context):
    macros = get_macro_data()
    data_list = get_todays_data()
    
    # 1. Update History File (Add new equity prices)
    fetch_and_save_history()
    
    # 2. Update Charts (Bond Regression)
    if data_list: calculate_analytics(data_list)
    
    # 3. Generate AI Report
    if data_list:
        report = run_chain(macros, data_list)
        s3.put_object(Bucket=BUCKET_NAME, Key=f"reports/ai_analysis_{str(datetime.date.today())}.txt", Body=report, ContentType='text/plain')     
    # 4. SEND MESSAGE TO SQS ---
    if data_list: 
      send_text_alert(report)            
    return {'statusCode': 200, 'body': report}
   
    return {'statusCode': 200, 'body': "No data found"}