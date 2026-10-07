import requests
import time
from datetime import datetime
import os
import subprocess
import pandas as pd
import threading
from typing import Dict, Optional, List, Tuple

# ==================== Git 推送配置 ====================
GITHUB_REPO = "Juineii/nctwish_km0519"        # 请替换为您的仓库名
GITHUB_BRANCH = "main"                          # 分支名（main 或 master）
PUSH_INTERVAL = 60                              # 推送检查间隔（秒）
# GitHub Personal Access Token 优先从环境变量 GITHUB_TOKEN 读取

# ==================== 监控配置 ====================
POLL_INTERVAL = 10                              # 爬取间隔（秒）
TAIWAN_URL = "https://www.kmonstar.com.tw/products/%E6%87%89%E5%8B%9F-261012-nflying-9th-mini-album-still-%E5%B0%88%E8%BC%AF%E7%99%BC%E8%A1%8C%E7%B4%80%E5%BF%B5%E8%A6%96%E8%A8%8A%E7%B0%BD%E5%90%8D%E6%9C%83.json"
INTERNATIONAL_URL = "https://kmonstar.com/api/v1/event/detail/99906a84-294f-4ff7-84a0-4de4d127c34c"

INTERNATIONAL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://kmonstar.org/zh/eventproductdetail/c52ecf14-69a2-4869-92e3-81d0df35123e",
    "Origin": "https://kmonstar.org",
    "Cookie": "nation=KR"
}

# ==================== 全局线程安全变量 ====================
# 每个 CSV 文件对应的待推送行数
lines_since_last_push: Dict[str, int] = {}
lines_lock = threading.Lock()      # 保护计数器的锁
file_lock = threading.Lock()       # 保护所有 CSV 文件读写的锁

# 成员状态管理
# 成员标识: 例如 "SION", "RIKU" 等，从原始名称中提取英文部分
member_eng_map: Dict[str, str] = {}       # 原始名称 -> 英文标识
member_reverse_map: Dict[str, str] = {}   # 英文标识 -> 原始名称（用于显示）

# 台湾成员: 原始名称 -> 上次库存
taiwan_last_qty: Dict[str, Optional[int]] = {}
taiwan_initial_logged: Dict[str, bool] = {}

# 国际成员: 原始名称 -> 上次库存
international_last_qty: Dict[str, Optional[int]] = {}
international_initial_logged: Dict[str, bool] = {}

# CSV 文件名映射: 英文标识 -> CSV文件名 (如 "SION.csv")
csv_filename_map: Dict[str, str] = {}


# ==================== 辅助函数 ====================
def extract_member_eng(original_name: str) -> str:
    """
    从 "시온 SION" 或 "시온 SION RIKU" 格式中提取所有英文部分
    如果无法提取，则用原始名称（去除空格特殊字符）
    """
    parts = original_name.strip().split()
    # 提取所有纯字母且大写的部分
    eng_parts = [p for p in parts if p.isalpha() and p.isupper()]
    if eng_parts:
        return "_".join(eng_parts)   # 多个英文用下划线连接，如 SION_RIKU
    # 否则返回原始名称（移除空格和特殊字符）
    return original_name.replace(" ", "_").replace("/", "_")

def get_csv_filename(member_eng: str) -> str:
    """获取成员对应的 CSV 文件名"""
    if member_eng not in csv_filename_map:
        csv_filename_map[member_eng] = f"{member_eng}.csv"
    return csv_filename_map[member_eng]

def append_to_csv(csv_filename: str, time_str: str, product_name: str, stock_change: str, single_sales: int):
    global lines_since_last_push
    try:
        columns = ["时间", "商品名称", "库存变化", "单笔销量"]
        single_sales = int(single_sales) if single_sales is not None else 0

        new_row = pd.DataFrame([[time_str, product_name, stock_change, single_sales]], columns=columns)

        with file_lock:
            if os.path.exists(csv_filename):
                df_existing = pd.read_csv(csv_filename, encoding='utf-8-sig')
                if '单笔销量' in df_existing.columns:
                    # 修复警告：安全转换
                    df_existing['单笔销量'] = pd.to_numeric(df_existing['单笔销量'], errors='coerce').fillna(0).astype(int)
            else:
                df_existing = pd.DataFrame(columns=columns)

            new_row['单笔销量'] = new_row['单笔销量'].astype(int)
            df_updated = pd.concat([df_existing, new_row], ignore_index=True)
            # 再次确保列类型（防止 concat 后类型变化）
            df_updated['单笔销量'] = pd.to_numeric(df_updated['单笔销量'], errors='coerce').fillna(0).astype(int)
            df_updated.to_csv(csv_filename, index=False, encoding='utf-8-sig')

        with lines_lock:
            lines_since_last_push[csv_filename] = lines_since_last_push.get(csv_filename, 0) + 1
    except Exception as e:
        print(f"❌ 写入 CSV {csv_filename} 失败: {e}")


# ==================== 数据获取函数 ====================
def get_taiwan_members_stocks() -> Dict[str, Optional[int]]:
    """
    获取台湾地址所有成员的当前库存
    返回: {原始成员名: 库存数量}，失败返回空字典
    """
    try:
        resp = requests.get(TAIWAN_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        variants = data.get("variants", [])
        result = {}
        for variant in variants:
            member = variant.get("option1")
            qty = variant.get("inventory_quantity")
            if member:
                result[member] = int(qty) if qty is not None else None
        return result
    except Exception as e:
        print(f"❌ 台湾地址请求失败: {e}")
        return {}

def get_international_members_stocks() -> Dict[str, Optional[int]]:
    """
    获取国际地址所有成员的当前库存（stockKo.quantity）
    返回: {原始成员名: 库存数量}，失败返回空字典
    """
    try:
        resp = requests.get(INTERNATIONAL_URL, headers=INTERNATIONAL_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        option_list = data.get("data", {}).get("optionList", [])
        result = {}
        for option in option_list:
            member = option.get("optionNameValue1")
            stock_ko = option.get("stockKo")
            if member and stock_ko and "quantity" in stock_ko:
                qty = stock_ko["quantity"]
                result[member] = int(qty) if qty is not None else None
        return result
    except Exception as e:
        print(f"❌ 国际地址请求失败: {e}")
        return {}


# ==================== Git 推送函数 ====================
def git_push_update(files_to_push: List[str]) -> bool:
    """
    将指定的 CSV 文件提交并推送到 GitHub
    参数: files_to_push - 需要推送的文件名列表
    返回: True 表示推送成功, False 表示失败
    """
    if not files_to_push:
        return True

    try:
        token = os.environ.get('GITHUB_TOKEN')
        if not token:
            print("⚠️ 环境变量 GITHUB_TOKEN 未设置，跳过 Git 推送")
            return False

        remote_url = f"https://{token}@github.com/{GITHUB_REPO}.git"

        # 依次添加所有文件
        for fname in files_to_push:
            if os.path.exists(fname):
                subprocess.run(['git', 'add', fname], check=True, capture_output=True, timeout=30)

        # 检查是否有文件变化（避免空提交）
        result = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True, timeout=30)
        if result.returncode != 0:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            commit_msg = f"自动更新数据 {timestamp}"
            subprocess.run(['git', 'commit', '-m', commit_msg], check=True, capture_output=True, timeout=30)
            subprocess.run(
                ['git', 'push', remote_url, f'HEAD:{GITHUB_BRANCH}'],
                check=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            print(f"✅ 已推送到 GitHub: {commit_msg} (文件: {', '.join(files_to_push)})")
            return True
        else:
            print("⏭️ CSV 文件无变化，跳过推送")
            return True

    except subprocess.TimeoutExpired:
        print("❌ Git 操作超时 (30秒)，推送失败")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ Git 操作失败: {e.stderr if e.stderr else e}")
        return False
    except Exception as e:
        print(f"❌ 推送过程中发生错误: {e}")
        return False


# ==================== 推送工作线程 ====================
def push_worker():
    """每分钟检查所有 CSV 文件，将有新数据的文件一次性推送"""
    global lines_since_last_push
    while True:
        time.sleep(PUSH_INTERVAL)

        # 收集有新增数据的文件
        pending_files = []
        with lines_lock:
            for fname, count in lines_since_last_push.items():
                if count > 0:
                    pending_files.append(fname)

        if pending_files:
            print(f"⏰ 定时推送：以下文件有新数据 {pending_files}")
            with file_lock:   # 推送期间禁止写入，保证文件完整
                success = git_push_update(pending_files)
            if success:
                with lines_lock:
                    for fname in pending_files:
                        lines_since_last_push[fname] = 0
                print("✅ 推送成功，对应计数器已归零")
            else:
                print("⚠️ 推送失败，下次再试")


# ==================== 主监控函数 ====================
def monitor_merged():
    global member_eng_map, member_reverse_map
    global taiwan_last_qty, taiwan_initial_logged
    global international_last_qty, international_initial_logged

    print(f"📊 启动合并监控（爬取间隔 {POLL_INTERVAL} 秒，推送间隔 {PUSH_INTERVAL} 秒）")
    print("🇹🇼 台湾 + 🌍 国际 统一按成员分 CSV 文件，商品名称列区分来源")

    # 用于存储所有已知成员（原始名称）
    all_members = set()

    while True:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ---------- 1. 获取台湾数据 ----------
        taiwan_stocks = get_taiwan_members_stocks()
        for member, qty in taiwan_stocks.items():
            if qty is None:
                continue
            all_members.add(member)

            # 确保该成员有映射
            if member not in member_eng_map:
                eng = extract_member_eng(member)
                member_eng_map[member] = eng
                member_reverse_map[eng] = member
                print(f"📁 新成员: {member} -> {eng}.csv")

            # 初始化台湾状态
            if member not in taiwan_last_qty:
                taiwan_last_qty[member] = None
                taiwan_initial_logged[member] = False

            csv_filename = get_csv_filename(member_eng_map[member])
            product_name = "台湾"   # 商品名称列填写“台湾”

            if not taiwan_initial_logged[member]:
                taiwan_last_qty[member] = qty
                taiwan_initial_logged[member] = True
                print(f"{current_time} [台湾][{member}] 初始库存: {qty}")
                append_to_csv(csv_filename, current_time, product_name, f"初始库存：{qty}", abs(qty))
            elif qty != taiwan_last_qty[member]:
                diff = taiwan_last_qty[member] - qty
                print(f"{current_time} [台湾][{member}] 变化: {taiwan_last_qty[member]} -> {qty}, 销量: {diff}")
                append_to_csv(csv_filename, current_time, product_name,
                              f"{taiwan_last_qty[member]} -> {qty}", diff)
                taiwan_last_qty[member] = qty

        # ---------- 2. 获取国际数据 ----------
        international_stocks = get_international_members_stocks()
        for member, qty in international_stocks.items():
            if qty is None:
                continue
            all_members.add(member)

            if member not in member_eng_map:
                eng = extract_member_eng(member)
                member_eng_map[member] = eng
                member_reverse_map[eng] = member
                print(f"📁 新成员: {member} -> {eng}.csv")

            # 初始化国际状态
            if member not in international_last_qty:
                international_last_qty[member] = None
                international_initial_logged[member] = False

            csv_filename = get_csv_filename(member_eng_map[member])
            product_name = "国际"   # 商品名称列填写“国际”

            if not international_initial_logged[member]:
                international_last_qty[member] = qty
                international_initial_logged[member] = True
                print(f"{current_time} [国际][{member}] 初始库存: {qty}")
                append_to_csv(csv_filename, current_time, product_name, f"初始库存：{qty}", 0)
            elif qty != international_last_qty[member]:
                diff = international_last_qty[member] - qty
                print(f"{current_time} [国际][{member}] 变化: {international_last_qty[member]} -> {qty}, 销量: {diff}")
                append_to_csv(csv_filename, current_time, product_name,
                              f"{international_last_qty[member]} -> {qty}", diff)
                international_last_qty[member] = qty

        # 可选的：如果某个成员只出现在一个平台，也是允许的
        time.sleep(POLL_INTERVAL)


# ==================== 程序入口 ====================
if __name__ == "__main__":
    # 启动推送守护线程
    push_thread = threading.Thread(target=push_worker, daemon=True)
    push_thread.start()

    try:
        monitor_merged()
    except KeyboardInterrupt:
        print("\n监控程序被用户终止")
        # 退出前推送所有剩余数据
        with lines_lock:
            pending_files = [fname for fname, count in lines_since_last_push.items() if count > 0]
        if pending_files:
            print(f"正在推送剩余数据（{pending_files}）...")
            with file_lock:
                success = git_push_update(pending_files)
            if success:
                with lines_lock:
                    for fname in pending_files:
                        lines_since_last_push[fname] = 0
                print("✅ 剩余数据已推送")
            else:
                print("⚠️ 剩余数据推送失败，请手动检查")
        else:
            print("无待推送数据")