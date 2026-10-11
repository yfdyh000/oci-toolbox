import csv
import glob
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Fix encoding for Windows console
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_ROOT = BASE_DIR
DATA_DIR = os.path.join(BASE_DIR, "reports", "usage-csv")
COST_DIR = os.path.join(BASE_DIR, "reports", "cost-csv")

OCI_NAMESPACE = "bling"
OCI_BUCKET = "ocid1.tenancy.oc1..a……"
OCI_PREFIX = "reports/usage-csv"
COST_PREFIX = "reports/cost-csv"
FREE_TIER_GB = 10240

# 下载行为：True=覆盖已有文件，False=跳过已存在文件
DOWNLOAD_OVERWRITE = False

# OCI 元数据查询（默认关闭，避免慢查询）
OCI_METADATA_QUERY_ENABLED = False

# 预测偏移（小时）：正常 / 延迟
PREDICT_BASE_HOURS = 4
PREDICT_DELAY_HOURS = 5

# 汇总展示数量
TOP_DAYS = 7
TOP_HOURS = 10

# OCI 对象列表参数
OCI_LIST_TIMEOUT = 30
OCI_PAGE_SIZE = 100
RECENT_INTERVALS = 5

# 数据格式决策说明：
# 1. 网络流量统计选用 Usage CSV（reports_usage-csv_*.csv），
#    原因：唯一提供原始字节级流量 usage/consumedQuantity（BYTES），
#    覆盖完整月份，支持小时/天级聚合与高峰分析。
# 2. Cost CSV（reports_cost-csv_*.csv）不用于流量统计：
#    - 仅有 usage/billedQuantity（GB Months，计费口径），非原始流量；
#    - 适合费用核对，不适合网络流量监控。
# 3. FOCUS Reports（FOCUS Reports_*.csv.gz）不纳入流量统计：
#    - 格式面向成本分摊，核心字段为 BilledCost（金额），非流量；
#    - 文件间隔不规律（5~7 小时），不适合网络流量监控；
#    - 可用场景：成本分析/分摊。
# 4. 若需成本分析，可单独处理 Cost CSV 或 FOCUS Reports。
#
# 参考 Oracle 文档：
# - Cost Reports 通常每 6 小时生成一次，数据可能延迟长达 24 小时，保留一年。
# - Usage Reports 已于 2025-01-31 被弃用，但 OCI 仍在生成。
# - 因此本地 usage-csv 的生成间隔并非官方固定周期，预测仅供参考。

STATE_FILE = os.path.join(tempfile.gettempdir(), "usage_report_helper_state.json")
_CACHED_LATEST_CSV_END = None


def load_state():
    """
    读取上次一键查询的状态文件。

    返回:
        dict | None: 包含 file_count、last_gz_file、last_check_time 的字典；
                     若状态文件不存在或解析失败，返回 None。
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(file_count, last_gz_file):
    """
    保存当前一键查询的状态到临时目录。

    参数:
        file_count (int): 当前 .csv.gz 文件总数。
        last_gz_file (str): 最新一个 .csv.gz 的文件名。
    """
    state = {
        "file_count": file_count,
        "last_gz_file": last_gz_file,
        "last_check_time": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_gz_summary():
    """
    扫描 usage-csv 目录，统计 .csv.gz 文件数量及最新文件名。

    返回:
        tuple[int, str]: (文件总数, 最新文件名)；若目录为空则返回 (0, "")。
    """
    gz_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv.gz")))
    return len(gz_files), os.path.basename(gz_files[-1]) if gz_files else ""


def get_latest_csv_end():
    global _CACHED_LATEST_CSV_END
    if _CACHED_LATEST_CSV_END is not None:
        return _CACHED_LATEST_CSV_END

    latest = None
    for csv_file in glob.glob(os.path.join(DATA_DIR, "*.csv")):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            net_rows = [r for r in rows if r.get("product/service") == "NETWORK"]
            if net_rows:
                end = max(r["lineItem/intervalUsageEnd"] for r in net_rows)
                if latest is None or end > latest:
                    latest = end
        except Exception:
            continue

    _CACHED_LATEST_CSV_END = latest
    return latest


def download():
    if not shutil.which("oci"):
        print("错误: 未找到 oci CLI，请先安装并配置。")
        sys.exit(1)

    download_dir = DOWNLOAD_ROOT
    cmd = [
        "oci", "os", "object", "bulk-download",
        "--namespace-name", OCI_NAMESPACE,
        "--bucket-name", OCI_BUCKET,
        "--prefix", OCI_PREFIX,
        "--download-dir", download_dir,
        "--overwrite" if DOWNLOAD_OVERWRITE else "--no-overwrite",
    ]

    print("[1/3] 正在从 OCI 下载 usage-csv ...")
    print(f"     下载目录: {DOWNLOAD_ROOT}（落到 {DATA_DIR}）")
    result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"下载失败 (return code={result.returncode})")
        if result.stderr:
            print(result.stderr)
        sys.exit(1)

    print("下载完成。")
    if result.stdout:
        print(result.stdout)


def download_cost():
    if not shutil.which("oci"):
        print("错误: 未找到 oci CLI，请先安装并配置。")
        sys.exit(1)

    cmd = [
        "oci", "os", "object", "bulk-download",
        "--namespace-name", OCI_NAMESPACE,
        "--bucket-name", OCI_BUCKET,
        "--prefix", COST_PREFIX,
        "--download-dir", DOWNLOAD_ROOT,
        "--overwrite" if DOWNLOAD_OVERWRITE else "--no-overwrite",
    ]

    print("正在从 OCI 下载 cost-csv ...")
    print(f"     下载目录: {DOWNLOAD_ROOT}（落到 {COST_DIR}）")
    result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"下载失败 (return code={result.returncode})")
        if result.stderr:
            print(result.stderr)
        sys.exit(1)

    print("下载完成。")
    if result.stdout:
        print(result.stdout)


def extract(folder=DATA_DIR):
    gz_files = sorted(glob.glob(os.path.join(folder, "*.csv.gz")))

    if not gz_files:
        print(f"[2/3] 在 {folder} 中未找到 .csv.gz 文件，跳过解压。")
        return

    print(f"[2/3] 正在解压 {len(gz_files)} 个 .csv.gz 文件 ...")
    extracted = 0

    for gz_path in gz_files:
        csv_path = gz_path[:-3]
        try:
            with gzip.open(gz_path, "rb") as f_in:
                with open(csv_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            print(f"  ✓ {os.path.basename(gz_path)} -> {os.path.basename(csv_path)}")
            extracted += 1
        except Exception as e:
            print(f"  ✗ {os.path.basename(gz_path)}: {e}")

    print(f"解压完成，成功 {extracted}/{len(gz_files)} 个文件。")


def extract_cost():
    extract(COST_DIR)


def get_current_utc_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_billing_periods_in_data():
    periods = set()
    csv_files = glob.glob(os.path.join(DATA_DIR, "*.csv"))

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    start = row.get("lineItem/intervalUsageStart", "")
                    if len(start) >= 7:
                        periods.add(start[:7])
        except Exception:
            continue

    return sorted(periods)


def predict_next_query_time():
    """
    根据本地 usage CSV 的历史生成规律预测下次新 CSV 可用时间。
    预测逻辑：
    1. 若环境变量 OCI_METADATA_QUERY=1，尝试通过 OCI CLI 获取对象真实创建时间，
       基于最近几份的创建间隔预测下一份可用时间；
    2. 否则回退到本地 CSV 内容结束时间 + 固定偏移（默认 +4h，延迟时 +5h）；
    3. 无论预测时间是否已过，均返回 UTC+8 时间字符串；
    4. 最终转换为 UTC+8 显示。
    """
    next_dt = _predict_from_oci_metadata()
    if next_dt is None:
        latest_end = get_latest_csv_end()
        if not latest_end:
            return None
        latest_dt = datetime.fromisoformat(latest_end.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        offset = (
            PREDICT_DELAY_HOURS
            if now >= latest_dt + timedelta(hours=PREDICT_BASE_HOURS)
            else PREDICT_BASE_HOURS
        )
        next_dt = latest_dt + timedelta(hours=offset)

    next_cst = next_dt.astimezone(timezone(timedelta(hours=8)))
    return next_cst.strftime("%Y-%m-%d %H:%M UTC+8")


def _predict_from_oci_metadata():
    """
    调用 OCI CLI 获取 usage-csv 对象的真实创建时间，
    基于最近几份的创建间隔预测下一份可用时间。
    仅当环境变量 OCI_METADATA_QUERY=1 时执行，否则返回 None。
    若 OCI CLI 不可用或获取失败，返回 None。
    """
    if not OCI_METADATA_QUERY_ENABLED:
        return None

    if not shutil.which("oci"):
        return None

    try:
        cmd = [
            "oci", "os", "object", "list",
            "--namespace-name", OCI_NAMESPACE,
            "--bucket-name", OCI_BUCKET,
            "--prefix", OCI_PREFIX,
            "--output", "json",
            "--page-size", str(OCI_PAGE_SIZE),
        ]
        result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=OCI_LIST_TIMEOUT)
        if result.returncode != 0:
            return None

        import json as _json
        data = _json.loads(result.stdout)
        items = data.get("data", [])
        if not items:
            return None

        objs = []
        for item in items:
            name = item.get("name", "")
            if not name.endswith(".csv.gz"):
                continue
            time_created = item.get("timeCreated")
            if not time_created:
                continue
            objs.append((name, datetime.fromisoformat(time_created.replace("Z", "+00:00"))))

        if len(objs) < 2:
            return None

        objs.sort(key=lambda x: x[1])
        recent = objs[-RECENT_INTERVALS:]
        intervals = []
        for i in range(1, len(recent)):
            delta = (recent[i][1] - recent[i - 1][1]).total_seconds() / 3600
            intervals.append(delta)

        avg_interval = sum(intervals) / len(intervals)
        latest_time = objs[-1][1]
        next_time = latest_time + timedelta(hours=avg_interval)
        return next_time

    except Exception:
        return None


def select_month():
    """
    列出本地数据中可用的计费周期（月份），供用户选择。
    默认当前 UTC 月份；直接回车使用默认。
    """
    current_month = get_current_utc_month()
    periods = get_billing_periods_in_data()

    if not periods:
        print("未找到任何可用月份。")
        return None

    print("可用计费周期:")
    default_idx = 1
    for i, p in enumerate(periods, 1):
        marker = "（默认）" if p == current_month else ""
        if marker:
            default_idx = i
        print(f"  {i}. {p} {marker}")

    while True:
        try:
            if sys.stdin.isatty():
                choice = input(f"\n选择序号或输入 YYYY-MM（回车默认 {current_month}）: ").strip()
            else:
                choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not choice:
            return current_month if current_month in periods else periods[-1]

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(periods):
                return periods[idx - 1]
            print(f"无效序号，请输入 1-{len(periods)} 或 YYYY-MM。")
            continue

        if choice in periods:
            return choice

        print(f"无效月份 {choice}，请输入 1-{len(periods)} 或 YYYY-MM。")


def summarize(target_period=None):
    current_month = get_current_utc_month()
    periods = get_billing_periods_in_data()

    if not periods:
        print("[3/3] 未找到任何 usage CSV 数据。")
        return

    if target_period is None:
        target_period = current_month
        if target_period not in periods:
            target_period = periods[-1]
            print(f"提示: 当前 UTC 计费周期 {current_month} 无数据，使用最新可用周期 {target_period}\n")
    elif target_period not in periods:
        print(f"提示: 指定计费周期 {target_period} 无数据。")
        return

    print(f"[3/3] 汇总计费周期: {target_period} (UTC)")
    print("=" * 60)

    all_network = []
    hourly = defaultdict(float)
    daily = defaultdict(float)
    file_count = 0

    csv_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception:
            continue

        file_network = []
        for row in rows:
            if row.get("product/service") != "NETWORK":
                continue
            start = row.get("lineItem/intervalUsageStart", "")
            if not start.startswith(target_period):
                continue
            qty = float(row.get("usage/consumedQuantity", "0") or "0")
            file_network.append((start, qty))

        if not file_network:
            continue

        file_count += 1
        all_network.extend(file_network)

        for start, qty in file_network:
            hour = start[:13]
            day = start[:10]
            hourly[hour] += qty
            daily[day] += qty

    if not all_network:
        print(f"计费周期 {target_period} 内无 NETWORK 数据。")
        return

    total_bytes = sum(qty for _, qty in all_network)
    total_gb = total_bytes / 1024**3
    total_tib = total_bytes / 1024**4

    starts = [s for s, _ in all_network]
    ends = [s for s, _ in all_network]
    start_dt = datetime.fromisoformat(min(starts).replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(max(ends).replace("Z", "+00:00"))

    free_tier_bytes = FREE_TIER_GB * 1024**3
    usage_pct = min(total_bytes / free_tier_bytes * 100, 100) if free_tier_bytes > 0 else 0
    remaining_gb = max(FREE_TIER_GB - total_gb, 0)

    # 概览
    print()
    print("【概览】")
    print(f"  计费周期: {target_period}")
    print(f"  时间范围: {start_dt.strftime('%Y-%m-%d %H:%M UTC')} ~ {end_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  当前 UTC 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  有效 NETWORK 文件数: {file_count}")
    print(f"  总出站流量: {total_gb:.4f} GB ({total_tib:.2f} TiB)")
    print(f"  Always Free 出站额度: {FREE_TIER_GB} GB/月")
    print(f"  已用百分比: {usage_pct:.2f}%")
    print(f"  剩余额度: {remaining_gb:.4f} GB")
    print()

    # 按天汇总（仅显示流量最高的 TOP_DAYS 天）
    if daily:
        print(f"【按天汇总 TOP {TOP_DAYS}】")
        top_days = sorted(daily.items(), key=lambda x: -x[1])[:TOP_DAYS]
        for day, qty in top_days:
            gb = qty / 1024**3
            print(f"  {day}: {gb:.4f} GB")
        print()

    # 高峰小时
    if hourly:
        print(f"【TOP {TOP_HOURS} 高峰小时】")
        for hour, qty in sorted(hourly.items(), key=lambda x: -x[1])[:TOP_HOURS]:
            gb = qty / 1024**3
            print(f"  {hour}: {gb:.4f} GB")
        print()

    next_time = predict_next_query_time()
    if next_time:
        #print(f"推测: 下一份 CSV 可用时间: {next_time}") # 目前很不准


def print_menu():
    print("用法: 输入选项后回车")
    print()
    print("选项:")
    print("  0 = 一键查询 (下载 + 解压 + 汇总)")
    print("  1 = 调用 oci 命令下载（不覆盖已有文件）")
    print("  2 = 解压 gz 到所在目录（usage + cost）")
    print("  3 = 查询 csv 汇总网络流量等配额（本计费周期）")
    print("  4 = 下载费用报告（cost-csv）")
    print("  5 = 选择月份查询汇总")
    print("  e = 退出")


def main():
    print_menu()

    while True:
        try:
            if sys.stdin.isatty():
                option = input("\n请输入选项: ").strip().lower()
            else:
                option = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)

        if option == "e":
            print("退出。")
            sys.exit(0)

        if option in ("", "0"):
            before_count, before_last = get_gz_summary()
            state = load_state()

            download()

            after_count, after_last = get_gz_summary()
            if (
                state
                and after_count > 0
                and after_count == before_count
                and after_count == state.get("file_count", 0)
                and after_last == state.get("last_gz_file", "")
            ):
                last_time = state.get("last_check_time", "未知")
                print(f"\n提示: 上次检查 ({last_time}) 至今无新增文件，当前共 {after_count} 个 .csv.gz 文件。")
            else:
                extract()

            summarize()

            save_state(after_count, after_last)
            sys.exit(0)
        elif option == "1":
            download()
        elif option == "2":
            extract()
            extract_cost()
        elif option == "3":
            summarize()
            sys.exit(0)
        elif option == "4":
            download_cost()
        elif option == "5":
            period = select_month()
            if period:
                summarize(period)
                sys.exit(0)
        else:
            print(f"无效选项: {option}")
            print_menu()


if __name__ == "__main__":
    main()
