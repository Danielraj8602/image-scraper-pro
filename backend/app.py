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

    # 2. Pinterest: upgrade any size/thumbnail folder to /originals/
    if 'pinimg.com' in url:
        url = re.sub(r'\/[a-z0-9_]+_RS\/', '/originals/', url)
        url = re.sub(r'\/\d+x\d+\/', '/originals/', url)
        url = re.sub(r'\/\d+x\/', '/originals/', url)

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
            # Check if we should connect to a remote headless browser (perfect for Vercel Serverless!)
            browserless_token = os.environ.get('BROWSERLESS_TOKEN')
            remote_browser_url = os.environ.get('REMOTE_BROWSER_URL')
            
            if browserless_token:
                ws_endpoint = f"wss://chrome.browserless.io?token={browserless_token}"
                print("Connecting to remote Browserless browser on Vercel...")
                browser = await p.chromium.connect_over_cdp(ws_endpoint)
            elif remote_browser_url:
                print(f"Connecting to remote browser at {remote_browser_url}...")
                browser = await p.chromium.connect_over_cdp(remote_browser_url)
            else:
                # Local headless launch (flawless on localhost)
                print("Launching local headless Chromium browser...")
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
        print("Falling back to direct HTTP requests pagination multi-fetch...")
        try:
            from urllib.parse import urlencode, urlunparse
            
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': referer
            }
            
            qs = parse_qs(parsed.query)
            
            urls_to_fetch = []
            if 'yandex' in parsed.netloc and '/images/' in parsed.path:
                # Generate 10 pages for Yandex search (p=0 to p=9) to harvest 300+ images
                for page_num in range(10):
                    new_qs = qs.copy()
                    new_qs['p'] = [str(page_num)]
                    new_query = urlencode(new_qs, doseq=True)
                    new_parsed = parsed._replace(query=new_query)
                    urls_to_fetch.append(urlunparse(new_parsed))
            else:
                urls_to_fetch = [url]
            
            print(f"Direct fetch: targeting {len(urls_to_fetch)} page(s) in parallel...")
            
            async def fetch_page(page_url):
                try:
                    response = await asyncio.to_thread(
                        requests.get, 
                        page_url, 
                        headers=headers, 
                        timeout=15
                    )
                    return response.text if response.status_code == 200 else ""
                except Exception as e:
                    print(f"Error fetching page {page_url}: {e}")
                    return ""
            
            # Fetch all pages concurrently in parallel threads
            contents = await asyncio.gather(*(fetch_page(u) for u in urls_to_fetch))
            content = "\n".join(contents)
        except Exception as req_err:
            print(f"Direct multi-fetch fallback failed: {req_err}")
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

    # 1.5 Social Metadata Tags Extractor
    meta_tags = [
        ('property', 'og:image'),
        ('property', 'og:image:secure_url'),
        ('name', 'twitter:image'),
        ('name', 'twitter:image:src'),
        ('property', 'pinterest:image')
    ]
    for attr, name in meta_tags:
        for tag in soup.find_all('meta', attrs={attr: name}):
            content_val = tag.get('content')
            if content_val and content_val.startswith('http'):
                add_img(content_val, f'Meta {name}')

    # 1.6 Application/LD+JSON schema extractor
    def find_urls_in_json(data):
        found = []
        if isinstance(data, dict):
            for k, v in data.items():
                if k in ['image', 'contentUrl', 'thumbnailUrl']:
                    if isinstance(v, str) and v.startswith('http'):
                        found.append((v, k))
                    elif isinstance(v, dict) and 'url' in v and isinstance(v['url'], str):
                        found.append((v['url'], k))
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str) and item.startswith('http'):
                                found.append((item, k))
                            elif isinstance(item, dict) and 'url' in item and isinstance(item['url'], str):
                                found.append((item['url'], k))
                else:
                    found.extend(find_urls_in_json(v))
        elif isinstance(data, list):
            for item in data:
                found.extend(find_urls_in_json(item))
        return found

    for script in soup.find_all('script', type='application/ld+json'):
        if not script.string: continue
        try:
            ld_data = json.loads(script.string)
            for ld_url, ld_key in find_urls_in_json(ld_data):
                add_img(ld_url, f"LD+JSON {ld_key}")
        except:
            pass

    # 1.7 Pinterest State Extractor (__PWS_DATA__)
    pws_data = soup.find('script', id='__PWS_DATA__')
    if pws_data and pws_data.string:
        try:
            pws_json = json.loads(pws_data.string)
            def find_pinimg_urls(obj):
                found = []
                if isinstance(obj, dict):
                    if 'url' in obj and isinstance(obj['url'], str) and 'pinimg.com' in obj['url']:
                        found.append((obj['url'], obj.get('width', 0), obj.get('height', 0)))
                    for val in obj.values():
                        found.extend(find_pinimg_urls(val))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(find_pinimg_urls(item))
                elif isinstance(obj, str):
                    if 'pinimg.com' in obj and (obj.endswith('.jpg') or obj.endswith('.png') or obj.endswith('.webp') or '.jpg?' in obj):
                        found.append((obj, 0, 0))
                return found
            
            for pinimg_url, pin_w, pin_h in find_pinimg_urls(pws_json):
                add_img(pinimg_url, "Pinterest State Image", pin_w, pin_h)
        except:
            pass

    # 1.8 Responsive source srcset elements
    for source_tag in soup.find_all(['source', 'img'], srcset=True):
        srcset_str = source_tag.get('srcset')
        if srcset_str:
            parts = srcset_str.split(',')
            for part in parts:
                part = part.strip()
                if not part: continue
                subparts = part.split()
                if subparts:
                    srcset_url = subparts[0]
                    # Attempt to resolve width if present (e.g. 1080w)
                    w_val = 0
                    if len(subparts) > 1:
                        desc = subparts[1].lower()
                        if desc.endswith('w'):
                            try: w_val = int(desc[:-1])
                            except: pass
                    add_img(srcset_url, 'Responsive Source', w_val, 0)

    # 1.9 Direct Anchor Image Links (often pointing directly to high-res raw images)
    for a_tag in soup.find_all('a', href=True):
        href_val = a_tag.get('href')
        if href_val and any(ext in href_val.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            add_img(href_val, 'Direct Link')

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
        src = img.get('src') or img.get('data-src') or img.get('data-original') or img.get('data-lazy') or img.get('data-lazy-src')
        if not src: continue
        
        # Skip common UI icons or very small thumbnails
        if 'icon' in src.lower() or 'logo' in src.lower() or 'spinner' in src.lower(): continue
        
        # If it's a Yandex thumbnail, we prefer the metadata version
        if 'avatars.mds.yandex.net' in src and '/i?id=' in src:
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
    
    parsed = urlparse(url)
    origin_referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else "https://www.google.com/"
    
    headers_list = [
        {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Referer': origin_referer,
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
        },
        {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
        }
    ]
    
    response = None
    for headers in headers_list:
        try:
            res = requests.get(url, headers=headers, timeout=12, stream=True)
            if res.status_code == 200 and res.content:
                response = res
                break
        except Exception as e:
            print(f"Proxy attempt failed for {url}: {e}")
            
    if response and response.status_code == 200:
        content_type = response.headers.get('Content-Type', 'image/jpeg')
        if not content_type or 'text/html' in content_type:
            content_type = 'image/jpeg'
            
        return send_file(
            io.BytesIO(response.content),
            mimetype=content_type,
            as_attachment=False
        )

    # Fallback to direct client redirect if proxy fetch returned non-200
    return redirect(url, code=302)

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.json
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400
    
    import concurrent.futures
    
    def download_image(url_index_tuple):
        index, url = url_index_tuple
        parsed = urlparse(url)
        origin_referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else "https://www.google.com/"
        
        headers_list = [
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36', 'Referer': origin_referer},
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'}
        ]
        
        for headers in headers_list:
            try:
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200 and response.content:
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
