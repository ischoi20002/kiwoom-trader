import FinanceDataReader as fdr
import pandas as pd
from bs4 import BeautifulSoup
import websockets
import asyncio

print("Verifying FinanceDataReader...")
try:
    # Get short-term data for Samsung Electronics
    df = fdr.DataReader('005930', '2025-03-01')
    print(f"FinanceDataReader Success: Row count = {len(df)}")
    print(df.tail(2))
except Exception as e:
    print(f"FinanceDataReader Failed: {e}")

print("\nVerifying BeautifulSoup...")
try:
    soup = BeautifulSoup("<html><body><h1>Hello</h1></body></html>", "lxml")
    print(f"BeautifulSoup Success: {soup.h1.text}")
except Exception as e:
    print(f"BeautifulSoup Failed: {e}")

print("\nVerifying Websockets...")
try:
    # Just check if we can access the module
    print(f"Websockets Version: {websockets.__version__}")
except Exception as e:
    print(f"Websockets Failed: {e}")
