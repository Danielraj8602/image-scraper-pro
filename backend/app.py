import os
import asyncio
import uuid
import zipfile
import io
import requests
import re
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import unquote, urlparse, parse_qs, urljoin

def normalize_url(url):
    if not url: return url
    if url.startswith('//'): url = 'https:' + url
    
    # 1. Yandex CDN URLs (avatars.mds.yandex.net)
    if 'avatars.mds.yandex.net' in url or 'get-shedevrum' in url:
        # Standard pattern: .../get-XXX/123/abc/suffix
        if '/get-' in url:
            parts = url.split('/')
            if len(parts) >= 6:
                # The last part is the size/optimization (e.g., 'small', '300x300', 'orig')
                last_part_full = parts[-1]
                # Remove any query parameters from the last part
                last_part = last_part_full.split('?')[0]
                
                # Check if it's already original or if it's a known size that can be upgraded
                if last_part not in ['orig', 'original']:
                    # We can safely replace the last part with 'orig' for most Yandex 'get-' services
                    parts[-1] = 'orig'
                    url = '/'.join(parts)
        elif '/get-shedevrum/' in url:
            if not url.endswith('/orig') and not '?' in url.split('/')[-1]:
                url = url.rstrip('/') + '/orig'

    # 2. Pinterest: /736x/ -> /originals/
    if 'pinimg.com' in url:
        if '/736x/' in url:
            url = url.replace('/736x/', '/originals/')
        elif '/236x/' in url:
            url = url.replace('/236x/', '/originals/')

    # 3. Strip common resizing query parameters from any source URL
    try:
        from urllib.parse import urlparse, parse_qs, urlunparse, urlencode
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        
        # Common resize/quality params to remove
        params_to_remove = ['w', 'h', 'width', 'height', 'size', 'quality', 'q', 'resize', 'fit', 'n']
        modified = False
        for p in params_to_remove:
            if p in qs:
                # Only remove if it's not a critical ID param (usually single letter or short)
                # But 'w' and 'h' are almost always sizes.
                del qs[p]
                modified = True
        
        if modified:
            new_query = urlencode(qs, doseq=True)
            url = urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass

    # 4. Google User Content
    if 'googleusercontent.com' in url:
        # Upgrade =s900 or =s400 to =s0 (original) or a large size
        if '=' in url.split('/')[-1]:
            url = re.sub(r'=s\d+.*$', '=s0', url)
        else:
            url = re.sub(r'\/s\d+(-c)?\/', '/s4096/', url)

    return url

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# Directory for temporary downloads
TEMP_DIR = "/tmp/temp_downloads" if "VERCEL" in os.environ else "temp_downloads"
try:
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
except Exception as e:
    print(f"Warning: Could not create temp directory {TEMP_DIR}: {e}")

async def auto_scroll(page, accumulation_set, max_scrolls=15):
    last_height = await page.evaluate("document.body.scrollHeight")
    
    for i in range(max_scrolls):
        # JS evaluation to find both thumbnails and high-res links
        current_data = await page.evaluate("""
            () => {
                const results = [];
                // Target links that usually wrap gallery images
                const links = document.querySelectorAll('a[href*="img_url="], a.ImagesContentImage-Cover, a.serp-item__link');
                links.forEach(a => {
                    let highRes = null;
                    try {
                        const urlParams = new URL(a.href, window.location.origin).searchParams;
                        highRes = urlParams.get('img_url');
                    } catch(e) {}
                    const img = a.querySelector('img');
                    if (img || highRes) {
                        results.push({ 
                            url: highRes || img?.src, 
                            alt: img?.alt || "", 
                            isHighRes: !!highRes 
                        });
                    }
                });

                // Also get all images not inside those links
                const allImgs = document.querySelectorAll('img');
                allImgs.forEach(img => {
                    if (!img.closest('a[href*="img_url="]')) {
                        results.push({ url: img.src, alt: img.alt || "", isHighRes: false });
                    }
                });
                return results;
            }
        """)
        
        for item in current_data:
            url = item['url']
            if url and not url.startswith('data:') and not 'spacer.gif' in url:
                # Add normalized URL
                norm_url = normalize_url(url)
                accumulation_set.add((norm_url, item['alt'], item['isHighRes']))

        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
        await asyncio.sleep(1.2)
        
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height and i > 5: break
        last_height = new_height

async def scrape_images(url, autoscroll=True):
    accumulated_data = set() # Store (url, alt) tuples
    content = ""
    
    # Try using Playwright first
    try:
        async with async_playwright() as p:
            # Use a real-looking user agent
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            try:
                print(f"Scraping URL via Playwright: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # Initial wait
                await asyncio.sleep(2)
                
                if autoscroll:
                    await auto_scroll(page, accumulated_data, max_scrolls=15)
                else:
                    await page.evaluate("window.scrollTo(0, 800)") 
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"Page load/scroll failed: {e}")
            
            # Final extraction from DOM content
            content = await page.content()
            await browser.close()
    except Exception as playwright_err:
        print(f"Playwright scraping failed or is not supported in this environment: {playwright_err}")
        print("Falling back to direct HTTP request using requests...")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://yandex.com/'
            }
            # Fetch directly using requests (extremely fast, compatible anywhere)
            response = requests.get(url, headers=headers, timeout=15)
            content = response.text
        except Exception as req_err:
            print(f"Direct request fallback failed: {req_err}")
            content = ""

    soup = BeautifulSoup(content, 'html.parser')
    images = []
    seen_urls = set()

    # Helper to add image if unique and high quality
    def add_img(url, alt, w=0, h=0):
        if not url or url.startswith('data:'): return
        
        # Skip low-res Yandex search grid thumbnails
        if 'avatars.mds.yandex.net' in url and '/i?id=' in url:
            return
            
        # Skip SVGs, web icons, tracking pixels, or standard small logos
        if url.split('?')[0].endswith('.svg') or any(k in url.lower() for k in ['favicon', '/tracker', 'pixel.gif', 'doubleclick', 'google-analytics', 'yandex.ru/metrika', 'logo', 'spinner', 'icon']):
            return
            
        url = normalize_url(url)
        
        # Try to convert w/h to int
        try:
            w_val = int(w) if w and w != 'Original' else 0
            h_val = int(h) if h and h != 'Original' else 0
        except:
            w_val, h_val = 0, 0

        # Deduplication: if we already have this URL, don't add again
        if url in seen_urls: return
        seen_urls.add(url)
        
        images.append({
            'url': url, 
            'alt': alt, 
            'width': w_val or 'Original', 
            'height': h_val or 'Original',
            'area': w_val * h_val
        })

    # --- Yandex Metadata Extraction (Highest Resolution) ---
    import html
    import json
    
    # Dictionary to store best version of each image ID: {id: {url, w, h, alt}}
    best_images = {}

    # 1. Search for JSON-like objects in the content (often entity encoded)
    # We look for "id":"..." and "origUrl":"..." or "dups":[...]
    # Pattern to find items that look like image metadata objects
    # They usually start with {"id":"..." or &quot;id&quot;:&quot;...
    
    # Try both encoded and unencoded
    for is_encoded in [True, False]:
        q = '&quot;' if is_encoded else '"'
        # Look for ID and then either origUrl or dups within a reasonable range
        # Yandex items are usually within a few thousand characters
        pattern = rf'{q}id{q}\s*:\s*{q}([a-f0-9]{{32}}){q}.*?({q}origUrl{q}|{q}dups{q})'
        matches = re.finditer(pattern, content)
        
        for match in matches:
            img_id = match.group(1)
            start_pos = match.start()
            # Find the boundaries of this object (approximate)
            # Usually it's inside {...}
            # We'll take a chunk and try to find the complete JSON
            chunk = content[start_pos:start_pos+5000]
            
            # Unescape if needed
            if is_encoded:
                chunk = html.unescape(chunk)
            
            # Try to find a valid JSON object starting from {
            # Since we started at "id", let's backtrack to find {
            # Or just construct a minimal JSON if we can find the keys
            
            try:
                # Simple extraction: find origUrl and dimensions directly if JSON parsing is too hard
                orig_match = re.search(r'"origUrl":"(.*?)"', chunk)
                dups_match = re.search(r'"dups":\[(.*?)]', chunk)
                w_match = re.search(r'"width":(\d+)', chunk)
                h_match = re.search(r'"height":(\d+)', chunk)
                
                current_best_url = None
                current_w = int(w_match.group(1)) if w_match else 0
                current_h = int(h_match.group(1)) if h_match else 0
                
                if orig_match:
                    current_best_url = orig_match.group(1).replace('\\/', '/')
                elif dups_match:
                    try:
                        dups_json = json.loads("[" + dups_match.group(1) + "]")
                        if dups_json:
                            best_dup = max(dups_json, key=lambda x: x.get('w', 0) * x.get('h', 0))
                            current_best_url = best_dup.get('url')
                            current_w = max(current_w, best_dup.get('w', 0))
                            current_h = max(current_h, best_dup.get('h', 0))
                    except: pass
                
                if current_best_url:
                    # Normalize early
                    current_best_url = normalize_url(current_best_url)
                    
                    if img_id not in best_images:
                        best_images[img_id] = {'url': current_best_url, 'w': current_w, 'h': current_h}
                    else:
                        # Keep the one with larger area
                        old = best_images[img_id]
                        if (current_w * current_h) > (old['w'] * old['h']):
                            best_images[img_id] = {'url': current_best_url, 'w': current_w, 'h': current_h}
            except: continue

    # Add the best versions found from metadata
    for img_id, data in best_images.items():
        add_img(data['url'], 'Highest Quality Asset', data['w'], data['h'])
    
    print(f"Extracted {len(best_images)} unique high-res images from metadata.")

    # 0. Check the target URL itself for a source image (CBIR)
    try:
        parsed_target = urlparse(url)
        target_qs = parse_qs(parsed_target.query)
        source_search_url = target_qs.get('img_url', target_qs.get('url', [None]))[0]
        if source_search_url:
            add_img(unquote(source_search_url), 'Search Source (Original)')
    except: pass

    # 0.1 Specifically look for CBIR/Source image in DOM
    try:
        source_link = soup.find('a', class_='CbirItem-Link') or soup.find('a', class_='CbirHeader-Image')
        if source_link:
            href = source_link.get('href')
            if href and 'img_url=' in href:
                src = parse_qs(urlparse(href).query).get('img_url', [None])[0]
                if src: add_img(unquote(src), 'Source Image (High Res)')
            else:
                img = source_link.find('img')
                if img: add_img(img.get('src'), 'Source Image')
    except: pass

    # 1. Process accumulated images (captured during scroll)
    # These are usually links with img_url params
    for data in list(accumulated_data):
        url, alt = data[0], data[1]
        # Skip low-res Yandex thumbnails captured during scrolling
        if 'avatars.mds.yandex.net' in url and '/i?id=' in url:
            continue
        add_img(url, alt)

    # 2. Extract from final DOM (BS4) - especially links
    for link in soup.find_all('a', class_=['ImagesContentImage-Cover', 'serp-item__link', 'serp-item__item']):
        try:
            href = link.get('href')
            if href and 'img_url=' in href:
                img_url = parse_qs(urlparse(href).query).get('img_url', [None])[0]
                if img_url:
                    add_img(unquote(img_url), 'High Res Asset')
        except: pass

    # 3. Fallback: all images (Filter out small ones if possible)
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-original')
        if not src: continue
        
        # Skip common UI icons or very small thumbnails
        if 'icon' in src.lower() or 'logo' in src.lower() or 'spinner' in src.lower(): continue
        
        # If it's a Yandex thumbnail, we prefer the metadata version
        if 'avatars.mds.yandex.net' in src and '/i?id=' in src:
            # Skip thumbnail fallbacks since we extract high-res equivalents from metadata
            continue
            
        add_img(src, img.get('alt', ''))
    
    print(f"Total unique images found: {len(images)}")
    return images

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/scrape', methods=['POST'])
async def api_scrape():
    data = request.json
    url = data.get('url')
    autoscroll = data.get('autoscroll', True)
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        images = await scrape_images(url, autoscroll=autoscroll)
        return jsonify({'images': images})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/proxy_download', methods=['GET'])
def api_proxy_download():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        # Fetch the image through the backend to bypass CORS
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Referer': url
        }
        response = requests.get(url, headers=headers, timeout=15, stream=True)
        response.raise_for_status()
        
        # Determine content type and suggested filename
        content_type = response.headers.get('Content-Type', 'image/jpeg')
        
        return send_file(
            io.BytesIO(response.content),
            mimetype=content_type,
            as_attachment=False # Browser will handle download with its own filename or our 'a' tag attribute
        )
    except Exception as e:
        print(f"Proxy download failed for {url}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.json
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400
    
    import concurrent.futures
    
    def download_image(url_index_tuple):
        index, url = url_index_tuple
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                'Referer': url
            }
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code == 200:
                ext = url.split('.')[-1].split('?')[0].lower()
                if not ext or len(ext) > 4 or not ext.isalnum():
                    ext = 'jpg'
                return index, response.content, ext
        except Exception as e:
            print(f"Parallel fetch failed for {url}: {e}")
        return index, None, None

    indexed_urls = list(enumerate(urls))
    downloaded_data = {}
    
    # Run requests concurrently using up to 12 workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        results = executor.map(download_image, indexed_urls)
        for index, content, ext in results:
            if content:
                downloaded_data[index] = (content, ext)

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for index, url in indexed_urls:
            if index in downloaded_data:
                content, ext = downloaded_data[index]
                filename = f"image_{index + 1}_{uuid.uuid4().hex[:6]}.{ext}"
                zf.writestr(filename, content)
                
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name='downloaded_images.zip'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
