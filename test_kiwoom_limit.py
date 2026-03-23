import requests
import json
import time
from datetime import datetime
import config

def get_token():
    auth_url = f"{config.URL}/oauth2/token"
    auth_body = {
        "grant_type": "client_credentials",
        "appkey": config.APP_KEY,
        "secretkey": config.APP_SECRET
    }
    res = requests.post(auth_url, json=auth_body)
    return res.json().get("token")

def test_limit():
    token = get_token()
    if not token:
        print("Failed to get token")
        return

    code = "005930" # Samsung Electronics
    tic_scope = "1" # 1-minute bars
    url = f"{config.URL}/api/dostk/chart"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10080"
    }

    next_key = ""
    total_bars = 0
    earliest_date = None
    
    # Use a far in the past base_dt if possible, or just current. 
    # Usually, we start from current and go backwards.
    base_dt = datetime.now().strftime("%Y%m%d")

    print(f"Testing Kiwoom API minute data limit (tic_scope={tic_scope}) for {code}...")

    try:
        while True:
            headers["next-key"] = next_key
            headers["cont-yn"] = "Y" if next_key else "N"
            
            body = {
                "stk_cd": code,
                "tic_scope": tic_scope,
                "upd_stkpc_tp": "1",
                "base_dt": base_dt
            }
            
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code != 200:
                print(f"\nAPI Error: {res.status_code}, {res.text}")
                break
            
            data = res.json()
            raw_list = data.get('stk_min_pole_chart_qry', data.get('output', []))
            
            if not raw_list:
                print("\nNo more data returned.")
                break
            
            total_bars += len(raw_list)
            
            # Find earliest date in this batch
            batch_earliest = None
            for b in raw_list:
                dt_str = str(b.get('cntr_tm', "")).strip()
                if dt_str:
                    dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
                    if batch_earliest is None or dt < batch_earliest:
                        batch_earliest = dt
            
            if batch_earliest:
                earliest_date = batch_earliest
                print(f"Earliest Date = {earliest_date}, Total Bars = {total_bars}", flush=True)

            next_key = res.headers.get('next-key', "").strip()
            cont_yn = res.headers.get('cont-yn', "N").strip()
            print(f"DEBUG: next_key={next_key}, cont_yn={cont_yn}", flush=True)
            
            if cont_yn != 'Y' or not next_key:
                print("\nEnd of available data reached (cont-yn != 'Y').", flush=True)
                break
            
            # Rate limiting safety
            time.sleep(0.5)

    except Exception as e:
        print(f"\nAn error occurred: {e}")

    print("\n" + "="*50)
    print(f"FINAL RESULT for {code}:")
    print(f"Earliest Date Reached: {earliest_date}")
    print(f"Total Minute Bars Fetched: {total_bars}")
    print("="*50)

if __name__ == "__main__":
    test_limit()
