import json

def extract_keywords(text):
    stopwords = set(['the', 'and', 'to', 'of', 'a', 'in', 'is', 'that', 'for', 'it', 'on', 'with', 'as', 'was', 'at', 'by', 'an', 'be', 'this', 'which', 'or', 'from', 'but', 'not'])
    words = text.lower().replace('.', '').replace(',', '').split()
    counts = {}
    for w in words:
        if w not in stopwords and len(w) > 3:
            counts[w] = counts.get(w, 0) + 1
    return sorted(counts, key=counts.get, reverse=True)[:20]

try:
    with open('debug_metadata_v3.json', 'r') as f:
        metadata = json.load(f)
    
    # Simulate Keyword Map (Rebuild it since we didn't download it)
    keyword_map = {}
    for item in metadata:
        kws = extract_keywords(item['content'])
        for k in kws:
            if k not in keyword_map: keyword_map[k] = []
            keyword_map[k].append(item['id'])

    query = "what is the contrarian take"
    print(f"QUERY: {query}")
    
    # Simple Keyword Matching
    tokens = query.lower().replace('.', ' ').split()
    scores = {}
    for t in tokens:
        if len(t) > 3 and t in keyword_map:
            for doc_id in keyword_map[t]:
                scores[doc_id] = scores.get(doc_id, 0) + 1
                
    top_ids = sorted(scores, key=scores.get, reverse=True)[:5]
    print(f"Keyword Matches: {top_ids}")
    
    # Fallback Logic
    if not top_ids:
        print("⚠️ No keywords found. Attempting full-text search...")
        query_lower = query.lower()
        for m in metadata:
            if query_lower in m.get('title', '').lower() or query_lower in m.get('content', '').lower():
                top_ids.append(m['id'])
        print(f"Fallback Matches: {top_ids}")

    for m in metadata:
        if m['id'] in top_ids:
            print(f"\nFOUND: {m.get('title')}")
            print(f"CONTENT: {m.get('content')[:100]}...")

except Exception as e:
    print(f"Error: {e}")
