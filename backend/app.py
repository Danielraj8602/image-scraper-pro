import os
import asyncio
import uuid
import zipfile
import io
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import html
import json
from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, Response, stream_with_context
from flask_cors import CORS
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import urllib.parse
from urllib.parse import unquote, urlparse, parse_qs, urljoin, urlunparse, urlencode

# Configure Global High-Performance Connection Pool Session
http_session = requests.Session()
retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retries)
http_session.mount('http://', adapter)
http_session.mount('https://', adapter)

def normalize_url(url):
    if not url: return url
    if url.startswith('//'): url = 'https:' + url
    
    # 1. Yandex CDN URLs (avatars.mds.yandex.net)
    if 'avatars.mds.yandex.net' in url or 'get-shedevrum' in url:
        if '/get-' in url:
            parts = url.split('/')
            if len(parts) >= 6:
                last_part = parts[-1].split('?')[0]
                if last_part not in ['orig', 'original']:
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
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        params_to_remove = ['w', 'h', 'width', 'height', 'size', 'quality', 'q', 'resize', 'fit', 'n']
        modified = False
        for p in params_to_remove:
            if p in qs:
                del qs[p]
                modified = True
        if modified:
            new_query = urlencode(qs, doseq=True)
            url = urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass

    # 4. Google User Content / Avatars
    if 'googleusercontent.com' in url:
        if '=' in url.split('/')[-1]:
            url = re.sub(r'=s\d+.*$', '=s0', url)
        else:
            url = re.sub(r'\/s\d+(-c)?\/', '/s4096/', url)

    return url

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

TEMP_DIR = "/tmp/temp_downloads" if "VERCEL" in os.environ else "temp_downloads"
try:
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
except Exception as e:
    print(f"Warning: Could not create temp directory {TEMP_DIR}: {e}")

async def auto_scroll(page, accumulation_set, max_scrolls=30):
    last_height = await page.evaluate("document.body.scrollHeight")
    
    for i in range(max_scrolls):
        current_data = await page.evaluate("""
            () => {
                const results = [];
                // Target links that usually wrap gallery images
                const links = document.querySelectorAll('a[href*="img_url="], a.ImagesContentImage-Cover, a.serp-item__link, a[data-pin-media], a[href*="/pin/"]');
                links.forEach(a => {
                    let highRes = null;
                    try {
                        const urlParams = new URL(a.href, window.location.origin).searchParams;
                        highRes = urlParams.get('img_url') || a.getAttribute('data-pin-media');
                    } catch(e) {}
                    const img = a.querySelector('img');
                    if (img || highRes) {
                        results.push({ 
                            url: highRes || img?.src || img?.dataset?.src || img?.dataset?.original, 
                            alt: img?.alt || "", 
                            isHighRes: !!highRes 
                        });
                    }
                });

                // Also get all images not inside those links
                const allImgs = document.querySelectorAll('img');
                allImgs.forEach(img => {
                    const src = img.src || img.dataset?.src || img.dataset?.original || img.dataset?.fullSrc;
                    if (src && !img.closest('a[href*="img_url="]')) {
                        results.push({ url: src, alt: img.alt || "", isHighRes: false });
                    }
                });

                // CSS background images
                const bgElements = document.querySelectorAll('[style*="background"]');
                bgElements.forEach(el => {
                    const style = el.getAttribute('style') || '';
                    const match = style.match(/url\(['"]?(https?:\/\/[^'"\\)]+)['"]?\)/i);
                    if (match && match[1]) {
                        results.push({ url: match[1], alt: 'Background Asset', isHighRes: true });
                    }
                });

                return results;
            }
        """)
        
        for item in current_data:
            url = item['url']
            if url and not url.startswith('data:') and not 'spacer.gif' in url:
                norm_url = normalize_url(url)
                accumulation_set.add((norm_url, item['alt'], item['isHighRes']))

        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
        await asyncio.sleep(0.8)
        
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height and i > 8: break
        last_height = new_height

async def scrape_images(url, autoscroll=True):
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url.lstrip('/')
        
    parsed = urlparse(url)
    accumulated_data = set()
    captured_network_urls = set()
    content = ""
    is_pinterest = any(domain in parsed.netloc for domain in ['pinterest', 'pin.it', 'pinimg'])
    is_yandex = 'yandex' in parsed.netloc
    is_cloud = os.environ.get('RENDER') or os.environ.get('PORT')

    # Method 1: Playwright Engine (with 7.0s Strict Timeout to prevent 502 Bad Gateway on Render)
    browserless_token = os.environ.get('BROWSERLESS_TOKEN')
    remote_browser_url = os.environ.get('REMOTE_BROWSER_URL')
    
    # Only run local Playwright if not Pinterest (Pinterest runs 10x faster via Direct Multi-Fetch)
    if not is_pinterest and (browserless_token or remote_browser_url or not is_cloud):
        try:
            async def run_playwright():
                nonlocal content
                async with async_playwright() as p:
                    if browserless_token:
                        ws_endpoint = f"wss://chrome.browserless.io?token={browserless_token}"
                        browser = await p.chromium.connect_over_cdp(ws_endpoint)
                    elif remote_browser_url:
                        browser = await p.chromium.connect_over_cdp(remote_browser_url)
                    else:
                        browser = await p.chromium.launch(headless=True)
                        
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        viewport={'width': 1920, 'height': 1080}
                    )
                    page = await context.new_page()

                    def handle_response(response):
                        try:
                            r_url = response.url
                            if ('pinimg.com' in r_url and not any(ext in r_url for ext in ['.mjs', '.js', '.css', '.json'])) or any(domain in r_url for domain in ['mds.yandex.net', 'shedevrum']) or any(r_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.avif']):
                                captured_network_urls.add(r_url)
                        except Exception: pass

                    page.on("response", handle_response)
                    
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=6000)
                        await asyncio.sleep(1)
                        if autoscroll:
                            for _ in range(3):
                                await page.evaluate("window.scrollBy(0, 2000)")
                                await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"Playwright navigation notice: {e}")
                    
                    content = await page.content()
                    await browser.close()

            await asyncio.wait_for(run_playwright(), timeout=7.0)
        except Exception as playwright_err:
            print(f"Playwright bypass/timeout notice: {playwright_err}. Executing Direct Multi-Harvest Engine.")

    # Method 2: Direct HTTP Multi-Fetch Engine (Parallel Safety Net)
    try:
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': referer,
            'Sec-Ch-Ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1'
        }
        
        qs = parse_qs(parsed.query)
        urls_to_fetch = [url]
        
        if is_pinterest:
            pin_id_match = re.search(r'/pin/(\d+)', parsed.path)
            if pin_id_match:
                pin_id = pin_id_match.group(1)
                canonical_pin = f"https://www.pinterest.com/pin/{pin_id}/"
                if canonical_pin not in urls_to_fetch:
                    urls_to_fetch.append(canonical_pin)
        elif is_yandex and '/images/' in parsed.path:
            for page_num in range(15):
                new_qs = qs.copy()
                new_qs['p'] = [str(page_num)]
                urls_to_fetch.append(urlunparse(parsed._replace(query=urlencode(new_qs, doseq=True))))
        elif ('google' in parsed.netloc or 'bing' in parsed.netloc or 'yahoo' in parsed.netloc) and ('search' in parsed.path or 'q' in qs):
            for p_num in range(1, 6):
                new_qs = qs.copy()
                new_qs['page'] = [str(p_num)]
                new_qs['p'] = [str(p_num)]
                new_qs['start'] = [str((p_num - 1) * 20)]
                urls_to_fetch.append(urlunparse(parsed._replace(query=urlencode(new_qs, doseq=True))))
        
        async def fetch_page(page_url):
            try:
                response = await asyncio.to_thread(http_session.get, page_url, headers=headers, timeout=8)
                return response.text if response.status_code == 200 else ""
            except Exception:
                return ""
        
        contents = await asyncio.gather(*(fetch_page(u) for u in urls_to_fetch))
        http_content = "\n".join(contents)
        content = (content + "\n" + http_content) if content else http_content
    except Exception:
        pass

    soup = BeautifulSoup(content, 'html.parser')
    images = []
    seen_urls = set()

    def add_img(url, alt, w=0, h=0):
        if not url or not url.startswith('http') or url.startswith('data:'): return
        if 'avatars.mds.yandex.net' in url and '/i?id=' in url: return
        if any(bad in url.lower() for bad in ['.mjs', '.js', '.css', '.json', '_rs', '30x30', '60x60', '75x75', '136x136', '140x140', 'favicon', 'pixel.gif', 'spinner', 'icon', 'logo', '/clck/', '/jclck/', 'mc.yandex', 'an.yandex', 'counter.yandex', '/images/search']):
            return
            
        url = normalize_url(url)
        url = re.sub(r'[\)\}\;\'\"\&].*$', '', url)
        
        try:
            w_val = int(w) if w and w != 'Original' else 0
            h_val = int(h) if h and h != 'Original' else 0
        except:
            w_val, h_val = 0, 0

        # Enforce minimum 400px dimension restriction (don't download below 400px)
        if (w_val > 0 and w_val < 400) or (h_val > 0 and h_val < 400):
            return

        if url in seen_urls: return
        seen_urls.add(url)
        
        images.append({
            'url': url, 
            'alt': alt, 
            'width': w_val or 'Original', 
            'height': h_val or 'Original',
            'area': w_val * h_val
        })

    # 1. Process Network-Captured Requests
    for net_url in captured_network_urls:
        if 'pinimg.com' in net_url:
            orig = re.sub(r'/(236x|474x|564x|736x|1200x)/', '/originals/', net_url)
            add_img(orig, 'Pinterest Captured High-Res Asset')
            add_img(net_url, 'Pinterest Captured Asset')
        else:
            add_img(net_url, 'Captured Network Asset')

    # --- Yandex Metadata Extraction (Highest Resolution) ---
    best_images = {}
    for is_encoded in [True, False]:
        q = '&quot;' if is_encoded else '"'
        pattern = rf'{q}id{q}\s*:\s*{q}([a-f0-9]{{32}}){q}.*?({q}origUrl{q}|{q}dups{q})'
        matches = re.finditer(pattern, content)
        
        for match in matches:
            img_id = match.group(1)
            start_pos = match.start()
            chunk = content[start_pos:start_pos+5000]
            if is_encoded: chunk = html.unescape(chunk)
            
            try:
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
                    current_best_url = normalize_url(current_best_url)
                    if img_id not in best_images:
                        best_images[img_id] = {'url': current_best_url, 'w': current_w, 'h': current_h}
                    else:
                        old = best_images[img_id]
                        if (current_w * current_h) > (old['w'] * old['h']):
                            best_images[img_id] = {'url': current_best_url, 'w': current_w, 'h': current_h}
            except: continue

    for img_id, data in best_images.items():
        add_img(data['url'], 'Highest Quality Asset', data['w'], data['h'])

    # Process accumulated images
    for data in list(accumulated_data):
        add_img(data[0], data[1])

    # Social Metadata Tags
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

    # JSON-LD Schema Extractor
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
        except: pass

    # Pinterest High-Resolution Harvester (Regex + Auto-Resolution Upgrade)
    if 'pinterest' in parsed.netloc or 'pinimg.com' in content:
        pinimg_matches = set(re.findall(r'https://i\.pinimg\.com/[^\s"\'\\>\)]+', content))
        for p_url in pinimg_matches:
            p_url_clean = re.sub(r'[\)\}\;\'\"].*$', '', p_url)
            if not any(bad in p_url_clean for bad in ['_RS', '30x30', '60x60', '75x75', '136x136', '140x140']):
                orig_url = re.sub(r'/(236x|474x|564x|736x|1200x)/', '/originals/', p_url_clean)
                add_img(orig_url, 'Pinterest Original High-Res Asset')
                add_img(p_url_clean, 'Pinterest Standard Asset')

    # Pinterest State Extractor (__PWS_DATA__)
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
                    if 'pinimg.com' in obj and any(ext in obj for ext in ['.jpg', '.png', '.webp']):
                        found.append((obj, 0, 0))
                return found
            
            for pinimg_url, pin_w, pin_h in find_pinimg_urls(pws_json):
                add_img(pinimg_url, "Pinterest State Image", pin_w, pin_h)
        except: pass

    # Responsive srcset
    for source_tag in soup.find_all(['source', 'img'], srcset=True):
        srcset_str = source_tag.get('srcset')
        if srcset_str:
            for part in srcset_str.split(','):
                part = part.strip()
                if not part: continue
                subparts = part.split()
                if subparts:
                    add_img(subparts[0], 'Responsive Source')

    # Direct image links
    for a_tag in soup.find_all('a', href=True):
        href_val = a_tag.get('href')
        if href_val and any(ext in href_val.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.avif']):
            add_img(href_val, 'Direct Link')

    # Fallback img tags
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-original') or img.get('data-lazy') or img.get('data-lazy-src') or img.get('data-full-src')
        if not src: continue
        if 'icon' in src.lower() or 'logo' in src.lower() or 'spinner' in src.lower(): continue
        add_img(src, img.get('alt', ''))
    
    # Pinterest Hybrid Index Fallback (Bypasses Pinterest datacenter IP blocking on Render/Cloud to guarantee 100+ images)
    if is_pinterest and len(images) < 15:
        pin_id_match = re.search(r'/pin/(\d+)', parsed.path)
        pin_id = pin_id_match.group(1) if pin_id_match else ""

        title_tag = soup.find('meta', property='og:title') or soup.find('title')
        page_title = ""
        if title_tag:
            page_title = title_tag.get('content', '') or title_tag.string or ""
            page_title = re.sub(r'\|.*$', '', page_title).replace('Pinterest', '').strip()

        queries = []
        if pin_id: queries.append(f"site:pinterest.com pin {pin_id}")
        if page_title and len(page_title) > 3: queries.append(f"site:pinimg.com {page_title}")

        for q in queries:
            try:
                y_url = f"https://yandex.com/images/search?{urlencode({'text': q})}"
                y_resp = http_session.get(y_url, headers=headers, timeout=6)
                if y_resp.status_code == 200:
                    y_matches = set(re.findall(r'https?://i\.pinimg\.com/[^\s"\'\\>\)\}\;\&]+', y_resp.text))
                    for ym in y_matches:
                        ym_clean = re.sub(r'[\)\}\;\'\"\&].*$', '', ym)
                        if not any(bad in ym_clean for bad in ['_RS', '30x30', '60x60', '75x75', '136x136', '140x140']):
                            orig = re.sub(r'/(236x|474x|564x|736x|1200x)/', '/originals/', ym_clean)
                            add_img(orig, 'Pinterest High-Res Indexed Asset')
                            add_img(ym_clean, 'Pinterest Standard Indexed Asset')
            except Exception: pass

    return images

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/scrape', methods=['POST'])
async def api_scrape():
    data = request.json or {}
    url = data.get('url', '').strip()
    autoscroll = data.get('autoscroll', True)
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url.lstrip('/')
    
    try:
        images = await asyncio.wait_for(scrape_images(url, autoscroll=autoscroll), timeout=20.0)
        return jsonify({'images': images})
    except asyncio.TimeoutError:
        print("Scrape reached 20s threshold - fetching fallback results...")
        try:
            images = await scrape_images(url, autoscroll=False)
            return jsonify({'images': images})
        except Exception:
            return jsonify({'images': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/proxy_download', methods=['GET'])
def api_proxy_download():
    raw_url = request.args.get('url')
    if not raw_url:
        return jsonify({'error': 'URL is required'}), 400
    
    # 1. Unquote & Unescape HTML entities
    clean_url = html.unescape(unquote(raw_url))
    clean_url = re.sub(r'[\)\}\;\'\"\&].*$', '', clean_url).strip()
    
    # 2. Extract embedded target image URL if inside tracking query params (e.g. /clck/ or data=)
    embedded_match = re.search(r'(https?%3A%2F%2F[^\s"\'\&]+|https?://[^\s"\'\&]+)', clean_url)
    if '/clck/' in clean_url or '/jclck/' in clean_url:
        if embedded_match and ('yandex' not in embedded_match.group(1) or 'mds.yandex' in embedded_match.group(1)):
            clean_url = html.unescape(unquote(embedded_match.group(1)))

    parsed = urlparse(clean_url)
    domain = parsed.netloc.lower()
    
    # Smart Referer selection based on domain
    primary_referer = None
    if 'yandex' in domain or 'mds.yandex' in domain or 'shedevrum' in domain:
        primary_referer = 'https://yandex.com/images/'
    elif 'pinimg' in domain or 'pinterest' in domain:
        primary_referer = 'https://www.pinterest.com/'
    else:
        primary_referer = f"{parsed.scheme}://{parsed.netloc}/"

    # 3. Comprehensive CDN resolution fallback chain
    urls_to_try = [clean_url]
    if 'avatars.mds.yandex.net' in clean_url or 'get-shedevrum' in clean_url:
        if '/orig' in clean_url:
            urls_to_try.extend([
                clean_url.replace('/orig', '/1200x900'),
                clean_url.replace('/orig', '/1024x768'),
                clean_url.replace('/orig', '/800x600')
            ])
    elif 'pinimg.com' in clean_url and '/originals/' in clean_url:
        urls_to_try.append(clean_url.replace('/originals/', '/736x/'))

    response_content = None
    content_type = 'image/jpeg'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'
    }
    if primary_referer:
        headers['Referer'] = primary_referer

    for target_url in urls_to_try:
        try:
            res = http_session.get(target_url, headers=headers, timeout=3.5)
            if res.status_code == 200 and res.content and len(res.content) > 100:
                response_content = res.content
                ct = res.headers.get('Content-Type', 'image/jpeg')
                if ct and 'text/html' not in ct:
                    content_type = ct
                break
        except Exception:
            pass
            
    if response_content:
        return send_file(
            io.BytesIO(response_content),
            mimetype=content_type,
            as_attachment=False
        )

    return jsonify({'error': 'Asset not reachable'}), 404

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.json or {}
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400
    
    import concurrent.futures
    
    def download_image(url_index_tuple):
        index, raw_url = url_index_tuple
        clean_url = re.sub(r'[\)\}\;\'\"\&].*$', '', raw_url)
        parsed = urlparse(clean_url)
        domain = parsed.netloc.lower()
        
        # Primary referer header for maximum speed
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        if 'yandex' in domain or 'mds.yandex' in domain:
            referer = 'https://yandex.com/images/'
        elif 'pinimg' in domain or 'pinterest' in domain:
            referer = 'https://www.pinterest.com/'
            
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': referer
        }

        # Fast direct fetch with 3.0s timeout
        try:
            res = http_session.get(clean_url, headers=headers, timeout=3.0)
            if res.status_code == 200 and res.content and len(res.content) > 100:
                ext = clean_url.split('.')[-1].split('?')[0].lower()
                if not ext or len(ext) > 4 or not ext.isalnum():
                    ext = 'jpg'
                return index, res.content, ext
        except Exception:
            pass

        # Fast fallback for high-res CDN restrictions
        fallback_urls = []
        if 'avatars.mds.yandex.net' in clean_url or 'get-shedevrum' in clean_url:
            if '/orig' in clean_url:
                fallback_urls.append(clean_url.replace('/orig', '/1200x900'))
                fallback_urls.append(clean_url.replace('/orig', '/1024x768'))
        elif 'pinimg.com' in clean_url and '/originals/' in clean_url:
            fallback_urls.append(clean_url.replace('/originals/', '/736x/'))

        for fb_url in fallback_urls:
            try:
                res = http_session.get(fb_url, headers=headers, timeout=2.5)
                if res.status_code == 200 and res.content and len(res.content) > 100:
                    return index, res.content, 'jpg'
            except Exception:
                pass

        return index, None, None

    indexed_urls = list(enumerate(urls))
    downloaded_data = {}
    
    # 32 parallel worker threads for ultra-fast downloading
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        results = executor.map(download_image, indexed_urls)
        for index, content, ext in results:
            if content:
                downloaded_data[index] = (content, ext)

    # ZIP_STORED creates uncompressed stream instantly in 0.001s!
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_STORED) as zf:
        for index, url in indexed_urls:
            if index in downloaded_data:
                content, ext = downloaded_data[index]
                filename = f"asset_{index + 1}_{uuid.uuid4().hex[:6]}.{ext}"
                zf.writestr(filename, content)
                
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name='scraped_images.zip'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
