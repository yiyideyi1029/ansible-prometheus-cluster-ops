# 基于 Ansible 与 Prometheus 的多节点集群自动化管理与监控体系

## 一、项目背景

面向多节点 Linux 生产集群，日常运维存在三类高频重复工作：**系统基线不统一**（内核参数、文件描述符、
SSH 策略逐台漂移）、**监控探针手工部署易遗漏**、**磁盘水位与服务异常依赖人工巡检**。

本项目以 Ansible 为配置编排入口，将系统基线收敛与 `node_exporter` 探针下发全部声明式化；
以 Prometheus + Grafana 构建指标采集与可视化闭环；并以 `Crontab + Python` 自愈脚本补齐
"告警之后的动作"，形成 **编排 → 采集 → 可视化 → 自愈** 的运维闭环。

## 二、系统架构

```text
                        ┌──────────────────────────────────────┐
                        │        Ansible Control Node          │
                        │  site.yml ─┬─ role: system_baseline  │
                        │            ├─ role: docker_runtime   │
                        │            └─ role: node_exporter    │
                        └───────────────┬──────────────────────┘
                                        │ SSH (批量并行 / 幂等下发)
          ┌─────────────────────────────┼─────────────────────────────┐
          ▼                             ▼                             ▼
 ┌─────────────────┐          ┌─────────────────┐          ┌─────────────────┐
 │  web_nodes      │          │   db_nodes      │          │ monitor_server  │
 │ node-web-01/02  │          │  node-db-01     │          │  node-mon-01    │
 │ ┌─────────────┐ │          │ ┌─────────────┐ │          │ ┌─────────────┐ │
 │ │node_exporter│ │          │ │node_exporter│ │          │ │ Prometheus  │ │
 │ │   :9100     │ │          │ │   :9100     │ │          │ │   :9090     │ │
 │ └──────┬──────┘ │          │ └──────┬──────┘ │          │ │  Grafana    │ │
 └────────┼────────┘          └────────┼────────┘          │ │   :3000     │ │
          │  pull /metrics             │                   │ │ Alertmanager│ │
          └────────────────────────────┼──────────────────▶│ └──────┬──────┘ │
                                       │                   └────────┼────────┘
                                       │                            │ 告警通知
                                       │                            ▼
 ┌─────────────────────────────────────┴───────────────────┐  ┌──────────────┐
 │  Crontab (每 5 分钟)                                     │  │ 值班 / 邮件   │
 │  scripts/auto_heal_check.py                             │  └──────────────┘
 │    ├─ 磁盘 > 85% ──▶ 调用 disk_cleanup.sh 清理过期日志    │
 │    └─ nginx / node_exporter 未运行 ──▶ systemctl restart │
 └─────────────────────────────────────────────────────────┘
```

**分层说明**

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 编排层 | Ansible `site.yml` + roles | 基线收敛、Docker 运行时、Exporter 部署 |
| 采集层 | `node_exporter` :9100 | 暴露 CPU / 内存 / 磁盘 / 网络 / 文件系统指标 |
| 存储与告警 | Prometheus :9090 + `alert_rules.yml` | 15s 抓取、15d 保留、规则评估 |
| 可视化 | Grafana :3000 | `Node Overview` 大盘（CPU/内存/磁盘/网络/存活） |
| 自愈层 | `auto_heal_check.py` + `disk_cleanup.sh` | 磁盘高水位清理、核心服务拉起 |

## 三、核心设计与工程实践

### 1. 幂等性 Playbook 设计

幂等是"可重复执行而不改变最终状态"的保证，是本项目可安全反复运行的前提：

- **声明终态而非命令序列**：全部使用 Ansible 模块（`sysctl`、`pam_limits`、`copy`、`user`、`systemd`），
  模块内部先比对期望与实际再决定是否变更，重复执行时 `changed=0`。
- **`sysctl_set: yes` + `reload: yes`**：既写入 `/etc/sysctl.conf` 持久化，又保证内存参数即时生效，
  避免"重启后失效"或"配置未加载"两类经典问题。
- **`pam_limits` 的 soft/hard 成对下发**：用 `loop` 覆盖两种 `limit_type`，避免只改软限制导致实际未生效。
- **`daemon_reload: yes`**：`node_exporter` 的 unit 文件变更后自动重载 systemd，
  而不是依赖人工 `systemctl daemon-reload`。
- **handler 收敛重启动作**：Docker `daemon.json` 变更通过 `notify: restart docker` 触发，
  多个任务同时改动时只重启一次，减少服务抖动。
- **`ignore_errors: yes` 的边界**：仅用于 `authorized_key`（本地无公钥时不阻断基线下发）与
  `node_exporter` 启动（首次部署时二进制可能尚未就位），核心幂等逻辑不受影响。

### 2. 内核网络参数调优原理

| 参数 | 值 | 调优原理 |
| --- | --- | --- |
| `net.ipv4.ip_forward` | `1` | 开启 IPv4 转发，容器网络 / 网关场景必需 |
| `net.ipv4.tcp_tw_reuse` | `1` | 允许复用 `TIME_WAIT` 状态的连接，缓解高并发短连接下端口耗尽 |
| `net.core.somaxconn` | `65535` | 提高已完成握手队列上限，避免瞬时高并发下 accept 队列溢出丢连接 |
| `vm.swappiness` | `10` | 降低换出倾向，优先保留页缓存，减少数据库/中间件因 swap 引起的长尾抖动 |
| 文件描述符 | `65535`（soft/hard） | 提升单进程可打开句柄数，避免 Nginx/Java 类服务 `Too many open files` |

同时通过 `authorized_key` 批量下发运维管理员公钥，统一 SSH 访问入口，替代密码登录。

### 3. 磁盘高水位自愈闭环

```text
Prometheus 规则 HighDiskUsage (>85%, for 2m) ──▶ 告警通知（人工可视）
Crontab 每 5 分钟: auto_heal_check.py
    ├─ shutil.disk_usage("/") 计算使用率
    ├─ ≥ 85% ──▶ 调用 disk_cleanup.sh
    │             find /var/log/app -name "*.log.*" -o -name "*.gz" -mtime +7 -delete
    └─ 重新采样使用率并记录自愈前后对比日志
```

- **阈值对齐**：脚本阈值（85%）与 Prometheus 告警规则同源，避免"告警已发但脚本不动作"的口径分裂。
- **先摘日志再谈扩容**：`disk_cleanup.sh` 使用 `set -euo pipefail`，只删除 `*.log.*` 与 `*.gz`
  这类归档文件且限定 `-mtime +7`，**不动正在写入的活跃日志**，属于低风险保守策略。
- **服务自愈带二次校验**：`systemctl restart` 后再次 `is-active` 确认，
  失败则记录 `CRITICAL` 级别日志转人工，避免"重启即认为恢复"的假成功。
- **可观测性**：自愈过程全部结构化日志输出，可被 `journald` / 日志平台采集，形成审计链路。

### 4. 监控选型与容量

- 抓取周期 15s、保留 15 天（`--storage.tsdb.retention.time=15d`），单节点估算可控，便于单机部署。
- `docker-compose.yml` 一键拉起 Prometheus + Grafana，通过 volume 挂载配置，
  配置变更只需改文件后 `docker compose restart prometheus`，无需重建镜像。
- Grafana 大盘 `node-overview.json` 支持 `instance` 多选变量，可直接导入后切换查看集群任意节点。

## 四、目录结构

```text
ansible-prometheus-cluster-ops/
├── ansible/
│   ├── inventory/
│   │   └── hosts.ini                    # web/db/monitor 分组主机清单
│   ├── roles/
│   │   ├── system_baseline/
│   │   │   └── tasks/main.yml           # 内核调优、ulimit、SSH 公钥
│   │   ├── docker_runtime/
│   │   │   ├── tasks/main.yml           # 幂等安装与配置 Docker
│   │   │   └── handlers/main.yml        # daemon.json 变更后收敛重启
│   │   └── node_exporter/
│   │       └── tasks/main.yml           # 批量下发并注册 systemd 服务
│   └── site.yml                         # 主入口 Playbook
├── monitoring/
│   ├── docker-compose.yml               # Prometheus + Grafana 一键启动
│   ├── prometheus/
│   │   ├── prometheus.yml               # 抓取配置与规则加载
│   │   └── alert_rules.yml              # CPU/内存/磁盘/宕机告警规则
│   └── grafana/
│       └── dashboards/
│           └── node-overview.json       # Grafana 监控大盘模板
├── scripts/
│   ├── auto_heal_check.py               # 自动化巡检与故障自愈
│   └── disk_cleanup.sh                  # 过期日志清理
└── README.md
```

## 五、快速开始

### 1. 执行基线收敛与探针部署

```bash
cd ansible
# 先做连通性验证（免密登录需提前配置）
ansible all -i inventory/hosts.ini -m ping

# 语法检查 + 首次执行
ansible-playbook -i inventory/hosts.ini site.yml --syntax-check
ansible-playbook -i inventory/hosts.ini site.yml

# 再次执行验证幂等性：预期 changed=0
ansible-playbook -i inventory/hosts.ini site.yml
```

### 2. 启动监控栈

```bash
cd monitoring
docker compose up -d
docker compose ps
```

- Prometheus：<http://127.0.0.1:9090>（Status → Targets 中 `linux_cluster_nodes` 应为 UP）
- Grafana：<http://127.0.0.1:3000>（`admin / admin`，导入
  `grafana/dashboards/node-overview.json` 并选择 Prometheus 数据源）

### 3. 部署自愈巡检任务

```bash
sudo install -m 755 scripts/auto_heal_check.py /usr/local/bin/auto_heal_check.py
sudo install -m 755 scripts/disk_cleanup.sh  /usr/local/bin/disk_cleanup.sh

# 每 5 分钟巡检一次
echo '*/5 * * * * root /usr/bin/python3 /usr/local/bin/auto_heal_check.py >> /var/log/auto_heal.log 2>&1' \
  | sudo tee /etc/cron.d/auto_heal_check

# 手动验证一次
sudo python3 /usr/local/bin/auto_heal_check.py
```

### 4. 关键验证点

```bash
# 探针指标可达
curl -s http://192.168.10.11:9100/metrics | head -n 5

# 告警规则语法
docker exec prometheus promtool check rules /etc/prometheus/alert_rules.yml

# 服务自愈演练（停掉 nginx 观察脚本是否自动拉起）
sudo systemctl stop nginx && sudo python3 /usr/local/bin/auto_heal_check.py
```

## 六、环境要求

| 组件 | 版本要求 |
| --- | --- |
| 受控节点 | CentOS 7+/Rocky 8+/Ubuntu 20.04+ |
| Ansible | 2.14+（Control Node） |
| Docker | 24.0+ / Docker Compose v2 |
| Prometheus | v2.53.0 |
| Grafana | 11.1.0 |
| Python | 3.8+（受控节点自带即可） |

> 说明：`inventory/hosts.ini` 中的 IP 为示例网段（192.168.10.x），实际使用时请替换为真实节点地址；
> 请勿在仓库中提交私钥、Token 等敏感信息。