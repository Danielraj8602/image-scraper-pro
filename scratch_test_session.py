import requests
import json

pin_id = "1129699887802674621"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"https://www.pinterest.com/pin/{pin_id}/",
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "x-requested-with": "XMLHttpRequest"
}

session = requests.Session()

try:
    print(f"Step 1: Fetching initial page to establish cookies...")
    page_res = session.get(f"https://www.pinterest.com/pin/{pin_id}/", headers=headers, timeout=15)
    print(f"Page response status: {page_res.status_code}")
    print(f"Established cookies: {session.cookies.get_dict()}")
    
    # Extract CSRF token if present
    csrf_token = session.cookies.get("csrftoken")
    if csrf_token:
        headers["x-csrftoken"] = csrf_token
        print("Using x-csrftoken header!")
        
    print(f"\nStep 2: Requesting RelatedModulesResource with established session...")
    url = "https://www.pinterest.com/resource/RelatedModulesResource/get/"
    params = {
        "source_url": f"/pin/{pin_id}/",
        "data": json.dumps({"options": {"pin_id": pin_id}})
    }
    
    api_res = session.get(url, params=params, headers=headers, timeout=15)
    print(f"API response status: {api_res.status_code}")
    
    if api_res.status_code == 200:
        data = api_res.json()
        print("Success! Keys in response:", list(data.keys()))
        
        # Count pinimg URLs
        from backend.app import normalize_url
        seen = set()
        for k, v in data.items():
            # Search strings for pinimg
            matches = re.findall(r'https://[^"\']*?pinimg\.com/[^"\']*?\.(?:jpg|png|webp)', str(data))
            for m in matches:
                seen.add(normalize_url(m))
        print(f"Extracted {len(seen)} related high-resolution images!")
        for idx, img in enumerate(list(seen)[:10]):
            print(f" - Image {idx+1}: {img}")
    else:
        print("API Response Text:", api_res.text[:300])
        
except Exception as e:
    print(f"Error: {e}")
