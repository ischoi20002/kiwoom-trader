import os
import shutil
import sys

# [v1.4] Python-based reliable deployment script
# [v3.5.1] Use dynamic path detection
SOURCE = os.path.dirname(os.path.abspath(__file__))
TARGET = r"c:\vibe_op"

def sync():
    print("-" * 50)
    print(f"🚀 Starting Deployment: {SOURCE} -> {TARGET}")
    
    if not os.path.exists(TARGET):
        os.makedirs(TARGET)
        print(f"Created target directory: {TARGET}")

    # 1. Sync Subfolders
    folders = ["core", "gui", "strategy", "utils", "reporting"]
    for folder in folders:
        src_path = os.path.join(SOURCE, folder)
        dst_path = os.path.join(TARGET, folder)
        if os.path.exists(src_path):
            if os.path.exists(dst_path):
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path)
            print(f"Synced folder: {folder}")

    # 2. Sync Root Files
    extensions = [".py", ".json", ".csv", ".md", ".bat", ".ps1"]
    files = [f for f in os.listdir(SOURCE) if os.path.isfile(os.path.join(SOURCE, f))]
    
    for f in files:
        if any(f.lower().endswith(ext) for ext in extensions):
            src_file = os.path.join(SOURCE, f)
            dst_file = os.path.join(TARGET, f)
            
            # Exclusion Logic — 사용자 로컬 데이터 덮어쓰기 방지
            if f.lower() == "trading_data.db":
                continue
            _preserve_if_dst = (
                "virtual_db.json",
                "virtual_db_mock.json",
                "virtual_db_live.json",
                "trading_state.db",
                "trading_state_mock.db",
                "trading_state_live.db",
            )
            if f.lower() in _preserve_if_dst and os.path.exists(dst_file):
                continue
            
            shutil.copy2(src_file, dst_file)
            print(f"Synced file: {f}")

    print("\n✅ Deployment Complete!")
    print("-" * 50)

if __name__ == "__main__":
    sync()
