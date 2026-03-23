import requests
import config

auth_url = f"{config.URL}/oauth2/token"
auth_data = {
    "grant_type": "client_credentials", 
    "appkey": config.APP_KEY, 
    "secretkey": config.APP_SECRET
}

print(f"Testing URL (Form Data): {auth_url}")
try:
    res = requests.post(auth_url, data=auth_data, timeout=10)
    print(f"Status: {res.status_code}")
    with open("test_auth_res.html", "w", encoding="utf-8") as f:
        f.write(res.text)
    print("Response saved to test_auth_res.html")
    try:
        print(f"JSON Response: {res.json()}")
    except:
        print("Not a JSON response.")
except Exception as e:
    print(f"Error: {e}")
