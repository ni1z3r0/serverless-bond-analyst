
import boto3
import urllib.request
import json
import os
import datetime
import re
import gzip
import logging
import uuid

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
BUCKET_NAME = os.environ.get('BUCKET_NAME')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
FRED_API_KEY = os.environ.get('FRED_API_KEY')
#GPT_MODEL = "gpt-4o"
GPT_MODEL = "gpt-5.1"  # Uncomment to use GPT-5.1
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
TARGET_EMAIL = os.environ.get('TARGET_EMAIL')

# Initialize the SNS Client
sns = boto3.client('sns')
sqs_client = boto3.client('sqs') 
s3 = boto3.client('s3')

from botocore.exceptions import ClientError

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
            # Assumes you have 'logger' defined globally or imported, otherwise use print
            print(f"[ALERT:DataFetch] Failed to fetch DXY from Yahoo: {e}")
            return None

    # Modified to accept a limit for history fetching
    def get_fred_series(id, limit=1):
        try:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit={limit}"
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode('utf-8'))
                observations = data['observations']
                
                # If requesting just 1, return the float value (backward compatibility)
                if limit == 1:
                    if not observations:
                        return None
                    val = observations[0]['value']
                    return float(val) if val != "." else None
                
                # Otherwise return the full list
                return observations
        except Exception as e:
            print(f"[ALERT:DataFetch] Failed to fetch FRED series {id}: {e}")
            return None if limit == 1 else []    # 1. Standard Macros (Latest)
    us10y = get_fred_series('DGS10')
    us02y = get_fred_series('DGS2')
    
    # 2. Labor Market Deep Dive (New)
    # UNRATE: Unemployment Rate (Last 60 months = 5 years)
    unrate_raw = get_fred_series('UNRATE', limit=60)
    
    # JTSLDL: Total Layoffs & Discharges (Last 12 months)
    layoffs_raw = get_fred_series('JTSLDL', limit=12)

    macros = {
        "DXY": get_dxy(), 
        "US10Y": us10y, 
        "Breakeven_5Y": get_fred_series('T5YIE'), 
        "Date": str(datetime.date.today())
    }
    
    # Add calculated spreads
    if us10y and us02y:
        macros['Curve_Spread'] = round(us10y - us02y, 2)
        macros['Is_Bear_Steepener'] = (us10y - us02y) > 0.60

    # Add History Data for AI
    if unrate_raw:
        macros["Current_Unemployment"] = unrate_raw[0]['value']
        # Sample every 12th month to give the AI a clear 5-year trend without token bloat
        macros["Unemployment_Trend"] = [ {'date': x['date'], 'rate': x['value']} for x in unrate_raw[::12] ]
        
    if layoffs_raw:
        macros["Layoffs_Last_12M"] = [ {'date': x['date'], 'value': x['value']} for x in layoffs_raw ]
        
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
                    
                    if isinstance(data, list):
                        data_list.extend(data)
                    else:
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
        
        # FIX: Calculate denominator first to check for zero
        denominator = (n * sum_xx - sum_x**2)
        
        if denominator != 0:
            try:
                slope = (n * sum_xy - sum_x * sum_y) / denominator
                intercept = (sum_y - slope * sum_x) / n
            except Exception as e:
                print(f"Regression math error: {e}")
        else:
            print("⚠️ Variance is zero (all Durations are identical). Cannot calculate slope.")

    dashboard_data = {'scatter_points': points, 'regression': {'slope': slope, 'intercept': intercept}}
    s3.put_object(Bucket=BUCKET_NAME, Key="dashboard_data.json", Body=json.dumps(dashboard_data), ContentType='application/json')

# 5b. CONTEXT RETRIEVAL (Zero-Cost RAG)
def get_article_context(market_keywords_text):
    print("📚 Fetching Analyst Context...")
    try:
        # Download Indexes
        meta_obj = s3.get_object(Bucket=BUCKET_NAME, Key="context/metadata.json")
        keys_obj = s3.get_object(Bucket=BUCKET_NAME, Key="context/keywords.json")
        
        metadata = json.loads(meta_obj['Body'].read())
        keyword_map = json.loads(keys_obj['Body'].read())
        
        # Simple Keyword Matching
        # We tokenize the "market text" (e.g. standard macros)
        tokens = market_keywords_text.lower().replace('.', ' ').split()
        
        scores = {}
        for t in tokens:
            if len(t) > 3 and t in keyword_map:
                for doc_id in keyword_map[t]:
                    scores[doc_id] = scores.get(doc_id, 0) + 1
                    
        # Top 5 Articles
        top_ids = sorted(scores, key=scores.get, reverse=True)[:5]
        
        context_str = ""
        for m in metadata:
            if m['id'] in top_ids:
                context_str += f"\n---\nTitle: {m['source']} ({m['date_added']})\n{m['content']}\n"
                
        if context_str:
            print(f"✅ Found {len(top_ids)} relevant articles.")
            return f"\n\n--- ANALYST BRIEFING MATERIALS (YOUR ARCHIVED CONTEXT) ---\n{context_str}\n"
            
    except Exception as e:
        print(f"⚠️ Context retrieval failed (First run?): {e}")
        
    return ""

# 5c. SELF-FEEDING KNOWLEDGE BASE
def extract_keywords(text):
    """Simple frequency-based keyword extractor to avoid heavy NLTK dependencies on Lambda side."""
    # Stopwords (Basic list)
    stopwords = set(['the', 'and', 'to', 'of', 'a', 'in', 'is', 'that', 'for', 'it', 'on', 'with', 'as', 'was', 'at', 'by', 'an', 'be', 'this', 'which', 'or', 'from', 'but', 'not'])
    
    words = text.lower().replace('.', '').replace(',', '').split()
    # Filter and count
    counts = {}
    for w in words:
        if w not in stopwords and len(w) > 3:
            counts[w] = counts.get(w, 0) + 1
            
    # Return top 20 words
    return sorted(counts, key=counts.get, reverse=True)[:20]

def generate_embedding(text):
    print("🧠 Generating Embedding...")
    url = "https://api.openai.com/v1/embeddings"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": "text-embedding-3-small",
        "input": text[:8000] # Safe limit
    }
    
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())['data'][0]['embedding']
    except Exception as e:
        logger.exception("Embedding generation failed: %s", e)
        return []

def generate_ai_title(report_content):
    print("🧠 Generating Smart Title...")
    prompt = f"""
    Create a short, punchy, 5-8 word headline for this market report.
    Style: Financial News (Bloomberg/WSJ).
    Focus on the "Contrarian" take or main signal.
    Do not use quotes.
    
    REPORT:
    {report_content[:1500]}
    """
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a financial editor. Return only the title text."}, 
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }
    
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            title = json.loads(response.read())['choices'][0]['message']['content']
            return title.replace('"', '').strip()
    except Exception as e:
        logger.exception("Title generation failed: %s", e)
        return f"Market Report {datetime.date.today()}"

def update_knowledge_base(report_json_str):
    print("🧠 Self-Feeding: Updating Knowledge Base with today's report...")
    try:
        report = json.loads(report_json_str)
        content = f"# MARKET MEMO\n{report.get('market_memo', '')}\n\n# SECTOR ANALYSIS\n{report.get('sector_analysis', '')}"
        
        # 0. Generate Smart Title
        smart_title = generate_ai_title(content)
        
        # 1. Download Current State
        # 1. Download Current State
        # A. Metadata & Keywords
        try:
            s3.download_file(BUCKET_NAME, "context/metadata.json", "/tmp/metadata.json")
            s3.download_file(BUCKET_NAME, "context/keywords.json", "/tmp/keywords.json")
            
            with open('/tmp/metadata.json', 'r') as f: metadata = json.load(f)
            with open('/tmp/keywords.json', 'r') as f: keywords = json.load(f)
        except Exception:
            print("⚠️ Metadata/Keywords not found or error. Initializing empty.")
            metadata = []
            keywords = {}

        # B. Embeddings (Separate check to avoid resetting Metadata if this is the only missing file)
        try:
            s3.download_file(BUCKET_NAME, "context/embeddings.json", "/tmp/embeddings.json")
            with open('/tmp/embeddings.json', 'r') as f: embeddings = json.load(f)
        except Exception:
            print("⚠️ Embeddings not found. Initializing empty.")
            embeddings = {}
        
        # 2. Create New Entry
        date_str = str(datetime.date.today())
        new_entry = {
            "id": str(uuid.uuid4())[:8],
            "system_date_added": date_str,
            "article_date": date_str,
            "source": "YieldCurve App (Contrarian Analyst)",
            "author": "Bond Analyst AI",
            "title": smart_title,
            "url": f"s3://{BUCKET_NAME}/reports/ai_analysis_{date_str}.json",
            "content_preview": content[:200] + "...",
            "content": content
        }
        
        # 3. Append & Deduplicate (Remove if same date exists)
        metadata = [m for m in metadata if m.get('article_date') != date_str] # Remove any existing report for today
        metadata.append(new_entry)
        
        # 4. Update Keywords & Vectors
        new_keywords = extract_keywords(content)
        for k in new_keywords:
            if k not in keywords: keywords[k] = []
            keywords[k].append(new_entry['id'])
            
        vector = generate_embedding(content)
        if vector:
            embeddings[new_entry['id']] = vector
            
        # 5. Upload Back to S3
        with open('/tmp/metadata.json', 'w') as f: json.dump(metadata, f)
        with open('/tmp/keywords.json', 'w') as f: json.dump(keywords, f)
        with open('/tmp/embeddings.json', 'w') as f: json.dump(embeddings, f)
        
        s3.upload_file('/tmp/metadata.json', BUCKET_NAME, "context/metadata.json")
        s3.upload_file('/tmp/keywords.json', BUCKET_NAME, "context/keywords.json")
        s3.upload_file('/tmp/embeddings.json', BUCKET_NAME, "context/embeddings.json")
        print(f"✅ Knowledge Base Updated: {smart_title}")
        
    except Exception as e:
        logger.exception("Failed to self-feed knowledge base: %s", e)

# 6. AI CHAIN (JSON Output Version)
def run_chain(macros, data_list):
    print("⛓️ Running AI Chain...")
    
    # Generate Context Key (String representation of macros for keyword matching)
    macro_text = f"Market {macros.get('US10Y')} Unemployment {macros.get('Current_Unemployment')} {json.dumps(macros)}"
    custom_context = get_article_context(macro_text)
    
    bonds = [d for d in data_list if d.get('Type') == 'bond']
    equities = [d for d in data_list if d.get('Type') == 'equity']
    sectors = [d for d in data_list if d.get('Type') == 'sector'] 
    
    prompt = f"""
    ROLE: Elite Contrarian Value Investor (Buffett/Marks Persona).
    
    --- DATA SNAPSHOT ---
    10Y Treasury: {macros.get('US10Y')}% | Yield Curve: {macros.get('Curve_Spread')}%
    Unemployment Trend: {json.dumps(macros.get('Unemployment_Trend'))}
    10Y Treasury: {macros.get('US10Y')}% | Yield Curve: {macros.get('Curve_Spread')}%
    Unemployment Trend: {json.dumps(macros.get('Unemployment_Trend'))}
    Sector Valuations: {json.dumps(sectors, indent=1)}
    {custom_context}
    --- MISSION ---
    Generate two distinct reports in JSON format.
    
    1. MARKET MEMO (The "Big Picture"):
       - Analyze the Macro Regime (Labor market cracks? Yield curve signal?).
       - Provide the "Contrarian" trade for broad assets (Bonds vs Equities).
       
    2. SECTOR DEEP DIVE (The "Alpha"):
       - Specifically analyze the Sector ETFs (IYW, IYE, IYF, IYH).
       - Compare valuations (P/E) vs momentum.
       - Pick one "Overvalued" sector to avoid and one "Undervalued" sector to buy.

    --- OUTPUT FORMAT ---
    You must return VALID JSON with exactly these two keys:
    {{
        "market_memo": "Markdown formatted text...",
        "sector_analysis": "Markdown formatted text..."
    }}
    """
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    # Force JSON mode to ensure the frontend doesn't break
    payload = {
        "model": GPT_MODEL,
        "response_format": { "type": "json_object" }, 
        "messages": [
            {"role": "system", "content": "You are a contrarian value investor. Output valid JSON."}, 
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }
    
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())['choices'][0]['message']['content']
    except Exception as e: 
        print(f"[ALERT:OpenAI] AI Chain Failed: {e}")
        return json.dumps({"market_memo": f"Error: {e}", "sector_analysis": "Unavailable"})

# --- SMS / EMAIL NOTIFIER ---
def send_text_alert(report_content):
    if not SQS_QUEUE_URL:
        logger.info("⚠️ No SQS configured. Skipping alert.")
        return

    logger.info("📱 Queuing Notification...")
    
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
        logger.info("SUCCESS: Notification message placed in SQS queue.")
    except Exception as e:
        logger.exception("ERROR publishing to SQS: %s", e)

# --- MAIN HANDLER ---
def lambda_handler(event, context):
    macros = get_macro_data()
    data_list = get_todays_data()
    
    # 1. Update History & Analytics
    fetch_and_save_history()
    if data_list: calculate_analytics(data_list)
    
    if data_list:
        # 2. Generate AI Report
        report_json_str = run_chain(macros, data_list) 
        
        # 3. Save full JSON to S3
        s3.put_object(
            Bucket=BUCKET_NAME, 
            Key=f"reports/ai_analysis_{str(datetime.date.today())}.json", 
            Body=report_json_str, 
            ContentType='application/json'
        )

        # 4. Self-Feed Knowledge Base
        update_knowledge_base(report_json_str)
        
        # --- SQS PAYLOAD GENERATION ---
        try:
            report_data = json.loads(report_json_str)
            email_text = report_data.get("market_memo", "Report generated successfully.")
        except:
            email_text = "Report generated (Raw Data)."

        # Dynamic URL
        dashboard_url = f"https://d269ewi535s8ar.cloudfront.net/index.html"
        final_message = f"{email_text}\n\n-----------------\n📊 View Live Dashboard:\n{dashboard_url}"

        # FIX: Use the existing global TARGET_EMAIL variable
        if not TARGET_EMAIL:
            print("[ALERT:Critical] Error: TARGET_EMAIL environment variable is missing.")
            return {'statusCode': 500, 'body': "Configuration Error: Missing Target Email"}

        message_body = {
            "subject": "Daily Market Memo (AI)",
            "recipient": TARGET_EMAIL, 
            "report_summary": final_message
        }
        
        try:
            sqs_client.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps(message_body)
            )
            print(f"SUCCESS: Notification sent to SQS for {TARGET_EMAIL}.")
        except Exception as e:
            print(f"ERROR publishing to SQS: {e}")
        
        return {'statusCode': 200, 'body': "Report Generated & Sent"}
    
    return {'statusCode': 200, 'body': "No data found"}