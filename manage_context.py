import argparse
import json
import os
import sys
import datetime
import uuid
import boto3
import numpy as np
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# --- CONFIGURATION ---
BUCKET_NAME = "bond-dashboard-rc11292025"
LOCAL_CACHE_DIR = ".context_cache"
METADATA_FILE = "metadata.json"
INDEX_FILE = "index.faiss"
KEYWORDS_FILE = "keywords.json"

# Ensure cache exists
if not os.path.exists(LOCAL_CACHE_DIR):
    os.makedirs(LOCAL_CACHE_DIR)

# S3 Client
s3 = boto3.client('s3')

# --- LAZY IMPORTS ---
def get_model():
    print("⏳ Loading local Embedding Model (this may take a moment)...")
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer('all-MiniLM-L6-v2') 

# --- S3 SYNC FUNCTIONS ---
def download_state():
    print("🔄 Syncing from S3...")
    for f_name in [METADATA_FILE, INDEX_FILE, KEYWORDS_FILE]:
        try:
            s3.download_file(BUCKET_NAME, f"context/{f_name}", os.path.join(LOCAL_CACHE_DIR, f_name))
        except:
            pass # File might not exist yet
    print("✅ Sync complete.")

def upload_state():
    print("☁️ Uploading to S3...")
    for f_name in [METADATA_FILE, INDEX_FILE, KEYWORDS_FILE]:
        path = os.path.join(LOCAL_CACHE_DIR, f_name)
        if os.path.exists(path):
            s3.upload_file(path, BUCKET_NAME, f"context/{f_name}")
    print("✅ Upload complete.")

# --- CORE LOGIC ---
def load_metadata():
    path = os.path.join(LOCAL_CACHE_DIR, METADATA_FILE)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return []

def save_metadata(data):
    path = os.path.join(LOCAL_CACHE_DIR, METADATA_FILE)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

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

def rebuild_index(metadata):
    import faiss
    
    if not metadata:
        return

    # 1. Update Keyword Index (Lightweight for Lambda)
    print("🔑 Building Keyword Index...")
    keyword_map = {}
    for item in metadata:
        keywords = extract_keywords(item['content'])
        for k in keywords:
            if k not in keyword_map: keyword_map[k] = []
            keyword_map[k].append(item['id'])
            
    with open(os.path.join(LOCAL_CACHE_DIR, KEYWORDS_FILE), 'w') as f:
        json.dump(keyword_map, f)

    # 2. Update Vector Index (For future advanced use)
    model = get_model()
    texts = [m['content'] for m in metadata]
    print(f"🧠 Generating embeddings for {len(texts)} articles...")
    embeddings = model.encode(texts)
    
    dimension = embeddings.shape[1]
    index = faiss.IndexFlatL2(dimension)
    index.add(np.array(embeddings).astype('float32'))
    faiss.write_index(index, os.path.join(LOCAL_CACHE_DIR, INDEX_FILE))
    
    print(f"✅ Indexes built (Keywords + Vectors).")

# --- INPUT HANDLERS ---

# --- INPUT HANDLERS ---

def extract_url_metadata(url):
    print(f"🌐 Fetching {url}...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"❌ HTTP Error: {resp.status_code}")
            return None
            
        soup = BeautifulSoup(resp.content, 'html.parser')

        # 0. JSON-LD Extraction (Gold Standard)
        json_ld_data = {}
        try:
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    # Handle if it's a list or graph
                    if isinstance(data, dict):
                        if '@graph' in data:
                            for item in data['@graph']:
                                if item.get('@type') in ['Article', 'BlogPosting', 'NewsArticle']:
                                    json_ld_data = item
                                    break
                        else:
                            json_ld_data = data
                            
                    # Break early if we found an article
                    if json_ld_data.get('@type') in ['Article', 'BlogPosting', 'NewsArticle']:
                        break
                except: continue
        except: pass

        # 1. Text Extraction & Cleaning
        paragraphs = soup.find_all('p')
        clean_text = []
        
        # Stop collecting if we hit these footer markers
        STOP_MARKERS = [
            "DISCLOSURE:", 
            "Financial Times Top 300", 
            "Forbes Next-Gen", 
            "Forbes Best in State", 
            "INVESTMENT NEWS", 
            "THE PUGET SOUND BUSINESS JOURNAL", 
            "TAX SERVICES", 
            "GENERAL DISCLOSURE"
        ]
        
        for p in paragraphs:
            p_text = p.get_text().strip()
            if not p_text: continue
            
            # Check for stop markers
            hit_marker = False
            for marker in STOP_MARKERS:
                if marker in p_text:
                    hit_marker = True
                    break
            
            if hit_marker:
                continue # Skip this paragraph (or break if it's the start of the footer)
                
            clean_text.append(p_text)

        text = "\n\n".join(clean_text).strip()
        
        # 2. Title
        title = json_ld_data.get('headline') or \
                soup.find("meta", property="og:title").get("content") if soup.find("meta", property="og:title") else \
                soup.title.string or url

        # 3. Source / Site Name
        source = soup.find("meta", property="og:site_name")
        source_str = source["content"] if source else "Web"
        
        # 4. Author (Priority: JSON-LD > Meta Name > Rel=Author)
        author_str = "Unknown"
        
        # Try JSON-LD first
        if 'author' in json_ld_data:
            auth_node = json_ld_data['author']
            if isinstance(auth_node, list) and auth_node: auth_node = auth_node[0] # Take first
            if isinstance(auth_node, dict): author_str = auth_node.get('name', 'Unknown')
            elif isinstance(auth_node, str): author_str = auth_node

        # Fallback to Meta Tags if unknown or if JSON gave us a URL/garbage
        if author_str in ["Unknown", ""] or "http" in author_str:
            meta_auth = soup.find("meta", attrs={"name": "author"}) or \
                        soup.find("meta", property="twitter:creator")
            if meta_auth: author_str = meta_auth.get("content")
            
            # If still a URL (like facebook), try scraping text tags
            if "http" in author_str or author_str == "Unknown":
                auth_tag = soup.find(class_="author-name") or \
                           soup.find(class_="entry-author") or \
                           soup.find(rel="author")
                if auth_tag: author_str = auth_tag.get_text().strip()

        # 5. Date
        date_str = "Unknown"
        if 'datePublished' in json_ld_data:
            date_str = json_ld_data['datePublished'][:10]
        else:
            date_meta = soup.find("meta", property="article:published_time") or \
                        soup.find("meta", attrs={"name": "date"}) or \
                        soup.find("time")
            
            if date_meta:
                val = date_meta.get("content") or date_meta.get("datetime") or date_meta.get_text()
                if val: date_str = val[:10]
            else:
                import re
                match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
                if match: date_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        
        # 6. Author Fallback (Text Scan) -> OpenAI Fallback
        # If we still don't have an author or date, and we have an API Key, ask AI.
        if (author_str in ["Unknown", ""] or "http" in author_str) or date_str == "Unknown":
            # 6a. Try Regex First
            lines = text.split('\n')[:20]
            for line in lines:
                match = re.search(r'^\s*(?:By|Author:)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', line, re.IGNORECASE)
                if match and author_str == "Unknown":
                    author_str = match.group(1)
            
            # 6b. Try OpenAI (Smart Scrape) if available
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key: 
                if (author_str == "Unknown" or date_str == "Unknown"):
                    print("🤖 Metadata incomplete. Asking OpenAI to find it...")
                    # Construct rich context for AI (Headers + Raw Text, not just <p> tags)
                    page_title = soup.title.string.strip() if soup.title else title
                    h1_text = soup.find('h1').get_text().strip() if soup.find('h1') else ""
                    raw_text_start = soup.get_text(separator='\n', strip=True)[:3000]
                    
                    ai_context = f"""
                    Page Title: {page_title}
                    H1 Header: {h1_text}
                    Raw Content Start:
                    {raw_text_start}
                    """
                    
                    ai_meta = scrape_metadata_with_ai(ai_context, api_key)
                    if ai_meta:
                        # Only overwrite if AI found something AND we are currently Unknown
                        if author_str == "Unknown" and ai_meta.get('author') != "Unknown": 
                            author_str = ai_meta.get('author')
                        if date_str == "Unknown" and ai_meta.get('article_date') != "Unknown": 
                            date_str = ai_meta.get('article_date')
                        if title == url: 
                            title = ai_meta.get('title', title)
            else:
                print("⚠️  Metadata incomplete, but OPENAI_API_KEY not found. Skipping AI fallback.")

        return {
            "content": text,
            "title": title,
            "source": source_str,
            "author": author_str,
            "article_date": date_str,
            "url": url
        }
        
    except Exception as e:
        print(f"❌ Error fetching URL: {e}")
        return None

def scrape_metadata_with_ai(text_chunk, api_key):
    """Uses GPT-4o-mini (cheap/fast) to extract metadata from raw text."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    prompt = f"""
    Extract the following metadata from the article text below.
    Return strictly valid JSON: {{"title": "...", "author": "...", "article_date": "YYYY-MM-DD"}}.
    If unknown, use "Unknown".
    
    Article Text:
    {text_chunk}
    """
    
    data = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }
    
    try:
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            res_json = resp.json()['choices'][0]['message']['content']
            # Clean json markdown if present
            res_json = res_json.replace('```json', '').replace('```', '').strip()
            return json.loads(res_json)
        else:
            print(f"⚠️ OpenAI Error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"⚠️ OpenAI Scrape failed: {e}")
    return None
        


def upload_state():
    print("☁️ Uploading to S3...")
    try:
        for f_name in [METADATA_FILE, INDEX_FILE, KEYWORDS_FILE]:
            path = os.path.join(LOCAL_CACHE_DIR, f_name)
            if os.path.exists(path):
                s3.upload_file(path, BUCKET_NAME, f"context/{f_name}")
        print("✅ Upload complete.")
    except Exception as e:
        print(f"\n❌ S3 UPLOAD FAILED: {e}")
        print("💡 TIP: Your IAM user might need 's3:PutObject' permission for this bucket.")
        print(f"   Resource: arn:aws:s3:::{BUCKET_NAME}/context/*")

def get_manual_input():
    print("📝 Paste article text (Ctrl+Z/D on new line to finish):")
    lines = sys.stdin.readlines()
    return {
        "content": "".join(lines).strip(),
        "title": "Manual Entry",
        "source": "Paste",
        "author": "Unknown",
        "article_date": "Unknown"
    }

def get_file_input(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return {
                "content": f.read().strip(),
                "title": os.path.basename(filepath),
                "source": "File",
                "author": "Unknown",
                "article_date": "Unknown"
            }
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return None

def enrich_metadata(data):
    """Interactively prompts user to fill in missing metadata."""
    print("\n--- ✍️ Metadata Validation ---")
    print("Press Enter to accept defaults/extracted values.")
    
    # Title
    curr = data.get('title', 'Untitled')
    val = input(f"Title [{curr[:50]}]: ").strip()
    if val: data['title'] = val
    
    # Author
    curr = data.get('author', 'Unknown')
    val = input(f"Author [{curr}]: ").strip()
    if val: data['author'] = val
    
    # Date
    curr = data.get('article_date', 'Unknown')
    val = input(f"Published Date (YYYY-MM-DD) [{curr}]: ").strip()
    if val: data['article_date'] = val
    elif curr == 'Unknown': data['article_date'] = str(datetime.date.today()) # Default to today if still unknown
    
    return data

# --- COMMANDS ---

def cmd_add(args):
    download_state()
    metadata = load_metadata()
    
    data = None
    if args.url:
        data = extract_url_metadata(args.url)
    elif args.file:
        data = get_file_input(args.file)
    else:
        data = get_manual_input()
        
    if not data or not data['content']:
        print("❌ No valid content found.")
        return

    # Enforce Metadata Collection
    data = enrich_metadata(data)

    # DEDUPLICATION CHECK
    existing_entry = None
    for m in metadata:
        # 1. Strict URL Check (New schema)
        if data.get('url') and m.get('url') == data['url']:
            existing_entry = m
            break
            
        # 2. Legacy Fallback: Title + Source (Exact match)
        # Prevents duplicates for entries created before 'url' field existed
        if m.get('title') == data['title'] and m.get('source') == data['source']:
             existing_entry = m
             break
        
        # 3. Source is URL Fallback (Very old schema)
        if m.get('source') == data['source'] and str(data['source']).startswith('http'):
            existing_entry = m
            break
            
    if existing_entry:
        print(f"\n⚠️  Duplicate found (ID: {existing_entry['id']}). Updating existing entry...")
        # Update fields
        existing_entry['article_date'] = data['article_date']
        existing_entry['author'] = data['author']
        existing_entry['title'] = data['title']
        existing_entry['content'] = data['content']
        existing_entry['source'] = data['source'] # Update site name/source if changed
        existing_entry['url'] = data.get('url')   # Ensure URL is saved
        existing_entry['content_preview'] = data['content'][:100] + "..."
        # We keep the original ID and system_date_added
        entry = existing_entry
    else:
        # New Entry
        entry = {
            "id": str(uuid.uuid4())[:8],
            "system_date_added": str(datetime.date.today()),
            "article_date": data['article_date'],
            "source": data['source'],
            "author": data['author'],
            "title": data['title'],
            "url": data.get('url'),
            "content_preview": data['content'][:100] + "...",
            "content": data['content']
        }
        metadata.append(entry)
    
    save_metadata(metadata)
    rebuild_index(metadata)
    upload_state()
    
    action = "Updated" if existing_entry else "Saved"
    print(f"\n🎉 {action}! ID: {entry['id']}")

def cmd_list(args):
    download_state()
    metadata = load_metadata()
    print(f"\n📚 Knowledge Base ({len(metadata)} articles)")
    
    # Header
    print(f"{'ID':<9} | {'Date':<10} | {'Author':<15} | {'Title'}")
    print("-" * 80)
    
    for m in metadata:
        pub = m.get('article_date', 'Unknown')
        auth = m.get('author', m.get('source', 'Unknown'))[:15] # Fallback to source if author missing
        tit = m.get('title', 'No Title')[:40]
        print(f"{m['id']:<9} | {pub:<10} | {auth:<15} | {tit}")
    print("-" * 80 + "\n")

def cmd_view(args):
    download_state()
    metadata = load_metadata()
    matches = [m for m in metadata if m['id'] == args.id]
    if not matches:
        print("❌ ID not found.")
        return
    
    m = matches[0]
    print(f"\n📄 VIEWING: {m.get('title', 'Untitled')}")
    print(f"   ID:       {m['id']}")
    print(f"   Author:   {m.get('author', 'Unknown')}")
    print(f"   Pub Date: {m.get('article_date', 'Unknown')} (Added: {m.get('system_date_added', '?')})")
    print(f"   Source:   {m.get('source', 'Unknown')}")
    print("-" * 40)
    print(m['content'])
    print("-" * 40 + "\n")

def cmd_delete(args):
    download_state()
    metadata = load_metadata()
    
    new_meta = [m for m in metadata if m['id'] != args.id]
    
    if len(new_meta) == len(metadata):
        print("❌ ID not found.")
        return
        
    save_metadata(new_meta)
    rebuild_index(new_meta)
    upload_state()
    print("🗑️ Article deleted.")

# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(description="Manage AI Analyst Context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # ADD
    parser_add = subparsers.add_parser('add', help="Add a new article")
    parser_add.add_argument('--url', help="URL to scrape")
    parser_add.add_argument('--file', help="Local file path")
    
    # LIST
    parser_list = subparsers.add_parser('list', help="List all articles")
    
    # VIEW
    parser_view = subparsers.add_parser('view', help="View full article content")
    parser_view.add_argument('id', help="ID of article to view")
    
    # DELETE
    parser_delete = subparsers.add_parser('delete', help="Delete an article by ID")
    parser_delete.add_argument('id', help="ID of article to delete")
    
    args = parser.parse_args()
    
    if args.command == 'add': cmd_add(args)
    elif args.command == 'list': cmd_list(args)
    elif args.command == 'view': cmd_view(args)
    elif args.command == 'delete': cmd_delete(args)

if __name__ == "__main__":
    main()
