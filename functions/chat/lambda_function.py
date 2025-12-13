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
        query = body.get('query')
        
        if not query:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Missing query'})
            }
            
        model = body.get('model', GPT_MODEL)
            
        # 1. Get Context
        ctx = get_context(query)
        
        # 2. Call AI
        response_text = call_ai(query, ctx, model)
        
        return {
            'statusCode': 200,
            'body': json.dumps({'response': response_text})
        }
        
    except Exception as e:
        logger.exception("Lambda Error: %s", e)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
