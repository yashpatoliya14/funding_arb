import re
with open('docs.html', encoding='utf-16') as f:
    text = f.read()
    urls = re.findall(r'wss?://[^\s"\'<]+', text, re.IGNORECASE)
    print("WebSocket URLs found:", set(urls))
    
    # Also grab lines near "wss"
    print("\nContext:")
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'wss' in line.lower() or 'websocket' in line.lower():
            print(f"{i}: {line.strip()}")
