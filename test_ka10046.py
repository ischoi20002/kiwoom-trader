import requests
import json
import os
import sys

# Move to the root directory to import local modules
sys.path.append(os.getcwd())

import config

# Set environment variables for kiwoom_rest_api
os.environ['KIWOOM_API_KEY'] = config.APP_KEY
os.environ['KIWOOM_API_SECRET'] = config.APP_SECRET

from kiwoom_rest_api.auth.token import TokenManager

def test_ka10046(code="005930"):
    tm = TokenManager()
    token = tm.get_token()
    if not token:
        print("Failed to get token")
        return

    url = f"{config.URL}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10046"
    }
    # Standard parameters for ka10046
    body = {
        "stk_cd": code,
        "tic_scope": "1",    # Time unit (usually 1 min or similar)
        "vol_pow_tp": "1"    # Execution strength type
    }
    
    print(f"Requesting ka10046 for {code}...")
    try:
        res = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"Status Code: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            # The search said the list might be in 'output' or similar
            # Let's print the keys to find the data
            print(f"Response Keys: {data.keys()}")
            
            # Common output keys: 'stck_cntg_hour_trnd_qry' or 'output'
            output = data.get('stck_cntg_hour_trnd_qry', data.get('output', []))
            if output:
                print(f"First Item: {output[0]}")
                print(f"Total Items: {len(output)}")
            else:
                print("No output data found in response.")
                print(f"Full response: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
        else:
            print(f"Error: {res.text}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_ka10046()
