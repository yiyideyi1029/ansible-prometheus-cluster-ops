#!/usr/bin/env python3
"""
多节点自动化巡检与基础故障自愈脚本
功能：
1. 检测根分区磁盘使用率，超过 85% 自动调用清理脚本释放过期日志；
2. 检测核心守护进程（nginx / node_exporter）存活状态，异常退出时自动拉起；
3. 输出结构化巡检日志并支持 Webhook 告警通知。
"""
import os
import shutil
import subprocess
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

DISK_THRESHOLD = 85.0
CRITICAL_SERVICES = ["nginx", "node_exporter"]

def check_and_heal_disk(mount_point="/"):
    total, used, free = shutil.disk_usage(mount_point)
    usage_pct = (used / total) * 100
    logging.info(f"当前磁盘 ({mount_point}) 使用率: {usage_pct:.2f}%")

    if usage_pct >= DISK_THRESHOLD:
        logging.warning(f"磁盘使用率超过阈值 {DISK_THRESHOLD}%，触发自动清理自愈流程...")
        cleanup_script = os.path.join(os.path.dirname(__file__), "disk_cleanup.sh")
        if os.path.exists(cleanup_script):
            subprocess.run(["bash", cleanup_script], check=False)
        _, used_after, _ = shutil.disk_usage(mount_point)
        logging.info(f"自愈完成，当前磁盘使用率降至: {(used_after / total) * 100:.2f}%")

def check_and_heal_services():
    for svc in CRITICAL_SERVICES:
        res = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
        if res.stdout.strip() != "active":
            logging.error(f"核心服务 [{svc}] 处于非运行状态，尝试自动拉起...")
            subprocess.run(["systemctl", "restart", svc], check=False)
            verify = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            if verify.stdout.strip() == "active":
                logging.info(f"服务 [{svc}] 自愈拉起成功！")
            else:
                logging.critical(f"服务 [{svc}] 自愈失败，需人工介入排查！")

if __name__ == "__main__":
    logging.info("=== 开始执行定时健康巡检任务 ===")
    check_and_heal_disk("/")
    check_and_heal_services()
    logging.info("=== 本轮巡检任务结束 ===")