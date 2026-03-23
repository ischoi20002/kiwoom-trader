import requests
import json
import config
import os

def get_token_robust():
    url = f"{config.URL}/oauth2/token"
    headers = {"Content-Type": "application/json"}
    
    # Try different key names
    body_variants = [
        {"grant_type": "client_credentials", "appkey": config.APP_KEY, "appsecret": config.APP_SECRET},
        {"grant_type": "client_credentials", "app_key": config.APP_KEY, "app_secret": config.APP_SECRET},
    ]
    
    for body in body_variants:
        try:
            print(f"DEBUG: Trying token with body keys: {list(body.keys())}")
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                data = res.json()
                token = data.get('access_token')
                if token:
                    print("DEBUG: Token Success!")
                    return token
                else:
                    print(f"DEBUG: 200 OK but no access_token. Body: {res.text}")
            else:
                print(f"DEBUG: Failed ({res.status_code}): {res.text}")
        except Exception as e:
            print(f"DEBUG: Exception: {e}")
    return None

def probe_indices():
    token = get_token_robust()
    if not token:
        print("FAIL: Could not get token.")
        return

    # Codes to test
    codes = ["001", "101", "1001", "2001", "0001", "1002"]
    url = f"{config.URL}/api/dostk/chart"
    
    for code in codes:
        print(f"\n--- PROBING CODE: {code} ---")
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "ki10082"
        }
        body = {
            "stk_cd": code,
            "tic_scope": "1",
            "upd_stkpc_tp": "1"
        }
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                data = res.json()
                # Check for various possible keys
                found_key = None
                for k in ["idx_min_pole_chart_qry", "stk_min_pole_chart_qry", "output", "data"]:
                    if k in data and isinstance(data[k], list) and len(data[k]) > 0:
                        found_key = k
                        break
                
                if found_key:
                    print(f"SUCCESS! Code {code} returned data in '{found_key}'. Count: {len(data[found_key])}")
                    print(f"Sample: {data[found_key][0]}")
                else:
                    print(f"EMPTY: Code {code} returned 200 but no data list found. Keys: {list(data.keys())}")
            else:
                print(f"ERROR ({res.status_code}): {res.text}")
        except Exception as e:
            print(f"EXCEPTION for {code}: {e}")

if __name__ == "__main__":
    probe_indices()
