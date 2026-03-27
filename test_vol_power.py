import requests
import json
import config
import os
from kiwoom_rest_api.auth.token import TokenManager

def test_vol_power(code="344820"):
    use_mock = getattr(config, 'USE_MOCK', False)
    app_key = config.MOCK_APP_KEY if use_mock else config.APP_KEY
    app_secret = config.MOCK_APP_SECRET if use_mock else config.APP_SECRET
    
    os.environ['KIWOOM_API_KEY'] = app_key
    os.environ['KIWOOM_API_SECRET'] = app_secret
    
    from kiwoom_rest_api import config as kiwoom_config
    kiwoom_config.API_KEY = app_key
    kiwoom_config.API_SECRET = app_secret
    kiwoom_config.BASE_URL = config.MOCK_URL if use_mock else config.REAL_URL
    
    token_manager = TokenManager()
    token = token_manager.get_token()
    if not token:
        print("Error: Could not get token.")
        return

    url = f"{config.URL}/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10046"
    }
    body = {
        "stk_cd": code,
        "tic_scope": "1",
        "vol_pow_tp": "1"
    }

    print(f"Requesting Vol Power for {code}...")
    res = requests.post(url, headers=headers, json=body, timeout=10)
    print(f"Status Code: {res.status_code}")
    if res.status_code == 200:
        data = res.json()
        raw_list = data.get('stk_cntg_hour_trnd_qry', data.get('output', []))
        print(f"Received {len(raw_list)} records.")
        if raw_list:
            print("First record sample:")
            print(json.dumps(raw_list[0], indent=2, ensure_ascii=False))
    else:
        print(f"Error Message: {res.text}")

if __name__ == "__main__":
    test_vol_power()
