import boto3
import json
import os
import urllib.request
import logging

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
BUCKET_NAME = os.environ.get('BUCKET_NAME')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
GPT_MODEL = "gpt-4o"

s3 = boto3.client('s3')

# --- HELPERS ---

def get_cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'OPTIONS,POST,GET'
    }

# --- SUBSCRIBER ACCESS CONTROL (MVP) ---
def get_subscribers():
    """Load authorized subscriber emails from S3."""
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key="infrastructure/subscribers.json")
        data = json.loads(obj['Body'].read())
        return [email.lower().strip() for email in data.get('emails', [])]
    except Exception as e:
        logger.warning("Could not load subscribers list: %s", e)
        return []

def is_authorized(email):
    """Check if email is in the subscribers list."""
    if not email:
        return False
    subscribers = get_subscribers()
    # Empty list = allow all (for testing), otherwise check
    if not subscribers:
        logger.info("No subscribers list found - allowing all access (dev mode)")
        return True
    return email.lower().strip() in subscribers


import math

def generate_embedding(text):
    print("🧠 Generating Query Embedding...")
    url = "https://api.openai.com/v1/embeddings"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": "text-embedding-3-small",
        "input": text[:8000]
    }
    
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())['data'][0]['embedding']
    except Exception as e:
        logger.exception("Embedding generation failed: %s", e)
        return []

def cosine_similarity(v1, v2):
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    return dot_product / (norm_a * norm_b) if norm_a > 0 and norm_b > 0 else 0

def get_context(query):
    print("📚 Fetching Context (Hybrid)...")
    try:
        # Download Indexes
        s3.download_file(BUCKET_NAME, "context/metadata.json", "/tmp/metadata.json")
        s3.download_file(BUCKET_NAME, "context/keywords.json", "/tmp/keywords.json")
        try:
            s3.download_file(BUCKET_NAME, "context/embeddings.json", "/tmp/embeddings.json")
        except:
            # Fallback if embeddings file doesn't exist yet
            with open('/tmp/embeddings.json', 'w') as f: json.dump({}, f)
            
        with open('/tmp/metadata.json', 'r') as f: metadata = json.load(f)
        with open('/tmp/keywords.json', 'r') as f: keyword_map = json.load(f)
        with open('/tmp/embeddings.json', 'r') as f: embeddings = json.load(f)
            
        scores = {}
        
        # 1. Keyword Matching
        tokens = query.lower().replace('.', ' ').split()
        for t in tokens:
            if len(t) > 3 and t in keyword_map:
                for doc_id in keyword_map[t]:
                    scores[doc_id] = scores.get(doc_id, 0) + 1.0 # Base score for keyword

        # 2. Vector Matching (Semantic)
        query_vec = generate_embedding(query)
        if query_vec and embeddings:
            print(f"🧠 Computing similarity against {len(embeddings)} docs...")
            for doc_id, doc_vec in embeddings.items():
                sim = cosine_similarity(query_vec, doc_vec)
                # Boost score by similarity (scaled to be comparable to keywords matching)
                # E.g., strong match (0.8) adds 3 points, weak match (0.4) adds 0 points
                if sim > 0.4:
                    scores[doc_id] = scores.get(doc_id, 0) + (sim * 5.0)

        # Top 5 Articles
        top_ids = sorted(scores, key=scores.get, reverse=True)[:5]
        
        if not top_ids:
            # Fallback: Simple String Search
            print("⚠️ No matches found. Attempting full-text search...")
            query_lower = query.lower()
            for m in metadata:
                if query_lower in m.get('title', '').lower() or query_lower in m.get('content', '').lower():
                    top_ids.append(m['id'])
            top_ids = top_ids[:3]

        context_str = ""
        for m in metadata:
            if m['id'] in top_ids:
                # Add score debug info if available
                score_val = scores.get(m['id'], 0)
                content_preview = m['content'][:2000] 
                context_str += f"\n---\nTitle: {m.get('title', 'Unknown')} (Score: {score_val:.2f})\n{content_preview}\n"
                
        if context_str:
            print(f"✅ Found {len(top_ids)} relevant articles.")
            return context_str
        else:
            return "No specific context found."
            
    except Exception as e:
        logger.exception("Context retrieval failed: %s", e)
        return ""

def call_ai(query, context, model_id=GPT_MODEL):
    print(f"🧠 Calling OpenAI ({model_id})...")

    # Handle "Preview" models or mapping if necessary
    target_model = model_id
    if "gpt-5" in model_id:
        # Mocking GPT-5 for now, or fallback to 4o
        target_model = "gpt-4o"

    prompt = f"""
    You are a helpful Bond Market Analyst Assistant.
    Answer the user's question using the provided context (Market Memos, Articles, Data).
    If the answer isn't in the context, use your general knowledge but mention that it's general info.

    --- CONTEXT ---
    {context}

    --- USER QUESTION ---
    {query}
    """

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": f"You are a helpful financial assistant running on {model_id} architecture. Keep answers concise (under 200 words)."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.5
    }

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())['choices'][0]['message']['content']
    except Exception as e:
        logger.exception("OpenAI Call Failed: %s", e)
        return "I'm sorry, I'm having trouble connecting to my brain right now."

# --- REFRESH ANALYSIS FUNCTIONS ---

def get_latest_data():
    """Read the most recent scraped data from S3 (today or fallback to latest)."""
    import datetime
    import re
    today = str(datetime.date.today())
    print(f"📊 Reading S3 data (preferring {today})...")
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="data/")
        if 'Contents' not in response:
            return [], None

        # Group files by date
        date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')
        files_by_date = {}
        for obj in response['Contents']:
            match = date_pattern.search(obj['Key'])
            if match:
                date_str = match.group(1)
                if date_str not in files_by_date:
                    files_by_date[date_str] = []
                files_by_date[date_str].append(obj['Key'])

        # Try today first, then fall back to most recent date
        if today in files_by_date:
            target_date = today
        elif files_by_date:
            target_date = max(files_by_date.keys())
            print(f"⚠️ No data for {today}, using {target_date}")
        else:
            return [], None

        # Load all files for the target date
        data_list = []
        for key in files_by_date[target_date]:
            file_content = s3.get_object(Bucket=BUCKET_NAME, Key=key)
            data = json.loads(file_content['Body'].read())
            if isinstance(data, list):
                data_list.extend(data)
            else:
                data_list.append(data)

        return data_list, target_date
    except Exception as e:
        logger.exception("Failed to read S3 data: %s", e)
        return [], None

def get_macro_data():
    """Fetch macro economic data for analysis."""
    import datetime
    import gzip
    print("📈 Fetching Macro Data...")

    FRED_API_KEY = os.environ.get('FRED_API_KEY')

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
            print(f"Failed to fetch DXY: {e}")
            return None

    def get_fred_series(series_id, limit=1):
        try:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit={limit}"
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode('utf-8'))
                observations = data['observations']
                if limit == 1:
                    if not observations:
                        return None
                    val = observations[0]['value']
                    return float(val) if val != "." else None
                return observations
        except Exception as e:
            print(f"Failed to fetch FRED {series_id}: {e}")
            return None if limit == 1 else []

    us10y = get_fred_series('DGS10')
    us02y = get_fred_series('DGS2')
    unrate_raw = get_fred_series('UNRATE', limit=60)

    macros = {
        "DXY": get_dxy(),
        "US10Y": us10y,
        "Breakeven_5Y": get_fred_series('T5YIE'),
        "Date": str(datetime.date.today())
    }

    if us10y and us02y:
        macros['Curve_Spread'] = round(us10y - us02y, 2)

    if unrate_raw:
        macros["Current_Unemployment"] = unrate_raw[0]['value']
        macros["Unemployment_Trend"] = [{'date': x['date'], 'rate': x['value']} for x in unrate_raw[::12]]

    return macros

def run_analysis_chain(macros, data_list, model_id, data_date=None):
    """Run AI chain to generate market memo and sector analysis.
    Returns dict with 'response' and 'debug' keys.
    """
    print(f"⛓️ Running Analysis Chain with {model_id} using data from {data_date}...")

    target_model = model_id
    if "gpt-5" in model_id:
        target_model = "gpt-4o"

    # Sector ETFs by ticker (IYW=Tech, IYE=Energy, IYF=Financials, IYH=Healthcare)
    sector_tickers = {'IYW', 'IYE', 'IYF', 'IYH'}
    sectors = [d for d in data_list if d.get('Ticker') in sector_tickers]

    prompt = f"""
    ROLE: Elite Contrarian Value Investor (Buffett/Marks Persona).

    --- DATA SNAPSHOT ---
    10Y Treasury: {macros.get('US10Y')}% | Yield Curve: {macros.get('Curve_Spread')}%
    Unemployment Trend: {json.dumps(macros.get('Unemployment_Trend'))}
    Sector Valuations: {json.dumps(sectors, indent=1)}

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

    system_msg = f"You are a contrarian value investor running on {model_id}. Output valid JSON."

    # Build debug info
    debug_info = {
        "model_requested": model_id,
        "model_used": target_model,
        "data_date": data_date,
        "macro_data": macros,
        "sector_data": sectors,
        "system_prompt": system_msg,
        "user_prompt": prompt
    }

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": target_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        with urllib.request.urlopen(req) as response:
            ai_response = json.loads(response.read())['choices'][0]['message']['content']
            return {"response": ai_response, "debug": debug_info}
    except Exception as e:
        logger.exception("Analysis Chain Failed: %s", e)
        return {
            "response": json.dumps({"market_memo": f"Error: {e}", "sector_analysis": "Unavailable"}),
            "debug": debug_info
        }

# --- MAIN HANDLER ---

# --- MAIN HANDLER ---

def lambda_handler(event, context):
    # Support both Payload 1.0 (API Gateway) and 2.0 (Function URL default)
    method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method')
    
    # Handle implicit OPTIONS (AWS passes them through)
    if method == 'OPTIONS':
        return {
            'statusCode': 200,
            'body': ''
        }
        
    try:
        body = json.loads(event.get('body', '{}'))
        action = body.get('action')
        email = body.get('email')  # MVP: Frontend must send user email

        # --- MVP ACCESS CONTROL ---
        if not is_authorized(email):
            logger.warning("Access denied for email: %s", email)
            return {
                'statusCode': 403,
                'headers': get_cors_headers(),
                'body': json.dumps({'error': 'Access denied. Please subscribe to use the analyst.'})
            }

        model = body.get('model', GPT_MODEL)

        # --- REFRESH ANALYSIS ACTION ---
        if action == 'refresh_analysis':
            print(f"🔄 Refresh Analysis requested with model: {model}")
            macros = get_macro_data()
            data_list, data_date = get_latest_data()

            if not data_list:
                return {
                    'statusCode': 200,
                    'headers': get_cors_headers(),
                    'body': json.dumps({
                        'error': 'No market data available for today. Please try again later.'
                    })
                }

            result = run_analysis_chain(macros, data_list, model, data_date)
            report = json.loads(result['response'])
            debug_info = result['debug']

            return {
                'statusCode': 200,
                'headers': get_cors_headers(),
                'body': json.dumps({
                    'market_memo': report.get('market_memo', ''),
                    'sector_analysis': report.get('sector_analysis', ''),
                    'debug': debug_info
                })
            }

        # --- STANDARD CHAT QUERY ---
        query = body.get('query')
        if not query:
            return {
                'statusCode': 400,
                'headers': get_cors_headers(),
                'body': json.dumps({'error': 'Missing query'})
            }

        # 1. Get Context
        ctx = get_context(query)

        # 2. Call AI
        response_text = call_ai(query, ctx, model)

        return {
            'statusCode': 200,
            'headers': get_cors_headers(),
            'body': json.dumps({'response': response_text})
        }
        
    except Exception as e:
        logger.exception("Lambda Error: %s", e)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
